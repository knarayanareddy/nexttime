from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from .consent import ConsentError, require_processing_consent
from .models import Situation
from .transcripts import (
    CaptureMode,
    MinimizedTranscript,
    SourceTranscript,
    Speaker,
    TranscriptTurn,
    minimize_transcript,
)


class SeparationMethod(StrEnum):
    BEE_SPEAKER_LABELS = "bee_speaker_labels"
    AWS_DIARIZATION = "aws_diarization"
    LEARNER_LABELED = "learner_labeled"
    SELF_ONLY = "self_only"


@dataclass(frozen=True)
class RawSpeakerSegment:
    segment_id: str
    raw_speaker_label: str | None
    text: str
    start_ms: int
    end_ms: int
    asr_confidence: float
    speaker_confidence: float = 1.0
    overlapping_speech: bool = False


@dataclass(frozen=True)
class LearnerTurnLabel:
    segment_id: str
    assigned_speaker: Literal["learner", "other", "exclude"]


@dataclass(frozen=True)
class SpeakerSeparationPolicy:
    min_speaker_confidence: float = 0.80
    learner_labels: frozenset[str] = frozenset({"learner", "self", "user", "wearer", "primary"})
    other_labels: frozenset[str] = frozenset(
        {"other", "counterpart", "guest", "interlocutor", "bystander", "agent"}
    )
    enrolled_diarization_speaker_id: str | None = None


@dataclass(frozen=True)
class SeparatedTurnAudit:
    segment_id: str
    resolved_speaker: Speaker
    reason_code: str
    review_required: bool


@dataclass(frozen=True)
class SpeakerSeparationResult:
    diarization_method: SeparationMethod
    turns: tuple[TranscriptTurn, ...]
    learner_turn_count: int
    excluded_other_turn_count: int
    ambiguous_turn_count: int
    diarization_ambiguous: bool
    review_required: bool
    reason_codes: tuple[str, ...]
    segment_audits: tuple[SeparatedTurnAudit, ...]


def _valid_unit_float(value: float) -> bool:
    return math.isfinite(value) and 0.0 <= value <= 1.0


def _normalized_label(label: str | None) -> str | None:
    if label is None:
        return None
    cleaned = label.strip().lower()
    return cleaned if cleaned else None


def _find_temporal_overlaps(segments: tuple[RawSpeakerSegment, ...]) -> set[str]:
    overlapping_ids: set[str] = set()
    for i, first in enumerate(segments):
        first_label = _normalized_label(first.raw_speaker_label)
        for second in segments[i + 1 :]:
            if first.start_ms < second.end_ms and second.start_ms < first.end_ms:
                second_label = _normalized_label(second.raw_speaker_label)
                if first_label is None or second_label is None or first_label != second_label:
                    overlapping_ids.add(first.segment_id)
                    overlapping_ids.add(second.segment_id)
    return overlapping_ids


def _validate_segments_and_policy(
    segments: tuple[RawSpeakerSegment, ...],
    *,
    capture_mode: CaptureMode,
    method: SeparationMethod,
    policy: SpeakerSeparationPolicy,
    learner_turn_labels: tuple[LearnerTurnLabel, ...],
) -> dict[str, Literal["learner", "other", "exclude"]]:
    if not _valid_unit_float(policy.min_speaker_confidence):
        raise ValueError("Speaker separation confidence threshold must be between zero and one")
    if capture_mode is CaptureMode.SELF_MEMO and method not in {
        SeparationMethod.SELF_ONLY,
        SeparationMethod.LEARNER_LABELED,
    }:
        raise ValueError("self_memo_rejects_conversation_diarization")
    if capture_mode is CaptureMode.CONSENTED_CONVERSATION and method is SeparationMethod.SELF_ONLY:
        raise ValueError("consented_conversation_rejects_self_only")
    if not segments:
        raise ValueError("At least one raw speaker segment is required")

    seen_ids: set[str] = set()
    for seg in segments:
        seg_id = seg.segment_id.strip()
        if not seg_id:
            raise ValueError("Segment ID cannot be empty")
        if seg_id in seen_ids:
            raise ValueError("Duplicate segment_id in raw speaker segments")
        seen_ids.add(seg_id)
        if not seg.text.strip():
            raise ValueError("Raw speaker segment text cannot be empty")
        if seg.start_ms < 0 or seg.end_ms <= seg.start_ms:
            raise ValueError("Raw speaker segment timing is invalid")
        if not _valid_unit_float(seg.asr_confidence) or not _valid_unit_float(
            seg.speaker_confidence
        ):
            raise ValueError("Segment confidences must be between zero and one")

    if learner_turn_labels and method is not SeparationMethod.LEARNER_LABELED:
        raise ValueError("Learner turn labels require learner_labeled separation method")

    label_map: dict[str, Literal["learner", "other", "exclude"]] = {}
    for entry in learner_turn_labels:
        target_id = entry.segment_id.strip()
        if target_id not in seen_ids:
            raise ValueError("Learner turn label references unknown segment_id")
        if target_id in label_map:
            raise ValueError("Duplicate learner turn label for segment_id")
        if entry.assigned_speaker not in {"learner", "other", "exclude"}:
            raise ValueError("Invalid learner turn label assignment")
        label_map[target_id] = entry.assigned_speaker
    return label_map


def _classify_segment(
    segment: RawSpeakerSegment,
    *,
    method: SeparationMethod,
    policy: SpeakerSeparationPolicy,
    temporal_overlap: bool,
    label_map: dict[str, Literal["learner", "other", "exclude"]],
) -> tuple[Speaker, str, bool]:
    norm_label = _normalized_label(segment.raw_speaker_label)
    has_overlap = segment.overlapping_speech or temporal_overlap
    low_conf = segment.speaker_confidence < policy.min_speaker_confidence

    if method is SeparationMethod.LEARNER_LABELED:
        assigned = label_map.get(segment.segment_id.strip())
        if assigned == "learner":
            return Speaker.LEARNER, "learner_confirmed_turn", False
        if assigned in {"other", "exclude"}:
            return Speaker.OTHER, "learner_excluded_turn", False
        return Speaker.UNKNOWN, "unlabeled_turn_requires_review", True

    if method is SeparationMethod.SELF_ONLY:
        if norm_label is not None and norm_label in policy.other_labels:
            return Speaker.OTHER, "other_speaker_label_in_self_memo", True
        if (
            has_overlap
            or low_conf
            or (norm_label is not None and norm_label not in policy.learner_labels)
        ):
            return Speaker.UNKNOWN, "ambiguous_self_memo_segment", True
        return Speaker.LEARNER, "self_only_verified", False

    if method is SeparationMethod.BEE_SPEAKER_LABELS:
        if has_overlap:
            return Speaker.UNKNOWN, "overlapping_speaker_segments", True
        if low_conf or norm_label is None:
            return Speaker.UNKNOWN, "unreliable_bee_speaker_label", True
        if norm_label in policy.learner_labels:
            return Speaker.LEARNER, "bee_learner_label", False
        if norm_label in policy.other_labels:
            return Speaker.OTHER, "bee_other_label", False
        return Speaker.UNKNOWN, "unrecognized_bee_speaker_label", True

    # AWS_DIARIZATION
    enrolled = _normalized_label(policy.enrolled_diarization_speaker_id)
    if enrolled is None:
        return Speaker.UNKNOWN, "unanchored_diarization_cluster", True
    if has_overlap or low_conf or norm_label is None:
        return Speaker.UNKNOWN, "ambiguous_diarization_segment", True
    if norm_label == enrolled:
        return Speaker.LEARNER, "diarization_enrolled_learner", False
    return Speaker.OTHER, "diarization_other_cluster", False


_DEFAULT_SEPARATION_POLICY = SpeakerSeparationPolicy()


def separate_speaker_turns(
    segments: tuple[RawSpeakerSegment, ...],
    *,
    capture_mode: CaptureMode,
    method: SeparationMethod,
    policy: SpeakerSeparationPolicy = _DEFAULT_SEPARATION_POLICY,
    learner_turn_labels: tuple[LearnerTurnLabel, ...] = (),
) -> SpeakerSeparationResult:
    label_map = _validate_segments_and_policy(
        segments,
        capture_mode=capture_mode,
        method=method,
        policy=policy,
        learner_turn_labels=learner_turn_labels,
    )
    temporal_overlaps = _find_temporal_overlaps(segments)

    turns: list[TranscriptTurn] = []
    audits: list[SeparatedTurnAudit] = []
    reasons: set[str] = set()
    learner_count = 0
    other_count = 0
    ambiguous_count = 0

    for seg in segments:
        seg_id = seg.segment_id.strip()
        speaker, reason, seg_review = _classify_segment(
            seg,
            method=method,
            policy=policy,
            temporal_overlap=(seg_id in temporal_overlaps),
            label_map=label_map,
        )
        if speaker is Speaker.LEARNER:
            learner_count += 1
        elif speaker is Speaker.OTHER:
            other_count += 1
        else:
            ambiguous_count += 1
            reasons.add("ambiguous_turn_requires_review")
        if seg_review:
            reasons.add(reason)

        turns.append(
            TranscriptTurn(
                speaker=speaker,
                text=seg.text,
                start_ms=seg.start_ms,
                end_ms=seg.end_ms,
                confidence=seg.asr_confidence,
            )
        )
        audits.append(
            SeparatedTurnAudit(
                segment_id=seg_id,
                resolved_speaker=speaker,
                reason_code=reason,
                review_required=seg_review,
            )
        )

    if learner_count == 0:
        reasons.add("no_unambiguous_learner_turns")
    if capture_mode is CaptureMode.SELF_MEMO:
        reasons.add("self_memo_requires_learner_review")

    diarization_ambiguous = ambiguous_count > 0
    review_required = (
        diarization_ambiguous
        or learner_count == 0
        or capture_mode is CaptureMode.SELF_MEMO
        or any(item.review_required for item in audits)
    )

    return SpeakerSeparationResult(
        diarization_method=method,
        turns=tuple(turns),
        learner_turn_count=learner_count,
        excluded_other_turn_count=other_count,
        ambiguous_turn_count=ambiguous_count,
        diarization_ambiguous=diarization_ambiguous,
        review_required=review_required,
        reason_codes=tuple(sorted(reasons)),
        segment_audits=tuple(audits),
    )


def separate_and_minimize_transcript(
    *,
    reference: str,
    learner_id: str,
    recorded_at: datetime,
    capture_mode: CaptureMode,
    situation: Situation,
    segments: tuple[RawSpeakerSegment, ...],
    method: SeparationMethod,
    consent_attested: bool,
    all_participants_consented: bool,
    revoked: bool,
    retention_policy_present: bool,
    provenance: Literal["fixture", "live"] = "fixture",
    policy: SpeakerSeparationPolicy = _DEFAULT_SEPARATION_POLICY,
    learner_turn_labels: tuple[LearnerTurnLabel, ...] = (),
    minimized_at: datetime | None = None,
) -> tuple[SpeakerSeparationResult, MinimizedTranscript]:
    require_processing_consent(
        consent_attested=consent_attested,
        revoked=revoked,
        retention_policy_present=retention_policy_present,
    )
    if capture_mode is CaptureMode.CONSENTED_CONVERSATION and not all_participants_consented:
        raise ConsentError("Every participant must consent to conversation processing")

    separation = separate_speaker_turns(
        segments,
        capture_mode=capture_mode,
        method=method,
        policy=policy,
        learner_turn_labels=learner_turn_labels,
    )
    source = SourceTranscript(
        reference=reference,
        learner_id=learner_id,
        recorded_at=recorded_at,
        capture_mode=capture_mode,
        situation=situation,
        turns=separation.turns,
        consent_attested=consent_attested,
        all_participants_consented=all_participants_consented,
        revoked=revoked,
        retention_policy_present=retention_policy_present,
        provenance=provenance,
    )
    minimized = minimize_transcript(source, minimized_at=minimized_at)
    return separation, minimized
