from __future__ import annotations

import binascii
import json
import struct
from dataclasses import dataclass
from typing import Any

_HEADER_STRING_TYPE = 7


class EventStreamFormatError(ValueError):
    """Raised when an AWS vnd.amazon.eventstream binary frame fails framing or CRC checks."""


@dataclass(frozen=True)
class DecodedEventStreamFrame:
    headers: dict[str, str]
    payload: bytes


@dataclass(frozen=True)
class DecodedTranscriptSegment:
    transcript: str
    is_partial: bool
    confidence: float


def _encode_string_header(name: str, value: str) -> bytes:
    name_bytes = name.encode("utf-8")
    val_bytes = value.encode("utf-8")
    if not 1 <= len(name_bytes) <= 255:
        raise EventStreamFormatError("Invalid EventStream header name length")
    return (
        bytes([len(name_bytes)])
        + name_bytes
        + struct.pack("!BH", _HEADER_STRING_TYPE, len(val_bytes))
        + val_bytes
    )


def encode_eventstream_frame(*, headers: dict[str, str], payload: bytes) -> bytes:
    encoded_headers = b"".join(_encode_string_header(k, v) for k, v in headers.items())
    headers_len = len(encoded_headers)
    total_len = 12 + headers_len + len(payload) + 4
    prelude_without_crc = struct.pack("!II", total_len, headers_len)
    prelude_crc = binascii.crc32(prelude_without_crc) & 0xFFFFFFFF
    body_without_msg_crc = (
        prelude_without_crc + struct.pack("!I", prelude_crc) + encoded_headers + payload
    )
    msg_crc = binascii.crc32(body_without_msg_crc) & 0xFFFFFFFF
    return body_without_msg_crc + struct.pack("!I", msg_crc)


def encode_audio_event_frame(pcm_bytes: bytes) -> bytes:
    return encode_eventstream_frame(
        headers={
            ":message-type": "event",
            ":event-type": "AudioEvent",
            ":content-type": "application/octet-stream",
        },
        payload=pcm_bytes,
    )


def encode_transcript_event_frame(
    *,
    transcript: str,
    is_partial: bool,
    confidence: float,
) -> bytes:
    words = [w for w in transcript.split() if w]
    items = [
        {"Content": word, "Type": "pronunciation", "Confidence": round(confidence, 4)}
        for word in words
    ]
    body = {
        "Transcript": {
            "Results": [
                {
                    "ResultId": "res-1",
                    "IsPartial": is_partial,
                    "Alternatives": [
                        {
                            "Transcript": transcript,
                            "Items": items,
                        }
                    ],
                }
            ]
        }
    }
    return encode_eventstream_frame(
        headers={
            ":message-type": "event",
            ":event-type": "TranscriptEvent",
            ":content-type": "application/json",
        },
        payload=json.dumps(body, separators=(",", ":")).encode("utf-8"),
    )


def decode_eventstream_frame(frame: bytes) -> DecodedEventStreamFrame:
    if len(frame) < 16:
        raise EventStreamFormatError("EventStream frame shorter than 16-byte minimum")
    total_len, headers_len, prelude_crc = struct.unpack("!III", frame[:12])
    if total_len != len(frame):
        raise EventStreamFormatError("EventStream total_length mismatch")
    expected_prelude_crc = binascii.crc32(frame[:8]) & 0xFFFFFFFF
    if prelude_crc != expected_prelude_crc:
        raise EventStreamFormatError("EventStream prelude CRC32 mismatch")

    (msg_crc,) = struct.unpack("!I", frame[-4:])
    expected_msg_crc = binascii.crc32(frame[:-4]) & 0xFFFFFFFF
    if msg_crc != expected_msg_crc:
        raise EventStreamFormatError("EventStream message CRC32 mismatch")

    header_end = 12 + headers_len
    if header_end > len(frame) - 4:
        raise EventStreamFormatError("EventStream headers_length exceeds frame bounds")

    headers_buf = frame[12:header_end]
    offset = 0
    headers: dict[str, str] = {}
    while offset < len(headers_buf):
        name_len = headers_buf[offset]
        offset += 1
        if offset + name_len + 3 > len(headers_buf):
            raise EventStreamFormatError("Truncated EventStream header")
        name = headers_buf[offset : offset + name_len].decode("utf-8")
        offset += name_len
        val_type, val_len = struct.unpack("!BH", headers_buf[offset : offset + 3])
        offset += 3
        if val_type != _HEADER_STRING_TYPE or offset + val_len > len(headers_buf):
            raise EventStreamFormatError("Unsupported or truncated EventStream header value")
        val = headers_buf[offset : offset + val_len].decode("utf-8")
        offset += val_len
        headers[name] = val

    payload = frame[header_end:-4]
    return DecodedEventStreamFrame(headers=headers, payload=payload)


def parse_transcript_event_frame(frame: bytes) -> tuple[DecodedTranscriptSegment, ...]:
    decoded = decode_eventstream_frame(frame)
    msg_type = decoded.headers.get(":message-type")
    if msg_type == "exception":
        exc_type = decoded.headers.get(":exception-type", "UnknownException")
        raise EventStreamFormatError(f"Transcribe stream exception: {exc_type}")
    if decoded.headers.get(":event-type") != "TranscriptEvent":
        return ()

    try:
        data: dict[str, Any] = json.loads(decoded.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EventStreamFormatError("Invalid TranscriptEvent JSON payload") from error

    results = data.get("Transcript", {}).get("Results", [])
    if not isinstance(results, list):
        raise EventStreamFormatError("TranscriptEvent Results must be a list")

    segments: list[DecodedTranscriptSegment] = []
    for res in results:
        if not isinstance(res, dict):
            continue
        is_partial = bool(res.get("IsPartial", False))
        alts = res.get("Alternatives", [])
        if not isinstance(alts, list) or not alts or not isinstance(alts[0], dict):
            continue
        first_alt = alts[0]
        text = str(first_alt.get("Transcript") or "").strip()
        items = first_alt.get("Items", [])
        confs = [
            float(it["Confidence"])
            for it in items
            if isinstance(it, dict) and isinstance(it.get("Confidence"), int | float)
        ]
        mean_conf = round(sum(confs) / len(confs), 4) if confs else 0.0
        segments.append(
            DecodedTranscriptSegment(
                transcript=text,
                is_partial=is_partial,
                confidence=mean_conf,
            )
        )
    return tuple(segments)
