from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Literal, Protocol

from .consent import ConsentError, require_processing_consent
from .minimization import find_prohibited_transcript_fields, validate_minimized_attempt
from .models import (
    AttemptEvaluationInput,
    CriterionEvidence,
    CriterionResult,
    OutcomeEvaluation,
    Rubric,
    Situation,
)
from .rubric import evaluate_authentic_attempt
from .transcripts import (
    CaptureMode,
    MinimizedTranscript,
    SelfExcerpt,
    redact_deterministic_identifiers,
)

MAX_PROPOSED_BREAKDOWNS: Final[int] = 3
BREAKDOWN_REVIEW_CONFIDENCE_THRESHOLD: Final[float] = 0.85
OUTCOME_CONFIDENCE_THRESHOLD: Final[float] = 0.75

_CROCKFORD_BASE32: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_POLICY_VERSION_PATTERN = re.compile(r"^consent_v[0-9]+$")
_CONTROL_OR_MARKUP_PATTERN = re.compile(r"[\x00-\x1f\x7f]|[<>]")

_PROHIBITED_INFERENCE_PATTERN = re.compile(
    r"\b(?:"
    r"native[- ]like|native[- ]speaker|accent|fluency\s+score|cefr|streak|"
    r"emotion(?:al)?|anxious|nervous|angry|personality|intelligent|intelligence|iq|"
    r"ethnic(?:ity)?|health\s+status|employment\s+status"
    r")\b",
    re.IGNORECASE,
)

_SENSITIVE_CONTENT_PATTERN = re.compile(
    r"\b(?:"
    r"diagnos(?:e|is)|recept|prescription|arts|dokter|ziekenhuis|medical\s+emergency|"
    r"advocaat|rechtbank|lawsuit|arrestatie|immigration|visum|verblijfsvergunning|asiel|asylum|"
    r"ontslag\s+op\s+staande\s+voet|disciplinary\s+hearing|"
    r"schuldsanering|hypotheek|bankrekening|iban|debt\s+collection|"
    r"zelfmoord|suicid(?:e|al)|crisisdienst"
    r")\b",
    re.IGNORECASE,
)


class BreakdownValidationError(ValueError):
    """Raised when proposed breakdowns or criterion candidates violate domain invariants."""


class SensitiveContentError(ValueError):
    """Raised when a transcript falls into an excluded sensitive conversation category."""


class BreakdownCode(StrEnum):
    CONFIRMATION_MISSING = "confirmation_missing"
    COMPREHENSION_REPAIR = "comprehension_repair"
    TIME_EXPRESSION = "time_expression"
    POLITE_REQUEST = "polite_request"
    CLOSURE = "closure"
    UNKNOWN = "unknown"


DeviceType = Literal["bee_device", "apple_watch_bee"]
DiarizationMethod = Literal["bee_speaker_labels", "aws_diarization", "learner_labeled", "self_only"]


@dataclass(frozen=True)
class RawCriterionCandidate:
    criterion_id: str
    result: CriterionResult
    excerpt_index: int | None
    self_evidence_excerpt: str
    model_confidence: float


@dataclass(frozen=True)
class RawBreakdownCandidate:
    code: BreakdownCode
    description: str
    excerpt_index: int
    self_evidence_excerpt: str
    model_confidence: float


@dataclass(frozen=True)
class RawExtractionPayload:
    criteria: tuple[RawCriterionCandidate, ...]
    breakdowns: tuple[RawBreakdownCandidate, ...]
    model_version: str
    fallback_used: bool = False
    fallback_reason: str | None = None


class BreakdownExtractorPort(Protocol):
    def extract(
        self,
        minimized: MinimizedTranscript,
        rubric: Rubric,
        *,
        correlation_id: str,
    ) -> RawExtractionPayload: ...


@dataclass(frozen=True)
class CriterionCandidate:
    criterion_id: str
    result: CriterionResult
    self_evidence_excerpt: str
    self_evidence_start_ms: int | None = None
    self_evidence_end_ms: int | None = None
    asr_confidence: float = 0.0
    model_confidence: float = 0.0
    confidence: float = 0.0


@dataclass(frozen=True)
class ProposedBreakdown:
    breakdown_id: str
    code: BreakdownCode
    description: str
    self_evidence_excerpt: str
    self_evidence_start_ms: int
    self_evidence_end_ms: int
    asr_confidence: float
    model_confidence: float
    confidence: float
    review_status: Literal["proposed"] = "proposed"
    requires_learner_review: bool = True


@dataclass(frozen=True)
class BreakdownProposalResult:
    attempt_id: str
    learner_id: str
    source_reference: str
    situation: Situation
    capture_mode: CaptureMode
    criteria: tuple[CriterionCandidate, ...]
    outcome: OutcomeEvaluation
    breakdowns: tuple[ProposedBreakdown, ...]
    asr_confidence: float
    model_version: str
    fallback_used: bool
    fallback_reason: str | None
    learner_review_required: bool
    review_reason_codes: tuple[str, ...]
    attempt_document: dict[str, Any]
    duplicate: bool = False


def _validate_probability(value: float, field: str) -> float:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise BreakdownValidationError(f"{field}_out_of_bounds")
    return value


def combine_breakdown_confidence(*, asr_confidence: float, model_confidence: float) -> float:
    """Conservatively bound breakdown confidence by both ASR and model confidence."""
    asr = _validate_probability(asr_confidence, "asr_confidence")
    model = _validate_probability(model_confidence, "model_confidence")
    return round(min(asr, model), 4)


def _encode_crockford(value: int, length: int) -> str:
    chars = ["0"] * length
    remaining = value
    for idx in range(length - 1, -1, -1):
        chars[idx] = _CROCKFORD_BASE32[remaining & 0x1F]
        remaining >>= 5
    return "".join(chars)


def deterministic_ulid(*, namespace: str, seed: str, timestamp: datetime) -> str:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("ULID timestamp must include a timezone")
    epoch_ms = max(0, int(timestamp.astimezone(UTC).timestamp() * 1000)) & ((1 << 48) - 1)
    digest = hashlib.sha256(f"{namespace}:{seed}".encode()).digest()
    entropy = int.from_bytes(digest[:10], "big")
    return f"{_encode_crockford(epoch_ms, 10)}{_encode_crockford(entropy, 16)}"


def _require_ulid(value: str, field: str) -> str:
    if not _ULID_PATTERN.match(value):
        raise ValueError(f"Invalid {field} ULID")
    return value


def _iso_millis(value: datetime, field: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def ensure_non_sensitive_excerpts(excerpts: tuple[SelfExcerpt, ...]) -> None:
    for excerpt in excerpts:
        if _SENSITIVE_CONTENT_PATTERN.search(excerpt.text):
            raise SensitiveContentError("sensitive_situation_content_excluded")


def _validate_minimized_input(minimized: MinimizedTranscript) -> None:
    if minimized.other_speaker_transcript_retained is not False:
        raise BreakdownValidationError("other_speaker_transcript_retained")
    if not minimized.self_excerpts:
        raise BreakdownValidationError("missing_self_excerpts")
    _validate_probability(minimized.asr_confidence, "asr_confidence")
    for excerpt in minimized.self_excerpts:
        _validate_probability(excerpt.confidence, "excerpt_confidence")
        if excerpt.start_ms < 0 or excerpt.end_ms <= excerpt.start_ms or not excerpt.text.strip():
            raise BreakdownValidationError("invalid_self_excerpt")
    ensure_non_sensitive_excerpts(minimized.self_excerpts)


def _verify_owned_excerpt(
    candidate_excerpt: str,
    owner: SelfExcerpt,
) -> str:
    redacted_owner, _ = redact_deterministic_identifiers(owner.text)
    redacted_candidate, _ = redact_deterministic_identifiers(candidate_excerpt)
    cleaned_candidate = _normalize_whitespace(redacted_candidate)
    cleaned_owner = _normalize_whitespace(redacted_owner)
    if not cleaned_candidate or len(cleaned_candidate) > 500:
        raise BreakdownValidationError("excerpt_length_out_of_bounds")
    if _CONTROL_OR_MARKUP_PATTERN.search(cleaned_candidate):
        raise BreakdownValidationError("unsafe_excerpt_characters")
    if cleaned_candidate not in cleaned_owner:
        raise BreakdownValidationError("unowned_self_evidence_excerpt")
    return cleaned_candidate


def _validate_description(description: str) -> str:
    cleaned = _normalize_whitespace(description)
    if not 1 <= len(cleaned) <= 300:
        raise BreakdownValidationError("description_length_out_of_bounds")
    if _CONTROL_OR_MARKUP_PATTERN.search(cleaned):
        raise BreakdownValidationError("unsafe_description_characters")
    if _PROHIBITED_INFERENCE_PATTERN.search(cleaned):
        raise BreakdownValidationError("prohibited_inference_or_copy")
    return cleaned


def validate_raw_extraction(
    minimized: MinimizedTranscript,
    rubric: Rubric,
    raw: RawExtractionPayload,
    *,
    timestamp: datetime,
) -> tuple[tuple[CriterionCandidate, ...], tuple[ProposedBreakdown, ...]]:
    _validate_minimized_input(minimized)
    model_version = raw.model_version.strip()
    if not 1 <= len(model_version) <= 120:
        raise BreakdownValidationError("invalid_model_version")
    if len(raw.breakdowns) > MAX_PROPOSED_BREAKDOWNS:
        raise BreakdownValidationError("too_many_breakdowns")

    seen_codes: set[BreakdownCode] = set()
    proposed_breakdowns: list[ProposedBreakdown] = []
    for index, item in enumerate(raw.breakdowns):
        if item.code in seen_codes:
            raise BreakdownValidationError("duplicate_breakdown_code")
        seen_codes.add(item.code)
        if not 0 <= item.excerpt_index < len(minimized.self_excerpts):
            raise BreakdownValidationError("invalid_excerpt_index")
        excerpt = minimized.self_excerpts[item.excerpt_index]
        verified_excerpt = _verify_owned_excerpt(item.self_evidence_excerpt, excerpt)
        cleaned_description = _validate_description(item.description)
        combined_confidence = combine_breakdown_confidence(
            asr_confidence=excerpt.confidence,
            model_confidence=item.model_confidence,
        )
        requires_review = (
            combined_confidence < BREAKDOWN_REVIEW_CONFIDENCE_THRESHOLD
            or minimized.capture_mode is CaptureMode.SELF_MEMO
            or minimized.asr_confidence < OUTCOME_CONFIDENCE_THRESHOLD
            or item.code is BreakdownCode.UNKNOWN
        )
        breakdown_id = deterministic_ulid(
            namespace="breakdown",
            seed=f"{minimized.learner_id}:{minimized.source_reference}:{index}:{item.code.value}",
            timestamp=timestamp,
        )
        proposed_breakdowns.append(
            ProposedBreakdown(
                breakdown_id=breakdown_id,
                code=item.code,
                description=cleaned_description,
                self_evidence_excerpt=verified_excerpt,
                self_evidence_start_ms=excerpt.start_ms,
                self_evidence_end_ms=excerpt.end_ms,
                asr_confidence=excerpt.confidence,
                model_confidence=item.model_confidence,
                confidence=combined_confidence,
                requires_learner_review=requires_review,
            )
        )

    required_ids = rubric.required[minimized.situation]
    allowed_ids = set(required_ids) | set(rubric.optional[minimized.situation])
    seen_criteria: dict[str, CriterionCandidate] = {}
    for candidate in raw.criteria:
        if candidate.criterion_id not in allowed_ids:
            raise BreakdownValidationError("unsupported_criterion_id")
        if candidate.criterion_id in seen_criteria:
            raise BreakdownValidationError("duplicate_criterion_id")
        model_conf = _validate_probability(candidate.model_confidence, "criterion_model_confidence")
        if candidate.result is CriterionResult.UNKNOWN:
            seen_criteria[candidate.criterion_id] = CriterionCandidate(
                criterion_id=candidate.criterion_id,
                result=CriterionResult.UNKNOWN,
                self_evidence_excerpt="",
                asr_confidence=minimized.asr_confidence,
                model_confidence=model_conf,
                confidence=combine_breakdown_confidence(
                    asr_confidence=minimized.asr_confidence,
                    model_confidence=model_conf,
                ),
            )
            continue
        if candidate.excerpt_index is None or not (
            0 <= candidate.excerpt_index < len(minimized.self_excerpts)
        ):
            raise BreakdownValidationError("criterion_requires_valid_excerpt_index")
        owner = minimized.self_excerpts[candidate.excerpt_index]
        verified_excerpt = _verify_owned_excerpt(candidate.self_evidence_excerpt, owner)
        combined = combine_breakdown_confidence(
            asr_confidence=owner.confidence,
            model_confidence=model_conf,
        )
        if combined < OUTCOME_CONFIDENCE_THRESHOLD:
            seen_criteria[candidate.criterion_id] = CriterionCandidate(
                criterion_id=candidate.criterion_id,
                result=CriterionResult.UNKNOWN,
                self_evidence_excerpt="",
                asr_confidence=owner.confidence,
                model_confidence=model_conf,
                confidence=combined,
            )
        else:
            seen_criteria[candidate.criterion_id] = CriterionCandidate(
                criterion_id=candidate.criterion_id,
                result=candidate.result,
                self_evidence_excerpt=verified_excerpt,
                self_evidence_start_ms=owner.start_ms,
                self_evidence_end_ms=owner.end_ms,
                asr_confidence=owner.confidence,
                model_confidence=model_conf,
                confidence=combined,
            )

    ordered_criteria: list[CriterionCandidate] = []
    for criterion_id in required_ids:
        ordered_criteria.append(
            seen_criteria.pop(
                criterion_id,
                CriterionCandidate(
                    criterion_id=criterion_id,
                    result=CriterionResult.UNKNOWN,
                    self_evidence_excerpt="",
                    asr_confidence=minimized.asr_confidence,
                    model_confidence=0.0,
                    confidence=0.0,
                ),
            )
        )
    for criterion_id in rubric.optional[minimized.situation]:
        if criterion_id in seen_criteria:
            ordered_criteria.append(seen_criteria.pop(criterion_id))

    confirmation_ids = {"appointment_confirmed", "next_step_confirmed"}
    if BreakdownCode.CONFIRMATION_MISSING in seen_codes and any(
        item.criterion_id in confirmation_ids and item.result is CriterionResult.SATISFIED
        for item in ordered_criteria
    ):
        raise BreakdownValidationError("breakdown_contradicts_satisfied_criterion")

    return tuple(ordered_criteria), tuple(proposed_breakdowns)


_GOAL_PATTERNS: dict[Situation, re.Pattern[str]] = {
    Situation.REPAIR: re.compile(
        r"\b(?:afspraak|reparatie|loodgieter|monteur|kapot|verwarming|lekkage)\b",
        re.IGNORECASE,
    ),
    Situation.SCHOOL_ADMIN: re.compile(
        r"\b(?:afspraak|schooladministratie|school|inschrijving|formulier|administratie)\b",
        re.IGNORECASE,
    ),
    Situation.SHIFT: re.compile(
        r"\b(?:dienst|rooster|shift|volgende\s+stap|controleren|werktijd)\b",
        re.IGNORECASE,
    ),
}

_CONFIRMATION_PATTERNS: dict[Situation, re.Pattern[str]] = {
    Situation.REPAIR: re.compile(
        r"\b(?:ik\s+bevestig|afspraak\s+is\s+om|datum\s+en\s+de\s+tijd|"
        r"(?:maandag|dinsdag|woensdag|donderdag|vrijdag)\s+om|"
        r"dan\s+zie\s+ik\s+u\s+om)\b",
        re.IGNORECASE,
    ),
    Situation.SCHOOL_ADMIN: re.compile(
        r"\b(?:ik\s+bevestig|afspraak\s+is\s+om|datum\s+en\s+de\s+tijd|"
        r"(?:maandag|dinsdag|woensdag|donderdag|vrijdag)\s+om|"
        r"ik\s+kom\s+om)\b",
        re.IGNORECASE,
    ),
    Situation.SHIFT: re.compile(
        r"\b(?:ik\s+bevestig|volgende\s+stap\s+is|volgende\s+dienst\s+begint|"
        r"(?:maandag|dinsdag|woensdag|donderdag|vrijdag)\s+om|"
        r"duidelijk,\s+ik\s+begin\s+om)\b",
        re.IGNORECASE,
    ),
}

_REPAIR_SATISFIED_PATTERN = re.compile(
    r"\b(?:kunt\s+u\s+dat\s+herhalen|kunt\s+u\s+langzamer\s+spreken|wat\s+bedoelt\s+u\s+met)\b",
    re.IGNORECASE,
)
_COMPREHENSION_GAP_PATTERN = re.compile(
    r"\b(?:ik\s+snap\s+het\s+niet|ik\s+begrijp\s+het\s+niet|sorry\s+english|"
    r"can\s+we\s+speak\s+english|i\s+don't\s+understand)\b",
    re.IGNORECASE,
)
_TIME_HESITATION_PATTERN = re.compile(
    r"\b(?:uhm?\s+hoe\s+laat|welke\s+dag\s+ook\s+alweer|misschien\s+ergens\s+in\s+de\s+middag)\b",
    re.IGNORECASE,
)
_ABRUPT_CLOSURE_PATTERN = re.compile(
    r"\b(?:laat\s+maar\s+zitten|ik\s+moet\s+nu\s+ophangen|vergeet\s+het\s+maar)\b",
    re.IGNORECASE,
)


def _match_excerpt(
    excerpts: tuple[SelfExcerpt, ...], pattern: re.Pattern[str]
) -> tuple[int, SelfExcerpt] | None:
    for idx, excerpt in enumerate(excerpts):
        if pattern.search(excerpt.text):
            return idx, excerpt
    return None


class DeterministicBreakdownExtractor:
    """Authoritative deterministic fallback when Bedrock/model gateway is unavailable."""

    model_version: str = "deterministic-breakdown-v1"

    def extract(
        self,
        minimized: MinimizedTranscript,
        rubric: Rubric,
        *,
        correlation_id: str,
    ) -> RawExtractionPayload:
        del correlation_id
        _validate_minimized_input(minimized)
        excerpts = minimized.self_excerpts
        situation = minimized.situation
        confirmation_id = (
            "next_step_confirmed" if situation is Situation.SHIFT else "appointment_confirmed"
        )

        criteria: list[RawCriterionCandidate] = []
        goal_match = _match_excerpt(excerpts, _GOAL_PATTERNS[situation])
        if goal_match is not None:
            goal_idx, goal_excerpt = goal_match
            criteria.append(
                RawCriterionCandidate(
                    criterion_id="goal_stated",
                    result=CriterionResult.SATISFIED,
                    excerpt_index=goal_idx,
                    self_evidence_excerpt=goal_excerpt.text,
                    model_confidence=0.88,
                )
            )
        else:
            criteria.append(
                RawCriterionCandidate(
                    criterion_id="goal_stated",
                    result=CriterionResult.UNKNOWN,
                    excerpt_index=None,
                    self_evidence_excerpt="",
                    model_confidence=0.60,
                )
            )

        confirm_match = _match_excerpt(excerpts, _CONFIRMATION_PATTERNS[situation])
        if confirm_match is not None:
            conf_idx, conf_excerpt = confirm_match
            criteria.append(
                RawCriterionCandidate(
                    criterion_id=confirmation_id,
                    result=CriterionResult.SATISFIED,
                    excerpt_index=conf_idx,
                    self_evidence_excerpt=conf_excerpt.text,
                    model_confidence=0.89,
                )
            )
        else:
            criteria.append(
                RawCriterionCandidate(
                    criterion_id=confirmation_id,
                    result=CriterionResult.UNKNOWN,
                    excerpt_index=None,
                    self_evidence_excerpt="",
                    model_confidence=0.60,
                )
            )

        repair_match = _match_excerpt(excerpts, _REPAIR_SATISFIED_PATTERN)
        if repair_match is not None and "repair_requested" in rubric.optional[situation]:
            rep_idx, rep_excerpt = repair_match
            criteria.append(
                RawCriterionCandidate(
                    criterion_id="repair_requested",
                    result=CriterionResult.SATISFIED,
                    excerpt_index=rep_idx,
                    self_evidence_excerpt=rep_excerpt.text,
                    model_confidence=0.86,
                )
            )

        candidates: list[RawBreakdownCandidate] = []
        comprehension_gap = _match_excerpt(excerpts, _COMPREHENSION_GAP_PATTERN)
        if comprehension_gap is not None and repair_match is None:
            comp_idx, comp_excerpt = comprehension_gap
            candidates.append(
                RawBreakdownCandidate(
                    code=BreakdownCode.COMPREHENSION_REPAIR,
                    description=(
                        "A misunderstanding or language switch occurred without a Dutch "
                        "clarification request."
                    ),
                    excerpt_index=comp_idx,
                    self_evidence_excerpt=comp_excerpt.text,
                    model_confidence=0.84,
                )
            )

        if confirm_match is None:
            last_idx = len(excerpts) - 1
            last_excerpt = excerpts[last_idx]
            confirm_description = (
                "The attempt ended without confirming the agreed next step."
                if situation is Situation.SHIFT
                else "The attempt ended without an explicit confirmation of the proposed time."
            )
            candidates.append(
                RawBreakdownCandidate(
                    code=BreakdownCode.CONFIRMATION_MISSING,
                    description=confirm_description,
                    excerpt_index=last_idx,
                    self_evidence_excerpt=last_excerpt.text,
                    model_confidence=0.82,
                )
            )

        time_gap = _match_excerpt(excerpts, _TIME_HESITATION_PATTERN)
        if time_gap is not None:
            time_idx, time_excerpt = time_gap
            candidates.append(
                RawBreakdownCandidate(
                    code=BreakdownCode.TIME_EXPRESSION,
                    description=(
                        "The day or time expression remained tentative and needed explicit "
                        "phrasing."
                    ),
                    excerpt_index=time_idx,
                    self_evidence_excerpt=time_excerpt.text,
                    model_confidence=0.80,
                )
            )

        if goal_match is None:
            candidates.append(
                RawBreakdownCandidate(
                    code=BreakdownCode.POLITE_REQUEST,
                    description=(
                        "The opening turn did not clearly state the request for this situation."
                    ),
                    excerpt_index=0,
                    self_evidence_excerpt=excerpts[0].text,
                    model_confidence=0.78,
                )
            )

        closure_gap = _match_excerpt(excerpts, _ABRUPT_CLOSURE_PATTERN)
        if closure_gap is not None:
            close_idx, close_excerpt = closure_gap
            candidates.append(
                RawBreakdownCandidate(
                    code=BreakdownCode.CLOSURE,
                    description="The conversation closed before wrapping up the agreed follow-up.",
                    excerpt_index=close_idx,
                    self_evidence_excerpt=close_excerpt.text,
                    model_confidence=0.79,
                )
            )

        bounded_candidates = tuple(candidates[:MAX_PROPOSED_BREAKDOWNS])
        return RawExtractionPayload(
            criteria=tuple(criteria),
            breakdowns=bounded_candidates,
            model_version=self.model_version,
            fallback_used=False,
        )


def _request_fingerprint(
    *,
    minimized: MinimizedTranscript,
    consent_id: str,
    consent_attested_at: datetime,
    all_participants_consented: bool,
    policy_version: str,
    learner_goal: str,
    device_type: DeviceType,
    diarization_method: DiarizationMethod,
    recurrence_id: str | None,
    derived_retention_days: int,
) -> str:
    canonical = json.dumps(
        {
            "source_reference": minimized.source_reference,
            "learner_id": minimized.learner_id,
            "recorded_at": minimized.recorded_at.astimezone(UTC).isoformat(),
            "situation": minimized.situation.value,
            "capture_mode": minimized.capture_mode.value,
            "asr_confidence": minimized.asr_confidence,
            "self_excerpts": [
                [item.text, item.start_ms, item.end_ms, item.confidence]
                for item in minimized.self_excerpts
            ],
            "consent_id": consent_id,
            "consent_attested_at": consent_attested_at.astimezone(UTC).isoformat(),
            "all_participants_consented": all_participants_consented,
            "policy_version": policy_version,
            "learner_goal": learner_goal,
            "device_type": device_type,
            "diarization_method": diarization_method,
            "recurrence_id": recurrence_id,
            "derived_retention_days": derived_retention_days,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class BreakdownProposalService:
    """Proposes at most three self-evidenced breakdowns and builds a review-gated attempt draft."""

    def __init__(
        self,
        rubric: Rubric,
        *,
        extractor: BreakdownExtractorPort | None = None,
    ) -> None:
        self._rubric = rubric
        self._extractor: BreakdownExtractorPort = extractor or DeterministicBreakdownExtractor()
        self._results: dict[tuple[str, str], BreakdownProposalResult] = {}
        self._fingerprints: dict[tuple[str, str], str] = {}

    def propose(
        self,
        minimized: MinimizedTranscript,
        *,
        consent_id: str,
        consent_attested_at: datetime,
        all_participants_consented: bool,
        learner_goal: str,
        consent_revoked: bool = False,
        policy_version: str = "consent_v1",
        device_type: DeviceType = "bee_device",
        diarization_method: DiarizationMethod | None = None,
        recurrence_id: str | None = None,
        derived_retention_days: int = 30,
        proposed_at: datetime | None = None,
        correlation_id: str | None = None,
    ) -> BreakdownProposalResult:
        require_processing_consent(
            consent_attested=True,
            revoked=consent_revoked,
            retention_policy_present=derived_retention_days >= 1,
        )
        if (
            minimized.capture_mode is CaptureMode.CONSENTED_CONVERSATION
            and not all_participants_consented
        ):
            raise ConsentError("Every participant must consent to conversation processing")

        _require_ulid(minimized.learner_id, "learner_id")
        _require_ulid(consent_id, "consent_id")
        if recurrence_id is not None:
            _require_ulid(recurrence_id, "recurrence_id")
        if not _POLICY_VERSION_PATTERN.match(policy_version):
            raise ValueError("Invalid consent policy_version")
        cleaned_goal = _normalize_whitespace(learner_goal)
        if not 1 <= len(cleaned_goal) <= 300:
            raise ValueError("Learner goal must be between 1 and 300 characters")
        if device_type not in {"bee_device", "apple_watch_bee"}:
            raise ValueError("Unsupported Bee device_type")

        resolved_diarization: DiarizationMethod = diarization_method or (
            "self_only" if minimized.capture_mode is CaptureMode.SELF_MEMO else "bee_speaker_labels"
        )
        if minimized.capture_mode is CaptureMode.SELF_MEMO and resolved_diarization != "self_only":
            raise ValueError("Self-memo mode requires self_only diarization")

        attested_iso = _iso_millis(consent_attested_at, "Consent attestation timestamp")
        recorded_utc = minimized.recorded_at.astimezone(UTC)
        created_utc = (proposed_at or datetime.now(UTC)).astimezone(UTC)
        _iso_millis(proposed_at or created_utc, "Proposal timestamp")
        if created_utc < recorded_utc:
            raise ValueError("Proposal timestamp cannot precede recording timestamp")

        key = (minimized.learner_id, minimized.source_reference)
        fingerprint = _request_fingerprint(
            minimized=minimized,
            consent_id=consent_id,
            consent_attested_at=consent_attested_at,
            all_participants_consented=all_participants_consented,
            policy_version=policy_version,
            learner_goal=cleaned_goal,
            device_type=device_type,
            diarization_method=resolved_diarization,
            recurrence_id=recurrence_id,
            derived_retention_days=derived_retention_days,
        )
        if key in self._results:
            if self._fingerprints[key] != fingerprint:
                raise ValueError("source_reference_reused_with_different_content")
            cached = self._results[key]
            return replace(
                cached,
                attempt_document=deepcopy(cached.attempt_document),
                duplicate=True,
            )

        effective_correlation_id = correlation_id or deterministic_ulid(
            namespace="correlation",
            seed=f"{minimized.learner_id}:{minimized.source_reference}",
            timestamp=created_utc,
        )
        raw = self._extractor.extract(
            minimized,
            self._rubric,
            correlation_id=effective_correlation_id,
        )
        criteria, breakdowns = validate_raw_extraction(
            minimized,
            self._rubric,
            raw,
            timestamp=created_utc,
        )

        diarization_ambiguous = "ambiguous_speaker_excluded" in minimized.reason_codes
        outcome = evaluate_authentic_attempt(
            self._rubric,
            AttemptEvaluationInput(
                situation=minimized.situation,
                criteria={
                    item.criterion_id: CriterionEvidence(
                        result=item.result,
                        evidence_present=bool(item.self_evidence_excerpt.strip()),
                    )
                    for item in criteria
                },
                asr_confidence=minimized.asr_confidence,
                minimization_complete=True,
                interaction_ended=True,
                diarization_ambiguous=diarization_ambiguous,
            ),
        )

        review_reasons: set[str] = set(outcome.reason_codes)
        if minimized.capture_mode is CaptureMode.SELF_MEMO:
            review_reasons.add("self_memo_requires_learner_review")
        if minimized.asr_confidence < OUTCOME_CONFIDENCE_THRESHOLD:
            review_reasons.add("asr_confidence_below_0_75")
        if diarization_ambiguous:
            review_reasons.add("ambiguous_speaker_excluded")
        if breakdowns:
            review_reasons.add("proposed_breakdown_requires_confirmation")
        if any(item.confidence < BREAKDOWN_REVIEW_CONFIDENCE_THRESHOLD for item in breakdowns):
            review_reasons.add("proposed_breakdown_below_0_85")

        learner_review_required = (
            outcome.confirmation_required
            or minimized.capture_mode is CaptureMode.SELF_MEMO
            or minimized.asr_confidence < OUTCOME_CONFIDENCE_THRESHOLD
            or diarization_ambiguous
            or any(item.requires_learner_review for item in breakdowns)
            or bool(breakdowns)
        )

        attempt_id = deterministic_ulid(
            namespace="attempt",
            seed=f"{minimized.learner_id}:{minimized.source_reference}",
            timestamp=recorded_utc,
        )
        transient_delete_utc = min(
            minimized.transient_source_delete_after.astimezone(UTC),
            recorded_utc + timedelta(hours=24),
        )
        derived_delete_utc = created_utc + timedelta(days=derived_retention_days)

        context_doc: dict[str, Any] = {
            "situation": minimized.situation.value,
            "target_language": "nl",
            "support_language": "en",
            "learner_goal": cleaned_goal,
            "rubric_version": self._rubric.version,
        }
        if recurrence_id is not None:
            context_doc["recurrence_id"] = recurrence_id

        criteria_docs: list[dict[str, Any]] = []
        for item in criteria:
            criterion_doc: dict[str, Any] = {
                "criterion_id": item.criterion_id,
                "result": item.result.value,
                "self_evidence_excerpt": item.self_evidence_excerpt,
            }
            if item.self_evidence_start_ms is not None and item.self_evidence_end_ms is not None:
                criterion_doc["self_evidence_start_ms"] = item.self_evidence_start_ms
                criterion_doc["self_evidence_end_ms"] = item.self_evidence_end_ms
            criteria_docs.append(criterion_doc)

        attempt_document: dict[str, Any] = {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "learner_id": minimized.learner_id,
            "source": {
                "bee_recording_reference": minimized.source_reference,
                "recorded_at": _iso_millis(recorded_utc, "Recorded timestamp"),
                "capture_mode": minimized.capture_mode.value,
                "device_type": device_type,
                "provenance": "live_bee" if minimized.provenance == "live" else "fixture",
            },
            "context": context_doc,
            "consent": {
                "consent_id": consent_id,
                "policy_version": policy_version,
                "attested_at": attested_iso,
                "all_participants_consented": all_participants_consented,
                "revoked": False,
            },
            "processing": {
                "diarization_method": resolved_diarization,
                "asr_confidence": round(minimized.asr_confidence, 4),
                "redaction_status": "minimized",
                "other_speaker_transcript_retained": False,
                "model_version": raw.model_version.strip(),
            },
            "outcome": {
                "status": outcome.status.value,
                "criteria": criteria_docs,
                "confirmation_required": learner_review_required,
                "confirmed_by_learner": False,
            },
            "breakdowns": [
                {
                    "breakdown_id": item.breakdown_id,
                    "code": item.code.value,
                    "description": item.description,
                    "self_evidence_excerpt": item.self_evidence_excerpt,
                    "confidence": item.confidence,
                    "review_status": item.review_status,
                }
                for item in breakdowns
            ],
            "status": "awaiting_learner_review",
            "retention": {
                "transient_source_delete_after": _iso_millis(
                    transient_delete_utc, "Transient source deletion timestamp"
                ),
                "derived_delete_after": _iso_millis(
                    derived_delete_utc, "Derived deletion timestamp"
                ),
            },
            "version": 1,
            "created_at": _iso_millis(created_utc, "Created timestamp"),
            "updated_at": _iso_millis(created_utc, "Updated timestamp"),
        }

        issues = validate_minimized_attempt(attempt_document)
        prohibited = find_prohibited_transcript_fields(attempt_document)
        if issues or prohibited:
            raise BreakdownValidationError(
                f"invalid_canonical_attempt:{','.join(issues + prohibited)}"
            )

        result = BreakdownProposalResult(
            attempt_id=attempt_id,
            learner_id=minimized.learner_id,
            source_reference=minimized.source_reference,
            situation=minimized.situation,
            capture_mode=minimized.capture_mode,
            criteria=criteria,
            outcome=outcome,
            breakdowns=breakdowns,
            asr_confidence=minimized.asr_confidence,
            model_version=raw.model_version.strip(),
            fallback_used=raw.fallback_used,
            fallback_reason=raw.fallback_reason,
            learner_review_required=learner_review_required,
            review_reason_codes=tuple(sorted(review_reasons)),
            attempt_document=attempt_document,
            duplicate=False,
        )
        self._results[key] = result
        self._fingerprints[key] = fingerprint
        return replace(result, attempt_document=deepcopy(attempt_document))

    def get(
        self,
        *,
        actor_learner_id: str,
        learner_id: str,
        source_reference: str,
    ) -> BreakdownProposalResult:
        if actor_learner_id != learner_id:
            raise PermissionError("attempt_access_denied")
        key = (learner_id, source_reference)
        if key not in self._results:
            raise KeyError("breakdown_proposal_not_found")
        cached = self._results[key]
        return replace(cached, attempt_document=deepcopy(cached.attempt_document))
