from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Literal

from .consent import ConsentError, require_processing_consent
from .models import Situation


class CaptureMode(str, Enum):
    CONSENTED_CONVERSATION = "consented_conversation"
    SELF_MEMO = "self_memo"


class Speaker(str, Enum):
    LEARNER = "learner"
    OTHER = "other"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TranscriptTurn:
    speaker: Speaker
    text: str
    start_ms: int
    end_ms: int
    confidence: float


@dataclass(frozen=True)
class SourceTranscript:
    reference: str
    learner_id: str
    recorded_at: datetime
    capture_mode: CaptureMode
    situation: Situation
    turns: tuple[TranscriptTurn, ...]
    consent_attested: bool
    all_participants_consented: bool
    revoked: bool
    retention_policy_present: bool
    provenance: Literal["fixture", "live"] = "fixture"


@dataclass(frozen=True)
class SelfExcerpt:
    text: str
    start_ms: int
    end_ms: int
    confidence: float


@dataclass(frozen=True)
class MinimizedTranscript:
    source_reference: str
    learner_id: str
    recorded_at: datetime
    situation: Situation
    capture_mode: CaptureMode
    self_excerpts: tuple[SelfExcerpt, ...]
    asr_confidence: float
    status: Literal["review_required"]
    reason_codes: tuple[str, ...]
    other_speaker_transcript_retained: Literal[False]
    transient_source_delete_after: datetime
    provenance: Literal["fixture", "live"]


_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\w)(?:\+\d[\d .()/-]{7,}\d)(?!\w)")
_LONG_NUMBER = re.compile(r"\b\d{6,}\b")


def redact_deterministic_identifiers(text: str) -> tuple[str, bool]:
    """Redact narrow patterns only; names/organizations still require learner review."""
    redacted = _EMAIL.sub("[redacted]", text)
    redacted = _PHONE.sub("[redacted]", redacted)
    redacted = _LONG_NUMBER.sub("[redacted]", redacted)
    return redacted, redacted != text


def _validate_source(source: SourceTranscript) -> None:
    require_processing_consent(
        consent_attested=source.consent_attested,
        revoked=source.revoked,
        retention_policy_present=source.retention_policy_present,
    )
    if (
        source.capture_mode is CaptureMode.CONSENTED_CONVERSATION
        and not source.all_participants_consented
    ):
        raise ConsentError("Every participant must consent to conversation processing")
    if not source.reference.strip() or not source.learner_id.strip():
        raise ValueError("Source reference and learner ID are required")
    if source.recorded_at.tzinfo is None or source.recorded_at.utcoffset() is None:
        raise ValueError("Recording timestamp must include a timezone")
    if not source.turns:
        raise ValueError("At least one transcript turn is required")
    if sum(len(turn.text) for turn in source.turns) > 1_000_000:
        raise ValueError("Transcript exceeds the bounded processing size")
    for turn in source.turns:
        if turn.start_ms < 0 or turn.end_ms <= turn.start_ms:
            raise ValueError("Transcript turn timing is invalid")
        if not 0.0 <= turn.confidence <= 1.0:
            raise ValueError("Transcript turn confidence must be between zero and one")
        if not turn.text.strip():
            raise ValueError("Transcript turns cannot be empty")


def minimize_transcript(
    source: SourceTranscript, *, minimized_at: datetime | None = None
) -> MinimizedTranscript:
    _validate_source(source)
    processed_at = minimized_at or datetime.now(UTC)
    if processed_at.tzinfo is None or processed_at.utcoffset() is None:
        raise ValueError("Minimization timestamp must include a timezone")
    reasons = {"self_excerpt_review_required"}
    excerpts: list[SelfExcerpt] = []
    for turn in source.turns:
        if turn.speaker is Speaker.UNKNOWN:
            reasons.add("ambiguous_speaker_excluded")
            continue
        if turn.speaker is Speaker.OTHER:
            if source.capture_mode is CaptureMode.SELF_MEMO:
                reasons.add("other_speaker_like_turn_excluded_from_self_memo")
            continue
        text, redacted = redact_deterministic_identifiers(turn.text)
        if redacted:
            reasons.add("deterministic_redaction_applied")
        excerpts.append(SelfExcerpt(text, turn.start_ms, turn.end_ms, turn.confidence))
    if not excerpts:
        raise ValueError("No unambiguous learner turns are available for minimization")
    if source.capture_mode is CaptureMode.SELF_MEMO:
        reasons.add("self_memo_requires_learner_review")
    confidence = sum(turn.confidence for turn in excerpts) / len(excerpts)
    if confidence < 0.75:
        reasons.add("asr_confidence_below_0_75")
    return MinimizedTranscript(
        source_reference=source.reference,
        learner_id=source.learner_id,
        recorded_at=source.recorded_at.astimezone(UTC),
        situation=source.situation,
        capture_mode=source.capture_mode,
        self_excerpts=tuple(excerpts),
        asr_confidence=confidence,
        status="review_required",
        reason_codes=tuple(sorted(reasons)),
        other_speaker_transcript_retained=False,
        transient_source_delete_after=processed_at.astimezone(UTC) + timedelta(hours=24),
        provenance=source.provenance,
    )


def _fingerprint(source: SourceTranscript) -> str:
    canonical = json.dumps(
        {
            "reference": source.reference,
            "learner_id": source.learner_id,
            "recorded_at": source.recorded_at.isoformat(),
            "capture_mode": source.capture_mode.value,
            "situation": source.situation.value,
            "turns": [
                [turn.speaker.value, turn.text, turn.start_ms, turn.end_ms, turn.confidence]
                for turn in source.turns
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class TranscriptMinimizationService:
    """In-memory idempotent minimization; durable ingestion and deletion are separate adapters."""

    def __init__(self) -> None:
        self._results: dict[tuple[str, str], MinimizedTranscript] = {}
        self._fingerprints: dict[tuple[str, str], str] = {}

    def minimize(
        self, source: SourceTranscript, *, minimized_at: datetime | None = None
    ) -> MinimizedTranscript:
        _validate_source(source)
        key = (source.learner_id, source.reference)
        fingerprint = _fingerprint(source)
        if key in self._results:
            if self._fingerprints[key] != fingerprint:
                raise ValueError("source_reference_reused_with_different_content")
            return self._results[key]
        result = minimize_transcript(source, minimized_at=minimized_at)
        self._results[key] = result
        self._fingerprints[key] = fingerprint
        return result

    def get(self, learner_id: str, reference: str) -> MinimizedTranscript:
        key = (learner_id, reference)
        if key not in self._results:
            raise KeyError("minimized_transcript_not_found")
        return self._results[key]
