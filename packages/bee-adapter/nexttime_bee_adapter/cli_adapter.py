from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexttime_domain.speaker_separation import RawSpeakerSegment

from .protocol import (
    AdapterHealth,
    BeeAdapterError,
    BeeRecordingSummary,
    BeeTranscript,
    DeviceKind,
    ProvenanceKind,
    RecordingPage,
    redact_bridge_log,
)

MAX_CLI_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_OUTPUT_BYTES = 1_048_576


def _parse_timestamp(raw: object, field: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise BeeAdapterError("invalid_cli_payload", f"Missing {field} timestamp")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise BeeAdapterError("invalid_cli_payload", f"Invalid {field} timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BeeAdapterError("invalid_cli_payload", f"{field} timestamp lacks timezone")
    return parsed.astimezone(UTC)


def _parse_device_type(raw: object) -> DeviceKind:
    if raw == "bee_device":
        return "bee_device"
    if raw == "apple_watch_bee":
        return "apple_watch_bee"
    raise BeeAdapterError("invalid_cli_payload", "Unsupported Bee device_type")


def _parse_provenance(raw: object, default: ProvenanceKind) -> ProvenanceKind:
    if raw is None:
        return default
    if raw in {"live", "live_bee"}:
        return "live"
    if raw == "fixture":
        return "fixture"
    raise BeeAdapterError("invalid_cli_payload", "Unsupported provenance value")


def parse_recording_page_json(
    payload: Mapping[str, Any],
    *,
    default_provenance: ProvenanceKind = "fixture",
) -> RecordingPage:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise BeeAdapterError("invalid_cli_payload", "Recording page items must be a list")
    items: list[BeeRecordingSummary] = []
    for entry in raw_items:
        if not isinstance(entry, Mapping):
            raise BeeAdapterError("invalid_cli_payload", "Recording summary must be an object")
        ref = entry.get("reference")
        if not isinstance(ref, str) or not ref.strip():
            raise BeeAdapterError("invalid_cli_payload", "Recording summary lacks reference")
        items.append(
            BeeRecordingSummary(
                reference=ref.strip(),
                recorded_at=_parse_timestamp(entry.get("recorded_at"), "recorded_at"),
                device_type=_parse_device_type(entry.get("device_type", "bee_device")),
                provenance=_parse_provenance(entry.get("provenance"), default_provenance),
            )
        )
    next_cursor = payload.get("next_cursor")
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise BeeAdapterError("invalid_cli_payload", "next_cursor must be a string or null")
    return RecordingPage(items=tuple(items), next_cursor=next_cursor)


def parse_transcript_json(
    payload: Mapping[str, Any],
    *,
    default_provenance: ProvenanceKind = "fixture",
) -> BeeTranscript:
    ref = payload.get("reference")
    if not isinstance(ref, str) or not ref.strip():
        raise BeeAdapterError("invalid_cli_payload", "Transcript payload lacks reference")
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise BeeAdapterError("invalid_cli_payload", "Transcript payload requires segments")

    segments: list[RawSpeakerSegment] = []
    for index, seg in enumerate(raw_segments):
        if not isinstance(seg, Mapping):
            raise BeeAdapterError("invalid_cli_payload", "Transcript segment must be an object")
        seg_id = str(seg.get("segment_id") or f"seg-{index + 1}").strip()
        raw_label = seg.get("speaker_label")
        if raw_label is not None and not isinstance(raw_label, str):
            raise BeeAdapterError("invalid_cli_payload", "speaker_label must be string or null")
        text = seg.get("text")
        if not isinstance(text, str) or not text.strip():
            raise BeeAdapterError("invalid_cli_payload", "Transcript segment text required")
        start_ms = seg.get("start_ms")
        end_ms = seg.get("end_ms")
        asr_conf = seg.get("asr_confidence", 0.9)
        spk_conf = seg.get("speaker_confidence", 1.0)
        overlap = seg.get("overlapping_speech", False)
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or isinstance(asr_conf, bool)
            or not isinstance(asr_conf, int | float)
            or isinstance(spk_conf, bool)
            or not isinstance(spk_conf, int | float)
            or not isinstance(overlap, bool)
        ):
            raise BeeAdapterError("invalid_cli_payload", "Invalid segment numeric/boolean field")
        segments.append(
            RawSpeakerSegment(
                segment_id=seg_id,
                raw_speaker_label=raw_label,
                text=text,
                start_ms=start_ms,
                end_ms=end_ms,
                asr_confidence=float(asr_conf),
                speaker_confidence=float(spk_conf),
                overlapping_speech=overlap,
            )
        )

    return BeeTranscript(
        reference=ref.strip(),
        recorded_at=_parse_timestamp(payload.get("recorded_at"), "recorded_at"),
        device_type=_parse_device_type(payload.get("device_type", "bee_device")),
        segments=tuple(segments),
        provenance=_parse_provenance(payload.get("provenance"), default_provenance),
    )


@dataclass(frozen=True)
class BeeCliConfig:
    executable_path: str
    health_args: tuple[str, ...]
    list_args: tuple[str, ...]
    transcript_args: tuple[str, ...]
    acknowledge_args: tuple[str, ...] = ()
    cursor_flag: str = "--cursor"
    reference_flag: str = "--reference"
    timeout_seconds: float = MAX_CLI_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    provenance: ProvenanceKind = "fixture"

    def __post_init__(self) -> None:
        exe = Path(self.executable_path)
        if not exe.is_absolute() or not exe.is_file() or not os.access(exe, os.X_OK):
            raise ValueError("Bee CLI executable_path must be an existing absolute executable")
        if not 0 < self.timeout_seconds <= MAX_CLI_TIMEOUT_SECONDS:
            raise ValueError("Bee CLI timeout_seconds must be within (0, 30]")
        if self.max_output_bytes < 1024:
            raise ValueError("Bee CLI max_output_bytes must be at least 1024")
        if not self.health_args or not self.list_args or not self.transcript_args:
            raise ValueError("Verified health, list, and transcript CLI arguments are required")


async def _read_bounded_stream(
    stream: asyncio.StreamReader | None,
    max_bytes: int,
) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(16_384)
        if not chunk:
            return b"".join(chunks), False
        total += len(chunk)
        if total > max_bytes:
            return b"", True
        chunks.append(chunk)


class BeeCliAdapter:
    """Executes a verified Bee CLI with argument arrays, 30s timeout, and bounded output."""

    def __init__(self, config: BeeCliConfig | None = None) -> None:
        self._config = config
        self._acknowledged_cursors: dict[str, str] = {}

    @property
    def acknowledged_cursors(self) -> dict[str, str]:
        return dict(self._acknowledged_cursors)

    def _require_config(self) -> BeeCliConfig:
        if self._config is None:
            raise BeeAdapterError(
                "bee_cli_not_configured",
                "Official Bee CLI path and verified command mapping are not configured",
                retryable=False,
            )
        return self._config

    async def _run_cli(self, args: tuple[str, ...]) -> bytes:
        config = self._require_config()
        proc = await asyncio.create_subprocess_exec(
            config.executable_path,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            (stdout_bytes, out_overflow), (stderr_bytes, err_overflow) = await asyncio.wait_for(
                asyncio.gather(
                    _read_bounded_stream(proc.stdout, config.max_output_bytes),
                    _read_bounded_stream(proc.stderr, config.max_output_bytes),
                ),
                timeout=config.timeout_seconds,
            )
            if out_overflow or err_overflow:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
                raise BeeAdapterError(
                    "cli_output_exceeded_bound",
                    "Bee CLI output exceeded bounded byte limit",
                    retryable=False,
                )
            return_code = await asyncio.wait_for(proc.wait(), timeout=config.timeout_seconds)
        except TimeoutError as error:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise BeeAdapterError(
                "cli_timeout",
                f"Bee CLI timed out after {config.timeout_seconds}s",
                retryable=True,
            ) from error

        if return_code != 0:
            safe_err = redact_bridge_log(stderr_bytes.decode("utf-8", errors="replace"))
            raise BeeAdapterError(
                "cli_nonzero_exit",
                f"Bee CLI exited with status {return_code}: {safe_err}",
                retryable=return_code in {75, 111},
            )
        return stdout_bytes

    def _decode_json_object(self, raw_bytes: bytes) -> dict[str, Any]:
        try:
            decoded = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BeeAdapterError(
                "invalid_cli_json",
                "Bee CLI returned invalid JSON output",
                retryable=False,
            ) from error
        if not isinstance(decoded, dict):
            raise BeeAdapterError(
                "invalid_cli_json",
                "Bee CLI output root must be a JSON object",
                retryable=False,
            )
        return decoded

    async def health(self) -> AdapterHealth:
        if self._config is None:
            return AdapterHealth(
                status="unconfigured",
                adapter_kind="bee_cli",
                live_bee_verified=False,
                detail="Official Bee CLI executable and verified arguments are not configured.",
            )
        try:
            raw = await self._run_cli(self._config.health_args)
            payload = self._decode_json_object(raw)
        except BeeAdapterError as error:
            return AdapterHealth(
                status="offline",
                adapter_kind="bee_cli",
                live_bee_verified=False,
                detail=f"{error.code}:{error}",
            )
        ok = bool(payload.get("ok", True))
        return AdapterHealth(
            status="healthy" if ok else "degraded",
            adapter_kind="bee_cli",
            live_bee_verified=(self._config.provenance == "live" and ok),
            detail=redact_bridge_log(str(payload.get("detail", "ok"))),
        )

    async def list_recordings(self, cursor: str | None) -> RecordingPage:
        config = self._require_config()
        args = config.list_args
        if cursor is not None:
            if not cursor.strip() or len(cursor) > 256:
                raise BeeAdapterError("invalid_cursor", "Invalid Bee listing cursor")
            args = (*args, config.cursor_flag, cursor)
        raw = await self._run_cli(args)
        payload = self._decode_json_object(raw)
        return parse_recording_page_json(payload, default_provenance=config.provenance)

    async def get_transcript(self, reference: str) -> BeeTranscript:
        config = self._require_config()
        if not reference.strip() or len(reference) > 256:
            raise BeeAdapterError("invalid_reference", "Invalid Bee recording reference")
        args = (*config.transcript_args, config.reference_flag, reference.strip())
        raw = await self._run_cli(args)
        payload = self._decode_json_object(raw)
        return parse_transcript_json(payload, default_provenance=config.provenance)

    async def acknowledge(self, reference: str, cursor: str) -> None:
        config = self._require_config()
        if not reference.strip() or not cursor.strip():
            raise BeeAdapterError("invalid_acknowledge_input", "Reference and cursor required")
        if config.acknowledge_args:
            args = (
                *config.acknowledge_args,
                config.reference_flag,
                reference.strip(),
                config.cursor_flag,
                cursor.strip(),
            )
            await self._run_cli(args)
        self._acknowledged_cursors[reference.strip()] = cursor.strip()


class LocalSpoolBeeAdapter:
    """Local JSON spool implementation of BeeAdapter for offline CLI bridge workflows."""

    def __init__(self, spool_dir: Path, *, page_size: int = 10) -> None:
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be between 1 and 100")
        self._spool_dir = spool_dir
        self._page_size = page_size
        self._acknowledged: set[str] = set()
        self._last_cursor: str | None = None

    @property
    def last_cursor(self) -> str | None:
        return self._last_cursor

    @property
    def acknowledged_references(self) -> frozenset[str]:
        return frozenset(self._acknowledged)

    def _load_entries(self) -> list[tuple[str, dict[str, Any]]]:
        if not self._spool_dir.exists():
            return []
        entries: list[tuple[str, dict[str, Any]]] = []
        for path in sorted(self._spool_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise BeeAdapterError(
                    "corrupt_spool_file", f"Corrupt spool JSON {path.name}"
                ) from error
            if not isinstance(data, dict) or not isinstance(data.get("reference"), str):
                raise BeeAdapterError("invalid_spool_entry", f"Missing reference in {path.name}")
            ref = data["reference"].strip()
            if ref not in self._acknowledged:
                entries.append((ref, data))
        return entries

    async def health(self) -> AdapterHealth:
        if not self._spool_dir.exists():
            return AdapterHealth(
                status="offline",
                adapter_kind="local_spool",
                live_bee_verified=False,
                detail="Spool directory does not exist.",
            )
        return AdapterHealth(
            status="healthy",
            adapter_kind="local_spool",
            live_bee_verified=False,
            detail="Local fixture spool ready; no live Bee hardware claimed.",
        )

    async def list_recordings(self, cursor: str | None) -> RecordingPage:
        entries = self._load_entries()
        start_idx = 0
        if cursor is not None:
            for idx, (ref, _) in enumerate(entries):
                if ref > cursor:
                    start_idx = idx
                    break
            else:
                return RecordingPage(items=(), next_cursor=None)
        window = entries[start_idx : start_idx + self._page_size]
        summaries = tuple(
            BeeRecordingSummary(
                reference=ref,
                recorded_at=_parse_timestamp(raw.get("recorded_at"), "recorded_at"),
                device_type=_parse_device_type(raw.get("device_type", "bee_device")),
                provenance=_parse_provenance(raw.get("provenance"), "fixture"),
            )
            for ref, raw in window
        )
        next_cursor = summaries[-1].reference if window else cursor
        return RecordingPage(items=summaries, next_cursor=next_cursor)

    async def get_transcript(self, reference: str) -> BeeTranscript:
        for ref, raw in self._load_entries():
            if ref == reference.strip():
                return parse_transcript_json(raw, default_provenance="fixture")
        raise BeeAdapterError("transcript_not_found", f"Recording {reference} not found in spool")

    async def acknowledge(self, reference: str, cursor: str) -> None:
        if not reference.strip() or not cursor.strip():
            raise BeeAdapterError("invalid_acknowledge_input", "Reference and cursor required")
        self._acknowledged.add(reference.strip())
        self._last_cursor = cursor.strip()
