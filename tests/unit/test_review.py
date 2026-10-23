from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from nexttime_domain import (
    AttemptReviewService,
    BreakdownReviewDecision,
    CriterionCorrection,
    CriterionResult,
    OutcomeStatus,
    Rubric,
    SkillState,
    validate_minimized_attempt,
)

LEARNER = "01000000000000000000000002"
BREAKDOWN = "01000000000000000000000005"
REVIEWED_AT = datetime(2026, 10, 22, 16, tzinfo=UTC)


def review(document: dict[str, Any], rubric: Rubric, **kwargs: Any) -> AttemptReviewService:
    return AttemptReviewService(document, rubric, **kwargs)


def decide(
    service: AttemptReviewService,
    decision: BreakdownReviewDecision = BreakdownReviewDecision.CONFIRMED,
    version: int = 1,
) -> None:
    service.review_breakdown(
        actor_learner_id=LEARNER,
        expected_version=version,
        breakdown_id=BREAKDOWN,
        decision=decision,
        reviewed_at=REVIEWED_AT,
    )


def confirm(service: AttemptReviewService, version: int = 2, **kwargs: Any) -> Any:
    return service.confirm_attempt(
        actor_learner_id=LEARNER,
        expected_version=version,
        reviewed_at=REVIEWED_AT,
        **kwargs,
    )


def test_confirmed_breakdown_updates_one_skill_once(
    attempt_document: dict[str, Any], rubric: Rubric
) -> None:
    service = review(
        attempt_document, rubric, initial_skill_states={"confirmation": SkillState(beta=2)}
    )
    reviewed = service.review_breakdown(
        actor_learner_id=LEARNER,
        expected_version=1,
        breakdown_id=BREAKDOWN,
        decision=BreakdownReviewDecision.CONFIRMED,
        reviewed_at=REVIEWED_AT,
    )
    assert reviewed.document["version"] == 2
    assert reviewed.skill_states["confirmation"].beta == 2
    assert reviewed.skill_updates == ()

    result = confirm(service)
    assert result.document["status"] == "confirmed"
    assert result.document["version"] == 3
    assert result.document["outcome"]["confirmed_by_learner"] is True
    assert result.document["outcome"]["confirmation_required"] is False
    assert result.evaluation is not None
    assert result.evaluation.status is OutcomeStatus.PARTIAL
    assert result.skill_updates == ("confirmation",)
    assert result.skill_states["confirmation"] == SkillState(beta=3, authentic_attempt_count=1)
    assert validate_minimized_attempt(result.document) == []

    repeated = confirm(service, version=2)
    assert repeated.document == result.document
    assert repeated.skill_states == result.skill_states
    assert repeated.skill_updates == ("confirmation",)


def test_rejected_breakdown_and_success_correction_do_not_update_skill(
    attempt_document: dict[str, Any], rubric: Rubric
) -> None:
    service = review(attempt_document, rubric)
    decide(service, BreakdownReviewDecision.REJECTED)
    correction = CriterionCorrection(
        criterion_id="appointment_confirmed",
        result=CriterionResult.SATISFIED,
        self_evidence_excerpt="Ja, om drie uur.",
        self_evidence_start_ms=5000,
        self_evidence_end_ms=6200,
    )
    result = confirm(service, corrections=(correction,))
    assert result.evaluation is not None
    assert result.evaluation.status is OutcomeStatus.SUCCESS
    assert result.document["outcome"]["status"] == "success"
    assert result.document["breakdowns"][0]["review_status"] == "rejected"
    assert result.skill_updates == ()
    assert result.skill_states == {}


def test_proposed_breakdown_blocks_confirmation_without_mutation(
    attempt_document: dict[str, Any], rubric: Rubric
) -> None:
    service = review(attempt_document, rubric)
    with pytest.raises(ValueError, match="require learner review"):
        confirm(service, version=1)
    assert service._document == attempt_document  # noqa: SLF001 - atomicity regression


def test_review_authorization_versions_and_idempotency(
    attempt_document: dict[str, Any], rubric: Rubric
) -> None:
    service = review(attempt_document, rubric)
    with pytest.raises(PermissionError, match="access_denied"):
        service.review_breakdown(
            actor_learner_id="other-learner",
            expected_version=1,
            breakdown_id=BREAKDOWN,
            decision=BreakdownReviewDecision.CONFIRMED,
            reviewed_at=REVIEWED_AT,
        )
    with pytest.raises(ValueError, match="breakdown_not_found"):
        service.review_breakdown(
            actor_learner_id=LEARNER,
            expected_version=1,
            breakdown_id="missing",
            decision=BreakdownReviewDecision.CONFIRMED,
            reviewed_at=REVIEWED_AT,
        )
    with pytest.raises(ValueError, match="stale_attempt_version"):
        decide(service, version=9)
    decide(service)
    first = service.review_breakdown(
        actor_learner_id=LEARNER,
        expected_version=1,
        breakdown_id=BREAKDOWN,
        decision=BreakdownReviewDecision.CONFIRMED,
        reviewed_at=REVIEWED_AT,
    )
    assert first.document["version"] == 2
    changed = service.review_breakdown(
        actor_learner_id=LEARNER,
        expected_version=2,
        breakdown_id=BREAKDOWN,
        decision=BreakdownReviewDecision.REJECTED,
        reviewed_at=REVIEWED_AT,
    )
    assert changed.document["version"] == 3
    confirm(service, version=3)
    with pytest.raises(ValueError, match="not_awaiting"):
        decide(service, version=4)


def test_unknown_outcome_never_updates_even_with_confirmed_breakdown(
    attempt_document: dict[str, Any], rubric: Rubric
) -> None:
    low_confidence = deepcopy(attempt_document)
    low_confidence["processing"]["asr_confidence"] = 0.5
    service = review(low_confidence, rubric)
    decide(service)
    result = confirm(service)
    assert result.evaluation is not None
    assert result.evaluation.status is OutcomeStatus.UNKNOWN
    assert result.skill_updates == ()
    assert result.skill_states == {}


def test_confirmed_missing_confirmation_cannot_contradict_satisfied_criterion(
    attempt_document: dict[str, Any], rubric: Rubric
) -> None:
    service = review(attempt_document, rubric)
    decide(service)
    correction = CriterionCorrection(
        "appointment_confirmed", CriterionResult.SATISFIED, "Ja, om drie uur."
    )
    with pytest.raises(ValueError, match="contradicts_criterion"):
        confirm(service, corrections=(correction,))
    assert service._document["status"] == "awaiting_learner_review"  # noqa: SLF001


def test_invalid_corrections_fail_before_any_state_change(
    attempt_document: dict[str, Any], rubric: Rubric
) -> None:
    service = review(attempt_document, rubric)
    decide(service)
    invalid = (
        CriterionCorrection("appointment_confirmed", CriterionResult.SATISFIED, ""),
        CriterionCorrection(
            "appointment_confirmed",
            CriterionResult.UNKNOWN,
            "",
            self_evidence_start_ms=100,
        ),
        CriterionCorrection(
            "appointment_confirmed",
            CriterionResult.UNKNOWN,
            "",
            self_evidence_start_ms=100,
            self_evidence_end_ms=100,
        ),
        CriterionCorrection("unsupported", CriterionResult.UNKNOWN, ""),
    )
    for correction in invalid:
        with pytest.raises(ValueError):
            confirm(service, corrections=(correction,))
        assert service._document["version"] == 2  # noqa: SLF001

    duplicate_correction = CriterionCorrection("appointment_confirmed", CriterionResult.UNKNOWN, "")
    with pytest.raises(ValueError, match="Duplicate criterion corrections"):
        confirm(service, corrections=(duplicate_correction, duplicate_correction))
    with pytest.raises(ValueError, match="Duplicate criterion IDs"):
        duplicated = deepcopy(attempt_document)
        duplicated["outcome"]["criteria"].append(duplicated["outcome"]["criteria"][0])
        duplicate_service = review(duplicated, rubric)
        decide(duplicate_service)
        confirm(duplicate_service)


def test_exclusion_and_revocation_do_not_update_skills(
    attempt_document: dict[str, Any], rubric: Rubric
) -> None:
    service = review(attempt_document, rubric)
    with pytest.raises(ValueError, match="Unknown attempt-exclusion"):
        service.exclude_attempt(
            actor_learner_id=LEARNER,
            expected_version=1,
            reviewed_at=REVIEWED_AT,
            reason=cast(Any, "other"),
        )
    result = service.exclude_attempt(
        actor_learner_id=LEARNER,
        expected_version=1,
        reviewed_at=REVIEWED_AT,
        reason="learner_excluded",
    )
    assert result.document["status"] == "excluded"
    assert result.skill_updates == ()
    assert (
        service.exclude_attempt(
            actor_learner_id=LEARNER,
            expected_version=1,
            reviewed_at=REVIEWED_AT,
            reason="learner_excluded",
        ).document["version"]
        == 2
    )
    with pytest.raises(ValueError, match="not_awaiting"):
        confirm(service, version=2)
    after_exclusion = service.exclude_attempt(
        actor_learner_id=LEARNER,
        expected_version=2,
        reviewed_at=REVIEWED_AT + timedelta(hours=1),
        reason="consent_revoked",
    )
    assert after_exclusion.document["status"] == "deletion_pending"
    assert after_exclusion.document["consent"]["revoked"] is True
    deletion_pending = review(after_exclusion.document, rubric)
    with pytest.raises(ValueError, match="not_awaiting"):
        deletion_pending.exclude_attempt(
            actor_learner_id=LEARNER,
            expected_version=3,
            reviewed_at=REVIEWED_AT,
            reason="learner_excluded",
        )
    assert (
        deletion_pending.exclude_attempt(
            actor_learner_id=LEARNER,
            expected_version=0,
            reviewed_at=REVIEWED_AT,
            reason="consent_revoked",
        ).document
        == after_exclusion.document
    )

    revoked = review(attempt_document, rubric).exclude_attempt(
        actor_learner_id=LEARNER,
        expected_version=1,
        reviewed_at=REVIEWED_AT,
        reason="consent_revoked",
    )
    assert revoked.document["status"] == "deletion_pending"
    assert revoked.document["consent"]["revoked"] is True


def test_constructor_and_timestamp_guards(attempt_document: dict[str, Any], rubric: Rubric) -> None:
    mismatched = deepcopy(attempt_document)
    mismatched["context"]["rubric_version"] = "other"
    with pytest.raises(ValueError, match="versions do not match"):
        review(mismatched, rubric)
    with pytest.raises(ValueError, match="not ready"):
        review({**attempt_document, "status": "processing"}, rubric)
    with pytest.raises(ValueError, match="Invalid minimized attempt"):
        review({"learner_id": LEARNER}, rubric)

    service = review(attempt_document, rubric)
    with pytest.raises(ValueError, match="timezone"):
        decide_naive(service)
    with pytest.raises(ValueError, match="cannot precede"):
        service.review_breakdown(
            actor_learner_id=LEARNER,
            expected_version=1,
            breakdown_id=BREAKDOWN,
            decision=BreakdownReviewDecision.CONFIRMED,
            reviewed_at=datetime(2026, 10, 22, 14, tzinfo=UTC),
        )
    assert service._document["version"] == 1  # noqa: SLF001


def decide_naive(service: AttemptReviewService) -> None:
    service.review_breakdown(
        actor_learner_id=LEARNER,
        expected_version=1,
        breakdown_id=BREAKDOWN,
        decision=BreakdownReviewDecision.CONFIRMED,
        reviewed_at=datetime(2026, 10, 22, 16),
    )
