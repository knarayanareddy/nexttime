from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .minimization import validate_minimized_attempt
from .models import CriterionResult, Rubric, Situation, SkillState
from .review import AttemptReviewResult


class FunctionalProgress(StrEnum):
    IMPROVED = "improved"
    DECLINED = "declined"
    UNCHANGED = "unchanged"
    NEW_POSITIVE_EVIDENCE = "new_positive_evidence"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class CriterionDelta:
    criterion_id: str
    prior_result: CriterionResult
    next_result: CriterionResult
    delta: str
    prior_self_evidence: str
    next_self_evidence: str


@dataclass(frozen=True)
class OutcomeComparison:
    prior_attempt_id: str
    next_attempt_id: str
    learner_id: str
    recurrence_id: str
    situation: str
    rubric_version: str
    criterion_deltas: tuple[CriterionDelta, ...]
    functional_progress: FunctionalProgress
    comparison_confidence: str
    uncertainty_codes: tuple[str, ...]
    causal_claim: bool = False


@dataclass(frozen=True)
class FunctionalWin:
    prior_attempt_id: str
    next_attempt_id: str
    criterion_id: str
    kind: str
    self_evidence_excerpt: str


@dataclass(frozen=True)
class ProgressRecord:
    attempt_id: str
    comparison: OutcomeComparison | None
    skill_states: Mapping[str, SkillState]
    duplicate: bool


@dataclass(frozen=True)
class ProgressSnapshot:
    authentic_attempts: tuple[dict[str, Any], ...]
    comparisons: tuple[OutcomeComparison, ...]
    functional_wins: tuple[FunctionalWin, ...]
    skill_states: Mapping[str, SkillState]
    rehearsal_history: tuple[dict[str, Any], ...]
    causal_claim: bool = False
    gamified_streaks: bool = False


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Attempt timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _fingerprint(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _validate_confirmed_authentic(document: dict[str, Any]) -> None:
    try:
        issues = validate_minimized_attempt(document)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid_confirmed_authentic_attempt") from error
    if issues:
        raise ValueError(f"invalid_confirmed_authentic_attempt:{','.join(issues)}")
    if (
        document.get("status") != "confirmed"
        or document.get("consent", {}).get("revoked") is not False
        or document.get("outcome", {}).get("confirmed_by_learner") is not True
        or document.get("processing", {}).get("redaction_status") != "minimized"
        or document.get("processing", {}).get("other_speaker_transcript_retained") is not False
        or document.get("source", {}).get("capture_mode")
        not in {"consented_conversation", "self_memo"}
    ):
        raise ValueError("confirmed_authentic_attempt_required")
    if not document.get("attempt_id", "").strip() or not document.get("learner_id", "").strip():
        raise ValueError("attempt_identity_required")
    _parse_timestamp(document["source"]["recorded_at"])


def _criterion_map(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    criteria = document["outcome"]["criteria"]
    result: dict[str, dict[str, Any]] = {}
    for criterion in criteria:
        criterion_id = criterion["criterion_id"]
        if criterion_id in result:
            raise ValueError("duplicate_attempt_criterion")
        result[criterion_id] = criterion
    return result


def _criterion_delta(
    criterion_id: str,
    prior: dict[str, Any] | None,
    next_: dict[str, Any] | None,
) -> CriterionDelta:
    prior_result = CriterionResult(prior["result"]) if prior else CriterionResult.UNKNOWN
    next_result = CriterionResult(next_["result"]) if next_ else CriterionResult.UNKNOWN
    if prior_result is next_result:
        delta = "unchanged" if prior_result is not CriterionResult.UNKNOWN else "unchanged_unknown"
    elif next_result is CriterionResult.UNKNOWN:
        delta = "uncertain"
    elif prior_result is CriterionResult.UNKNOWN:
        delta = "new_positive_evidence" if next_result is CriterionResult.SATISFIED else "uncertain"
    elif prior_result is CriterionResult.NOT_SATISFIED and next_result is CriterionResult.SATISFIED:
        delta = "improved"
    elif prior_result is CriterionResult.SATISFIED and next_result is CriterionResult.NOT_SATISFIED:
        delta = "declined"
    else:
        delta = "unchanged"
    return CriterionDelta(
        criterion_id=criterion_id,
        prior_result=prior_result,
        next_result=next_result,
        delta=delta,
        prior_self_evidence=(prior or {}).get("self_evidence_excerpt", ""),
        next_self_evidence=(next_ or {}).get("self_evidence_excerpt", ""),
    )


def compare_confirmed_attempts(
    prior: dict[str, Any],
    next_: dict[str, Any],
    rubric: Rubric,
) -> OutcomeComparison:
    """Compare only two learner-confirmed authentic attempts with identical linkage."""
    _validate_confirmed_authentic(prior)
    _validate_confirmed_authentic(next_)
    if prior["attempt_id"] == next_["attempt_id"]:
        raise ValueError("comparison_requires_distinct_attempts")
    prior_context = prior["context"]
    next_context = next_["context"]
    linkage_fields = ("learner_id",)
    if any(prior[field] != next_[field] for field in linkage_fields):
        raise ValueError("comparison_learner_mismatch")
    for field in ("situation", "rubric_version", "recurrence_id"):
        if prior_context.get(field) != next_context.get(field):
            raise ValueError("comparison_recurrence_or_rubric_mismatch")
    if _parse_timestamp(prior["source"]["recorded_at"]) >= _parse_timestamp(
        next_["source"]["recorded_at"]
    ):
        raise ValueError("comparison_attempt_order_invalid")
    situation = Situation(prior_context["situation"])
    if prior_context["rubric_version"] != rubric.version:
        raise ValueError("comparison_rubric_not_loaded")
    prior_criteria = _criterion_map(prior)
    next_criteria = _criterion_map(next_)
    ordered_ids = tuple(dict.fromkeys(rubric.required[situation] + rubric.optional[situation]))
    deltas = tuple(
        _criterion_delta(
            criterion_id, prior_criteria.get(criterion_id), next_criteria.get(criterion_id)
        )
        for criterion_id in ordered_ids
    )
    required_deltas = deltas[: len(rubric.required[situation])]
    uncertainty: list[str] = []
    for attempt_label, document in (("prior", prior), ("next", next_)):
        if document["processing"]["asr_confidence"] < 0.75:
            uncertainty.append(f"{attempt_label}_low_asr")
    for delta in required_deltas:
        if delta.prior_result is CriterionResult.UNKNOWN:
            uncertainty.append(f"prior_unknown:{delta.criterion_id}")
        if delta.next_result is CriterionResult.UNKNOWN:
            uncertainty.append(f"next_unknown:{delta.criterion_id}")
    if any(delta.delta in {"uncertain", "unchanged_unknown"} for delta in required_deltas):
        progress = FunctionalProgress.INSUFFICIENT_EVIDENCE
    elif any(delta.delta == "declined" for delta in required_deltas):
        progress = FunctionalProgress.DECLINED
    elif any(delta.delta == "improved" for delta in required_deltas):
        progress = FunctionalProgress.IMPROVED
    elif any(delta.delta == "new_positive_evidence" for delta in required_deltas):
        progress = FunctionalProgress.NEW_POSITIVE_EVIDENCE
    else:
        progress = FunctionalProgress.UNCHANGED
    confidence = "limited" if uncertainty else "high"
    return OutcomeComparison(
        prior_attempt_id=prior["attempt_id"],
        next_attempt_id=next_["attempt_id"],
        learner_id=prior["learner_id"],
        recurrence_id=prior_context["recurrence_id"],
        situation=prior_context["situation"],
        rubric_version=prior_context["rubric_version"],
        criterion_deltas=deltas,
        functional_progress=progress,
        comparison_confidence=confidence,
        uncertainty_codes=tuple(dict.fromkeys(uncertainty)),
    )


class AuthenticProgressService:
    """Fixture/in-memory projection of confirmed review results, never rehearsal mastery."""

    def __init__(
        self,
        rubric: Rubric,
        *,
        initial_skill_states: Mapping[str, SkillState] | None = None,
    ) -> None:
        self._rubric = rubric
        self._learner_id: str | None = None
        self._skill_states = dict(initial_skill_states or {})
        self._attempts: dict[str, dict[str, Any]] = {}
        self._attempt_fingerprints: dict[str, str] = {}
        self._attempt_skill_states: dict[str, dict[str, SkillState]] = {}
        self._records: dict[str, ProgressRecord] = {}
        self._comparisons: dict[tuple[str, str], OutcomeComparison] = {}
        self._rehearsals: dict[str, dict[str, Any]] = {}

    def record_review(self, result: AttemptReviewResult) -> ProgressRecord:
        document = deepcopy(result.document)
        _validate_confirmed_authentic(document)
        learner_id = document["learner_id"]
        if self._learner_id is not None and learner_id != self._learner_id:
            raise ValueError("progress_learner_scope_mismatch")
        attempt_id = document["attempt_id"]
        fingerprint = _fingerprint(document)
        skill_states = dict(result.skill_states)
        if attempt_id in self._attempts:
            if (
                self._attempt_fingerprints[attempt_id] != fingerprint
                or self._attempt_skill_states[attempt_id] != skill_states
            ):
                raise ValueError("confirmed_attempt_replay_conflict")
            existing_record = self._records[attempt_id]
            return ProgressRecord(
                attempt_id=attempt_id,
                comparison=existing_record.comparison,
                skill_states=dict(self._skill_states),
                duplicate=True,
            )
        for existing_document in self._attempts.values():
            if (
                existing_document["learner_id"] == document["learner_id"]
                and existing_document["context"]["recurrence_id"]
                == document["context"]["recurrence_id"]
                and _parse_timestamp(existing_document["source"]["recorded_at"])
                >= _parse_timestamp(document["source"]["recorded_at"])
            ):
                raise ValueError("authentic_attempts_must_be_recorded_in_order")
        prior_document: dict[str, Any] | None = None
        for candidate in reversed(tuple(self._attempts.values())):
            if (
                candidate["learner_id"] == document["learner_id"]
                and candidate["context"]["recurrence_id"] == document["context"]["recurrence_id"]
                and candidate["context"]["rubric_version"] == document["context"]["rubric_version"]
            ):
                prior_document = candidate
                break
        comparison = (
            compare_confirmed_attempts(prior_document, document, self._rubric)
            if prior_document is not None
            else None
        )
        self._learner_id = learner_id
        self._attempts[attempt_id] = document
        self._attempt_fingerprints[attempt_id] = fingerprint
        self._attempt_skill_states[attempt_id] = dict(skill_states)
        self._skill_states = skill_states
        if comparison is not None:
            self._comparisons[(comparison.prior_attempt_id, comparison.next_attempt_id)] = (
                comparison
            )
        record = ProgressRecord(
            attempt_id=attempt_id,
            comparison=comparison,
            skill_states=dict(self._skill_states),
            duplicate=False,
        )
        self._records[attempt_id] = record
        return record

    def record_rehearsal(self, rehearsal: Mapping[str, Any]) -> None:
        allowed = {"rehearsal_id", "recurrence_id", "target_function", "state", "completed_at"}
        if set(rehearsal) - allowed or not rehearsal.get("rehearsal_id", "").strip():
            raise ValueError("invalid_rehearsal_history_metadata")
        rehearsal_id = rehearsal["rehearsal_id"]
        copy = deepcopy(dict(rehearsal))
        if rehearsal_id in self._rehearsals:
            if self._rehearsals[rehearsal_id] != copy:
                raise ValueError("rehearsal_history_replay_conflict")
            return
        self._rehearsals[rehearsal_id] = copy

    def snapshot(self) -> ProgressSnapshot:
        wins: list[FunctionalWin] = []
        for comparison in self._comparisons.values():
            for delta in comparison.criterion_deltas:
                if delta.delta in {"improved", "new_positive_evidence"}:
                    wins.append(
                        FunctionalWin(
                            prior_attempt_id=comparison.prior_attempt_id,
                            next_attempt_id=comparison.next_attempt_id,
                            criterion_id=delta.criterion_id,
                            kind=delta.delta,
                            self_evidence_excerpt=delta.next_self_evidence,
                        )
                    )
        return ProgressSnapshot(
            authentic_attempts=tuple(deepcopy(tuple(self._attempts.values()))),
            comparisons=tuple(self._comparisons.values()),
            functional_wins=tuple(wins),
            skill_states=dict(self._skill_states),
            rehearsal_history=tuple(deepcopy(tuple(self._rehearsals.values()))),
        )
