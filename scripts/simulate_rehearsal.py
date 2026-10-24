#!/usr/bin/env python3
"""Interactive terminal Dutch rehearsal simulator over /v1/rehearsal/ws.

Allows practicing NextTime's bounded 90-second Dutch conversation turns without
Xcode or SwiftUI, supporting WAV/PCM audio file uploads, typed/tapped phrases,
console haptic pulse simulation, and post-rehearsal breakdown summaries.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import socket
import ssl
import struct
import sys
import urllib.parse
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol, TextIO

DEFAULT_LEARNER_ID = "01000000000000000000000002"
CHUNK_SIZE_BYTES = 16_000  # 0.5s of 16kHz mono s16le PCM per chunk


class WsConnectionProtocol(Protocol):
    async def send_json(self, payload: dict[str, Any]) -> None: ...

    async def recv_json(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class InProcessAsgiWsClient:
    """In-process ASGI WebSocket client connecting directly to FastAPI /v1/rehearsal/ws."""

    def __init__(self, app: Any, *, path: str, query_string: str, token: str) -> None:
        self._app = app
        self._path = path
        self._query_string = query_string.encode("utf-8")
        self._token = token
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "http_version": "1.1",
            "path": self._path,
            "raw_path": self._path.encode("utf-8"),
            "query_string": self._query_string,
            "headers": [
                (b"host", b"nexttime.local"),
                (b"authorization", f"Bearer {self._token}".encode()),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("nexttime.local", 80),
            "subprotocols": [],
        }

        async def _receive() -> dict[str, Any]:
            return await self._to_app.get()

        async def _send(message: dict[str, Any]) -> None:
            await self._from_app.put(message)

        await self._to_app.put({"type": "websocket.connect"})
        self._task = asyncio.create_task(self._app(scope, _receive, _send))
        first = await self._from_app.get()
        if first.get("type") == "websocket.close":
            reason = str(first.get("reason") or "handshake_rejected")
            code = int(first.get("code") or 1008)
            raise ConnectionError(f"WebSocket handshake closed ({code}: {reason})")
        if first.get("type") != "websocket.accept":
            raise ConnectionError(f"Unexpected ASGI WebSocket event: {first}")

    async def send_json(self, payload: dict[str, Any]) -> None:
        await self._to_app.put(
            {"type": "websocket.receive", "text": json.dumps(payload, ensure_ascii=False)}
        )

    async def recv_json(self) -> dict[str, Any]:
        msg = await self._from_app.get()
        if msg.get("type") == "websocket.close":
            raise EOFError("WebSocket closed by server")
        text = msg.get("text")
        if not isinstance(text, str):
            raise ValueError("Expected text WebSocket frame")
        parsed: dict[str, Any] = json.loads(text)
        return parsed

    async def close(self) -> None:
        await self._to_app.put({"type": "websocket.disconnect", "code": 1000})
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)


class StdlibTcpWsClient:
    """Minimal RFC 6455 WebSocket client using Python stdlib sockets for live --ws-url mode."""

    def __init__(self, ws_url: str, *, token: str) -> None:
        self._url = ws_url
        self._token = token
        self._sock: socket.socket | None = None

    def _connect_sync(self) -> None:
        parsed = urllib.parse.urlsplit(self._url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError(f"Invalid WebSocket URL: {self._url}")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        raw_sock = socket.create_connection((parsed.hostname, port), timeout=10.0)
        if parsed.scheme == "wss":
            ctx = ssl.create_default_context()
            raw_sock = ctx.wrap_socket(raw_sock, server_hostname=parsed.hostname)
        ws_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        req = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {self._token}\r\n\r\n"
        )
        raw_sock.sendall(req.encode("utf-8"))
        header_buf = bytearray()
        while b"\r\n\r\n" not in header_buf:
            chunk = raw_sock.recv(1024)
            if not chunk:
                raw_sock.close()
                raise ConnectionError("Server closed connection during WebSocket upgrade")
            header_buf.extend(chunk)
        status_line = header_buf.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in status_line:
            raw_sock.close()
            raise ConnectionError(f"WebSocket upgrade rejected: {status_line}")
        expected_accept = base64.b64encode(
            hashlib.sha1(  # noqa: S324 - RFC 6455 §4.2.2 mandates SHA-1 for Sec-WebSocket-Accept
                (ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        if expected_accept.encode("ascii") not in header_buf:
            raw_sock.close()
            raise ConnectionError("Invalid Sec-WebSocket-Accept response header")
        self._sock = raw_sock

    async def connect(self) -> None:
        await asyncio.to_thread(self._connect_sync)

    def _send_text_sync(self, text: str) -> None:
        if self._sock is None:
            raise ConnectionError("WebSocket is not connected")
        data = text.encode("utf-8")
        mask_key = secrets.token_bytes(4)
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
        self._sock.sendall(bytes(header) + mask_key + masked)

    def _recv_exact(self, count: int) -> bytes:
        if self._sock is None:
            raise ConnectionError("WebSocket is not connected")
        buf = bytearray()
        while len(buf) < count:
            part = self._sock.recv(count - len(buf))
            if not part:
                raise EOFError("WebSocket closed unexpectedly")
            buf.extend(part)
        return bytes(buf)

    def _recv_text_sync(self) -> str:
        first_two = self._recv_exact(2)
        opcode = first_two[0] & 0x0F
        masked = bool(first_two[1] & 0x80)
        length = first_two[1] & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._recv_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._recv_exact(8))
        mask_key = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:
            raise EOFError("WebSocket closed by server")
        return payload.decode("utf-8")

    async def send_json(self, payload: dict[str, Any]) -> None:
        await asyncio.to_thread(self._send_text_sync, json.dumps(payload, ensure_ascii=False))

    async def recv_json(self) -> dict[str, Any]:
        raw = await asyncio.to_thread(self._recv_text_sync)
        parsed: dict[str, Any] = json.loads(raw)
        return parsed

    async def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None


def load_pcm_chunks_from_file(
    file_path: Path,
    *,
    chunk_size_bytes: int = CHUNK_SIZE_BYTES,
) -> list[bytes]:
    """Load a .wav or raw .pcm file into bounded 16kHz mono 16-bit s16le chunks."""
    if not file_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {file_path}")

    if file_path.suffix.lower() == ".wav":
        with wave.open(str(file_path), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            raw_frames = wf.readframes(wf.getnframes())
        if sampwidth != 2:
            raise ValueError("WAV file must use 16-bit linear PCM (sample width = 2 bytes)")
        samples = list(struct.unpack(f"<{len(raw_frames) // 2}h", raw_frames))
        if channels > 1:
            samples = [
                int(sum(samples[i : i + channels]) / channels)
                for i in range(0, len(samples) - channels + 1, channels)
            ]
        if framerate != 16_000 and framerate > 0 and samples:
            target_len = max(1, int(len(samples) * 16_000 / framerate))
            samples = [
                samples[min(len(samples) - 1, int(idx * framerate / 16_000))]
                for idx in range(target_len)
            ]
        pcm_bytes = struct.pack(f"<{len(samples)}h", *samples)
    else:
        pcm_bytes = file_path.read_bytes()
        if len(pcm_bytes) % 2 != 0:
            pcm_bytes = pcm_bytes[:-1]

    if not pcm_bytes:
        raise ValueError(f"Audio file '{file_path}' contains zero PCM samples")

    even_chunk = max(2, chunk_size_bytes - (chunk_size_bytes % 2))
    return [pcm_bytes[i : i + even_chunk] for i in range(0, len(pcm_bytes), even_chunk)]


def format_haptic_event(msg: dict[str, Any]) -> str:
    if not msg.get("emitted"):
        reason = msg.get("suppression_reason") or "suppressed"
        caption = msg.get("visual_caption_key") or "none"
        voice = msg.get("voiceover_announcement", "")
        return f"  [HAPTIC ○ SUPPRESSED ({reason})] caption={caption} | {voice}"
    pattern = msg.get("pattern")
    seq = msg.get("sequence") or {}
    total_ms = seq.get("total_duration_ms", 0)
    trigger = msg.get("trigger", "")
    voiceover = msg.get("voiceover_announcement", "")
    if pattern == "STEADY_BUZZ":
        glyph = "━━━●━━━"
        detail = f"{total_ms}ms"
    else:
        pulses = seq.get("pulse_durations_ms", [60, 60])
        gaps = seq.get("gap_durations_ms", [80])
        glyph = "●━●"
        detail = f"{pulses[0]}ms-{gaps[0]}ms-{pulses[1]}ms ({total_ms}ms)"
    return f"  [HAPTIC {glyph} {pattern} {detail}] ({trigger}) {voiceover}"


def format_counterpart_event(msg: dict[str, Any]) -> str:
    audio_info = msg.get("counterpart_audio") or {}
    lines = [
        f"\n── Counterpart ({msg.get('counterpart_persona')}) [{msg.get('turn_id')}] ──",
        f"  NL: {msg.get('counterpart_text_nl')}",
        f"  EN: {msg.get('counterpart_text_en')}",
        f"  Expected function(s): {', '.join(msg.get('expected_functions') or [])}",
        f"  Audio stream: {audio_info.get('signed_url', 'n/a')}",
    ]
    phrases = msg.get("support_phrases") or []
    if phrases:
        p0 = phrases[0]
        lines.append(f"  Support phrase [{p0['phrase_id']}]: {p0['text_nl']} ({p0['text_en']})")
    return "\n".join(lines)


def format_breakdown_summary(msg: dict[str, Any]) -> str:
    lines = [
        "\n════════════════ POST-REHEARSAL BREAKDOWN SUMMARY ════════════════",
        f"  Rehearsal ID:                 {msg.get('rehearsal_id')}",
        f"  Situation / Target:           {msg.get('situation')} -> {msg.get('target_function')}",
        (
            f"  Session State / Turns:        {msg.get('state')} "
            f"({msg.get('committed_turn_count')} committed)"
        ),
        (
            f"  Model Version (Fallback):     {msg.get('model_version')} "
            f"(fallback={msg.get('fallback_used')})"
        ),
        f"  Other-Speaker Retained:       {msg.get('other_speaker_transcript_retained')}",
        f"  Raw PCM Bytes Retained:       {msg.get('raw_pcm_retained_bytes')}",
        f"  Authentic Mastery Updated:    {msg.get('authentic_mastery_updated')}",
        "  Minimized Learner Excerpts:",
    ]
    excerpts = msg.get("minimized_excerpts") or []
    if not excerpts:
        lines.append("    (none)")
    for ex in excerpts:
        lines.append(f'    - "{ex["text"]}" (conf={ex["confidence"]:.2f})')

    lines.append("  Rubric Criteria:")
    for crit in msg.get("criteria") or []:
        lines.append(
            f"    - {crit['criterion_id']}: {crit['result']} (conf={crit['model_confidence']:.2f})"
        )

    lines.append("  Coaching Breakdowns:")
    breakdowns = msg.get("breakdowns") or []
    if not breakdowns:
        lines.append("    - None detected — target communicative functions satisfied!")
    for b in breakdowns:
        lines.append(
            f'    - [{b["code"]}] {b["description"]} (evidence: "{b["self_evidence_excerpt"]}")'
        )
    lines.append("══════════════════════════════════════════════════════════════════")
    return "\n".join(lines)


async def run_simulator_session(
    *,
    situation: str = "repair_appointment",
    ws_url: str | None = None,
    rehearsal_id: str | None = None,
    learner_id: str = DEFAULT_LEARNER_ID,
    haptics_enabled: bool = True,
    scripted_inputs: Sequence[str] | None = None,
    input_fn: Callable[[str], str] = input,
    out: TextIO = sys.stdout,
) -> dict[str, Any] | None:
    """Execute an interactive or scripted terminal rehearsal session."""
    token = f"learner:{learner_id}"
    query_params: dict[str, str] = {
        "target_device": "cli_rehearsal_simulator",
        "haptics_enabled": "true" if haptics_enabled else "false",
    }
    if rehearsal_id:
        query_params["rehearsal_id"] = rehearsal_id
    else:
        query_params["situation"] = situation
    query_str = urllib.parse.urlencode(query_params)

    client: WsConnectionProtocol
    if ws_url:
        sep = "&" if "?" in ws_url else "?"
        full_url = f"{ws_url}{sep}{query_str}"
        tcp_client = StdlibTcpWsClient(full_url, token=token)
        await tcp_client.connect()
        client = tcp_client
    else:
        from nexttime_api import create_app

        asgi_client = InProcessAsgiWsClient(
            create_app(),
            path="/v1/rehearsal/ws",
            query_string=query_str,
            token=token,
        )
        await asgi_client.connect()
        client = asgi_client

    summary: dict[str, Any] | None = None
    script_queue = list(scripted_inputs) if scripted_inputs is not None else None
    turn_counter = 0

    try:
        ready = await client.recv_json()
        print(
            "[SIMULATOR / FIXTURE MODE — No Xcode/SwiftUI, Bee hardware, or live AWS; "
            "rehearsal never updates authentic mastery]",
            file=out,
        )
        print(
            f"Connected: rehearsal={ready.get('rehearsal_id')} "
            f"situation={ready.get('situation')} "
            f"budget={ready.get('estimated_seconds')}s",
            file=out,
        )

        # Request initial counterpart prompt
        await client.send_json({"type": "request_counterpart", "request_id": "init-cp"})
        cp_msg = await client.recv_json()
        hap_msg = await client.recv_json()
        print(format_counterpart_event(cp_msg), file=out)
        print(format_haptic_event(hap_msg), file=out)

        while True:
            if script_queue is not None:
                if not script_queue:
                    await client.send_json({"type": "phrase_attempt", "exit_early": True})
                    end_hap = await client.recv_json()
                    print(format_haptic_event(end_hap), file=out)
                    summary = await client.recv_json()
                    print(format_breakdown_summary(summary), file=out)
                    break
                raw_entry = script_queue.pop(0)
                print(f"\nYou (nl)> {raw_entry}", file=out)
            else:
                try:
                    raw_entry = input_fn(
                        "\nYou (nl) [Dutch text | /audio <wav> | /tap <id> | /replay | /quit]> "
                    )
                except EOFError:
                    raw_entry = "/quit"

            stripped = raw_entry.strip()
            if not stripped:
                continue

            if stripped == "/quit":
                await client.send_json({"type": "phrase_attempt", "exit_early": True})
                quit_hap = await client.recv_json()
                print(format_haptic_event(quit_hap), file=out)
                summary = await client.recv_json()
                print(format_breakdown_summary(summary), file=out)
                break

            if stripped == "/replay":
                turn_counter += 1
                await client.send_json(
                    {"type": "request_counterpart", "request_id": f"replay-{turn_counter}"}
                )
                cp_replay = await client.recv_json()
                hap_replay = await client.recv_json()
                print(format_counterpart_event(cp_replay), file=out)
                print(format_haptic_event(hap_replay), file=out)
                continue

            turn_counter += 1
            turn_event_id = f"sim-turn-{turn_counter}"

            if stripped.startswith("/audio "):
                rest = stripped[len("/audio ") :].strip()
                parts = rest.split(maxsplit=1)
                audio_path = Path(parts[0])
                hint = parts[1].strip() if len(parts) > 1 else None
                pcm_chunks = load_pcm_chunks_from_file(audio_path)

                for idx, chunk_bytes in enumerate(pcm_chunks, start=1):
                    is_last = idx == len(pcm_chunks)
                    payload: dict[str, Any] = {
                        "type": "audio_chunk",
                        "turn_event_id": turn_event_id,
                        "sequence_id": idx,
                        "pcm_base64": base64.b64encode(chunk_bytes).decode("ascii"),
                        "is_final": is_last,
                        "streaming_transcription_consented": True,
                        "capture_origin": "cli_rehearsal_simulator",
                    }
                    if is_last and hint:
                        payload["transcript_hint"] = hint
                    await client.send_json(payload)

                    if not is_last:
                        if idx == 1:
                            start_hap = await client.recv_json()
                            print(format_haptic_event(start_hap), file=out)
                        _partial = await client.recv_json()
                    else:
                        tx_final = await client.recv_json()
                        rel_hap = await client.recv_json()
                        print(
                            f'  [TRANSCRIPT] "{tx_final.get("transcript")}" '
                            f"(mapped={tx_final.get('mapped_function')}, "
                            f"support={tx_final.get('support')}, "
                            f"pcm_deleted={tx_final.get('raw_audio_deleted')})",
                            file=out,
                        )
                        print(format_haptic_event(rel_hap), file=out)
                        if tx_final.get("state") == "completed":
                            summary = await client.recv_json()
                            print(format_breakdown_summary(summary), file=out)
                            return summary
                        if tx_final.get("mapped_function") is not None:
                            await client.send_json(
                                {
                                    "type": "request_counterpart",
                                    "request_id": f"next-cp-{turn_counter}",
                                }
                            )
                            next_cp = await client.recv_json()
                            next_hap = await client.recv_json()
                            print(format_counterpart_event(next_cp), file=out)
                            print(format_haptic_event(next_hap), file=out)
                continue

            if stripped.startswith("/tap "):
                phrase_id = stripped[len("/tap ") :].strip()
                await client.send_json(
                    {
                        "type": "phrase_attempt",
                        "turn_event_id": turn_event_id,
                        "transcript": "",
                        "phrase_id": phrase_id,
                        "mode": "tap",
                        "confidence": 1.0,
                    }
                )
            else:
                await client.send_json(
                    {
                        "type": "phrase_attempt",
                        "turn_event_id": turn_event_id,
                        "transcript": stripped,
                        "mode": "text",
                        "confidence": 0.95,
                    }
                )

            tx_msg = await client.recv_json()
            if tx_msg.get("type") == "error":
                print(f"  [ERROR {tx_msg.get('code')}] {tx_msg.get('detail')}", file=out)
                continue
            hap_after = await client.recv_json()
            print(
                f'  [TURN] "{tx_msg.get("transcript")}" '
                f"(mapped={tx_msg.get('mapped_function')}, support={tx_msg.get('support')})",
                file=out,
            )
            print(format_haptic_event(hap_after), file=out)

            if tx_msg.get("state") == "completed":
                summary = await client.recv_json()
                print(format_breakdown_summary(summary), file=out)
                break

            if tx_msg.get("mapped_function") is not None:
                await client.send_json(
                    {"type": "request_counterpart", "request_id": f"next-cp-{turn_counter}"}
                )
                next_cp = await client.recv_json()
                next_hap = await client.recv_json()
                print(format_counterpart_event(next_cp), file=out)
                print(format_haptic_event(next_hap), file=out)
    finally:
        await client.close()

    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Interactive NextTime Dutch rehearsal terminal simulator (/v1/rehearsal/ws)."
    )
    parser.add_argument(
        "--situation",
        choices=["repair_appointment", "school_admin_appointment", "shift_clarification"],
        default="repair_appointment",
        help="Dutch real-world situation to rehearse.",
    )
    parser.add_argument(
        "--ws-url",
        default=None,
        help="Optional live WebSocket URL (e.g. ws://127.0.0.1:8000/v1/rehearsal/ws). "
        "Runs in-process against create_app() when omitted.",
    )
    parser.add_argument(
        "--rehearsal-id",
        default=None,
        help="Optional existing rehearsal_id to attach to.",
    )
    parser.add_argument(
        "--learner-id",
        default=DEFAULT_LEARNER_ID,
        help="26-char ULID learner identifier for local bearer token.",
    )
    parser.add_argument(
        "--audio-file",
        action="append",
        default=[],
        help="Upload a .wav or .pcm file as a push-to-talk turn (may be passed multiple times).",
    )
    parser.add_argument(
        "--phrases",
        nargs="*",
        default=None,
        help="Optional scripted Dutch phrases for non-interactive simulation.",
    )
    parser.add_argument(
        "--no-haptics",
        action="store_true",
        help="Disable tactile pulse simulation (accessibility caption/VoiceOver mode).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    scripted: list[str] | None = None
    if args.audio_file or args.phrases is not None:
        scripted = [f"/audio {p}" for p in args.audio_file]
        if args.phrases:
            scripted.extend(args.phrases)

    asyncio.run(
        run_simulator_session(
            situation=args.situation,
            ws_url=args.ws_url,
            rehearsal_id=args.rehearsal_id,
            learner_id=args.learner_id,
            haptics_enabled=not args.no_haptics,
            scripted_inputs=scripted,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
