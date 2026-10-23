from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from .minimization import validate_minimized_attempt
from .models import (
    AttemptEvaluationInput,
    CriterionEvidence,
    CriterionResult,
    OutcomeEvaluation,
    OutcomeStatus,
    Rubric,
    Situation,
    SkillState,
)
from .rubric import evaluate_authentic_attempt
from .skills import update_skill_from_authentic_attempt


class BreakdownReviewDecision(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class CriterionCorrection:
    criterion_id: str
    result: CriterionResult
    self_evidence_excerpt: str
    self_evidence_start_ms: int | None = None
    self_evidence_end_ms: int | None = None


@dataclass(frozen=True)
class AttemptReviewResult:
    document: dict[str, Any]
    evaluation: OutcomeEvaluation | None
    skill_states: Mapping[str, SkillState]
    skill_updates: tuple[str, ...]


_BREAKDOWN_SKILLS = {
    "confirmation_missing": "confirmation",
    "comprehension_repair": "comprehension_repair",
    "time_expression": "time_expression",
    "polite_request": "polite_request",
    "closure": "closure",
}


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Review timestamp must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Attempt timestamps must include a timezone")
    return parsed


class AttemptReviewService:
    """In-memory learner review; durable attempt history and deletion are separate adapters."""

    def __init__(
        self,
        document: dict[str, Any],
        rubric: Rubric,
        *,
        initial_skill_states: Mapping[str, SkillState] | None = None,
        diarization_ambiguous: bool = False,
    ) -> None:
        try:
            issues = validate_minimized_attempt(document)
        except (KeyError, TypeError) as error:
            raise ValueError("Invalid minimized attempt: malformed document") from error
        if issues:
            raise ValueError(f"Invalid minimized attempt: {issues}")
        if document["context"]["rubric_version"] != rubric.version:
            raise ValueError("Attempt and rubric versions do not match")
        if document["status"] not in {
            "awaiting_learner_review",
            "confirmed",
            "excluded",
            "deletion_pending",
        }:
            raise ValueError("Attempt is not ready for learner review")
        self._document = deepcopy(document)
        self._rubric = rubric
        self._skill_states = dict(initial_skill_states or {})
        self._diarization_ambiguous = diarization_ambiguous
        self._evaluation: OutcomeEvaluation | None = None
        self._skill_updates: tuple[str, ...] = ()

    def _result(self) -> AttemptReviewResult:
        return AttemptReviewResult(
            document=deepcopy(self._document),
            evaluation=self._evaluation,
            skill_states=dict(self._skill_states),
            skill_updates=self._skill_updates,
        )

    def _require_actor(self, actor_learner_id: str) -> None:
        if actor_learner_id != self._document["learner_id"]:
            raise PermissionError("attempt_access_denied")

    def _require_version(self, expected_version: int) -> None:
        if expected_version != self._document["version"]:
            raise ValueError("stale_attempt_version")

    def _require_editable(self) -> None:
        if self._document["status"] != "awaiting_learner_review":
            raise ValueError("attempt_is_not_awaiting_learner_review")

    def _review_timestamp(self, reviewed_at: datetime) -> str:
        timestamp = _iso(reviewed_at)
        if _parse(timestamp) < _parse(self._document["updated_at"]):
            raise ValueError("Review timestamp cannot precede the current attempt version")
        return timestamp

    def _commit_timestamp(self, timestamp: str) -> None:
        self._document["updated_at"] = timestamp
        self._document["version"] += 1

    def review_breakdown(
        self,
        *,
        actor_learner_id: str,
        expected_version: int,
        breakdown_id: str,
        decision: BreakdownReviewDecision,
        reviewed_at: datetime,
    ) -> AttemptReviewResult:
        self._require_actor(actor_learner_id)
        breakdown = next(
            (item for item in self._document["breakdowns"] if item["breakdown_id"] == breakdown_id),
            None,
        )
        if breakdown is None:
            raise ValueError("breakdown_not_found")
        if breakdown["review_status"] == decision.value:
            return self._result()
        self._require_editable()
        self._require_version(expected_version)
        timestamp = self._review_timestamp(reviewed_at)
        breakdown["review_status"] = decision.value
        self._commit_timestamp(timestamp)
        return self._result()

    def _corrected_criteria(
        self, corrections: tuple[CriterionCorrection, ...]
    ) -> list[dict[str, Any]]:
        criteria: list[dict[str, Any]] = deepcopy(self._document["outcome"]["criteria"])
        if len({item["criterion_id"] for item in criteria}) != len(criteria):
            raise ValueError("Duplicate criterion IDs are invalid")
        situation = Situation(self._document["context"]["situation"])
        allowed = set(self._rubric.required[situation]) | set(self._rubric.optional[situation])
        for correction in corrections:
            if correction.criterion_id not in allowed:
                raise ValueError("Correction is outside the selected situation rubric")
            if (
                correction.result is not CriterionResult.UNKNOWN
                and not correction.self_evidence_excerpt.strip()
            ):
                raise ValueError("Non-unknown corrections require learner self-evidence")
            start = correction.self_evidence_start_ms
            end = correction.self_evidence_end_ms
            if (start is None) != (end is None):
                raise ValueError("Evidence start and end must be supplied together")
            if start is not None and end is not None and (start < 0 or end <= start):
                raise ValueError("Evidence timing is invalid")
            corrected: dict[str, Any] = {
                "criterion_id": correction.criterion_id,
                "result": correction.result.value,
                "self_evidence_excerpt": correction.self_evidence_excerpt,
            }
            if start is not None and end is not None:
                corrected["self_evidence_start_ms"] = start
                corrected["self_evidence_end_ms"] = end
            existing = next(
                (
                    index
                    for index, item in enumerate(criteria)
                    if item["criterion_id"] == correction.criterion_id
                ),
                None,
            )
            if existing is None:
                criteria.append(corrected)
            else:
                criteria[existing] = corrected
        if len({item.criterion_id for item in corrections}) != len(corrections):
            raise ValueError("Duplicate criterion corrections are invalid")
        return criteria

    def _evaluate(
        self,
        criteria: list[dict[str, Any]],
        *,
        interaction_ended: bool,
        assistance_used: bool,
    ) -> OutcomeEvaluation:
        situation = Situation(self._document["context"]["situation"])
        evidence = {
            item["criterion_id"]: CriterionEvidence(
                result=CriterionResult(item["result"]),
                evidence_present=bool(item["self_evidence_excerpt"].strip()),
            )
            for item in criteria
        }
        return evaluate_authentic_attempt(
            self._rubric,
            AttemptEvaluationInput(
                situation=situation,
                criteria=evidence,
                asr_confidence=self._document["processing"]["asr_confidence"],
                minimization_complete=(
                    self._document["processing"]["redaction_status"] == "minimized"
                ),
                interaction_ended=interaction_ended,
                diarization_ambiguous=self._diarization_ambiguous,
                assistance_used=assistance_used,
            ),
        )

    def _validate_breakdown_outcome_consistency(self, criteria: list[dict[str, Any]]) -> None:
        confirmed_codes = {
            item["code"]
            for item in self._document["breakdowns"]
            if item["review_status"] == "confirmed"
        }
        if "confirmation_missing" not in confirmed_codes:
            return
        confirmation_ids = {"appointment_confirmed", "next_step_confirmed"}
        if any(
            item["criterion_id"] in confirmation_ids
            and item["result"] == CriterionResult.SATISFIED.value
            for item in criteria
        ):
            raise ValueError("confirmed_breakdown_contradicts_criterion")

    def confirm_attempt(
        self,
        *,
        actor_learner_id: str,
        expected_version: int,
        reviewed_at: datetime,
        corrections: tuple[CriterionCorrection, ...] = (),
        interaction_ended: bool = True,
        assistance_used: bool = False,
    ) -> AttemptReviewResult:
        self._require_actor(actor_learner_id)
        if self._document["status"] == "confirmed":
            if corrections:
                raise ValueError("confirmed_attempt_correction_requires_a_new_review_version")
            return self._result()
        self._require_editable()
        self._require_version(expected_version)
        if any(item["review_status"] == "proposed" for item in self._document["breakdowns"]):
            raise ValueError("All breakdowns require learner review before confirmation")
        criteria = self._corrected_criteria(corrections)
        self._validate_breakdown_outcome_consistency(criteria)
        evaluation = self._evaluate(
            criteria,
            interaction_ended=interaction_ended,
            assistance_used=assistance_used,
        )
        timestamp = self._review_timestamp(reviewed_at)
        self._document["outcome"]["criteria"] = criteria
        self._document["outcome"]["status"] = evaluation.status.value
        self._document["outcome"]["confirmation_required"] = False
        self._document["outcome"]["confirmed_by_learner"] = True
        self._document["outcome"]["confirmed_at"] = timestamp
        self._document["status"] = "confirmed"
        self._commit_timestamp(timestamp)
        self._evaluation = evaluation

        updated_skills: list[str] = []
        if evaluation.status is not OutcomeStatus.UNKNOWN:
            confirmed_codes = {
                item["code"]
                for item in self._document["breakdowns"]
                if item["review_status"] == "confirmed"
            }
            for code in sorted(confirmed_codes):
                skill = _BREAKDOWN_SKILLS.get(code)
                if skill is None:
                    continue
                prior = self._skill_states.get(skill, SkillState())
                updated = update_skill_from_authentic_attempt(
                    prior,
                    CriterionResult.NOT_SATISFIED,
                    confirmed_authentic_attempt=True,
                )
                self._skill_states[skill] = updated
                if updated != prior:
                    updated_skills.append(skill)
        self._skill_updates = tuple(updated_skills)
        return self._result()

    def exclude_attempt(
        self,
        *,
        actor_learner_id: str,
        expected_version: int,
        reviewed_at: datetime,
        reason: Literal["learner_excluded", "consent_revoked"],
    ) -> AttemptReviewResult:
        self._require_actor(actor_learner_id)
        if reason not in {"learner_excluded", "consent_revoked"}:
            raise ValueError("Unknown attempt-exclusion reason")
        status = self._document["status"]
        if status == "deletion_pending":
            if reason == "consent_revoked":
                return self._result()
            raise ValueError("attempt_is_not_awaiting_learner_review")
        if status == "excluded" and reason == "learner_excluded":
            return self._result()
        self._require_version(expected_version)
        timestamp = self._review_timestamp(reviewed_at)
        if reason == "consent_revoked":
            self._document["consent"]["revoked"] = True
            self._document["status"] = "deletion_pending"
        else:
            self._document["status"] = "excluded"
        self._document["outcome"]["confirmed_by_learner"] = False
        self._skill_updates = ()
        self._commit_timestamp(timestamp)
        return self._result()
