from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from nexttime_domain.speaker_separation import RawSpeakerSegment

DeviceKind = Literal["bee_device", "apple_watch_bee"]
ProvenanceKind = Literal["live", "fixture"]
AdapterStatus = Literal["healthy", "degraded", "unconfigured", "offline"]

_SECRET_PATTERN = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/-]+=*|(?:token|secret|password|api[_-]?key)\s*[:=]\s*\S+)"
)
_UTTERANCE_JSON_PATTERN = re.compile(
    r'("(?:text|transcript|self_evidence_excerpt)"\s*:\s*)"(?:\\.|[^"\\])*"',
    re.IGNORECASE,
)
_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+\d[\d .()/-]{7,}\d)(?!\w)")


def redact_bridge_log(message: str, *, max_length: int = 240) -> str:
    redacted = _SECRET_PATTERN.sub("[redacted-secret]", message)
    redacted = _UTTERANCE_JSON_PATTERN.sub(r'\1"[redacted-utterance]"', redacted)
    redacted = _EMAIL_PATTERN.sub("[redacted-email]", redacted)
    redacted = _PHONE_PATTERN.sub("[redacted-phone]", redacted)
    compact = " ".join(redacted.split())
    if len(compact) > max_length:
        return f"{compact[:max_length]}...[truncated]"
    return compact


class BeeAdapterError(RuntimeError):
    """Typed error raised by Bee CLI or local spool adapters."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(redact_bridge_log(message))
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class AdapterHealth:
    status: AdapterStatus
    adapter_kind: Literal["bee_cli", "local_spool"]
    live_bee_verified: bool
    detail: str


@dataclass(frozen=True)
class BeeRecordingSummary:
    reference: str
    recorded_at: datetime
    device_type: DeviceKind
    provenance: ProvenanceKind = "fixture"


@dataclass(frozen=True)
class RecordingPage:
    items: tuple[BeeRecordingSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class BeeTranscript:
    reference: str
    recorded_at: datetime
    device_type: DeviceKind
    segments: tuple[RawSpeakerSegment, ...]
    provenance: ProvenanceKind = "fixture"


class BeeAdapter(Protocol):
    async def health(self) -> AdapterHealth: ...
    async def list_recordings(self, cursor: str | None) -> RecordingPage: ...
    async def get_transcript(self, reference: str) -> BeeTranscript: ...
    async def acknowledge(self, reference: str, cursor: str) -> None: ...
