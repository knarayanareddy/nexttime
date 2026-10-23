from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from nexttime_domain import (
    AttemptReviewResult,
    AuthenticProgressService,
    CriterionResult,
    FunctionalProgress,
    ProgressRecord,
    Rubric,
    SkillState,
    compare_confirmed_attempts,
)


def confirmed_attempt(
    source: dict[str, Any],
    *,
    attempt_id: str,
    recorded_at: datetime,
    confirmation: CriterionResult,
    asr_confidence: float = 0.91,
) -> dict[str, Any]:
    document = deepcopy(source)
    document["attempt_id"] = attempt_id
    document["source"]["bee_recording_reference"] = f"fixture-{attempt_id}"
    document["source"]["recorded_at"] = recorded_at.isoformat().replace("+00:00", "Z")
    document["processing"]["asr_confidence"] = asr_confidence
    document["outcome"]["criteria"][1]["result"] = confirmation.value
    document["outcome"]["criteria"][1]["self_evidence_excerpt"] = (
        "Ja, de afspraak is om drie uur."
        if confirmation is CriterionResult.SATISFIED
        else "Ik heb de tijd niet bevestigd."
        if confirmation is CriterionResult.NOT_SATISFIED
        else ""
    )
    document["outcome"]["status"] = (
        "success" if confirmation is CriterionResult.SATISFIED else "partial"
    )
    document["outcome"]["confirmation_required"] = False
    document["outcome"]["confirmed_by_learner"] = True
    document["outcome"]["confirmed_at"] = (
        (recorded_at + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    )
    document["breakdowns"] = []
    document["status"] = "confirmed"
    document["created_at"] = (recorded_at + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    document["updated_at"] = (recorded_at + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    document["retention"]["transient_source_delete_after"] = (
        (recorded_at + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    )
    document["version"] = 3
    return document


def review_result(document: dict[str, Any], skills: dict[str, SkillState]) -> AttemptReviewResult:
    return AttemptReviewResult(
        document=document,
        evaluation=None,
        skill_states=skills,
        skill_updates=tuple(skills),
    )


def test_linked_attempt_comparison_reports_functional_delta_and_evidence(
    attempt_document: dict[str, Any], rubric: Rubric
) -> None:
    prior = confirmed_attempt(
        attempt_document,
        attempt_id="attempt-prior",
        recorded_at=datetime(2026, 10, 22, 15, tzinfo=UTC),
        confirmation=CriterionResult.NOT_SATISFIED,
    )
    next_ = confirmed_attempt(
        attempt_document,
        attempt_id="attempt-next",
        recorded_at=datetime(2026, 10, 24, 15, tzinfo=UTC),
        confirmation=CriterionResult.SATISFIED,
    )
    comparison = compare_confirmed_attempts(prior, next_, rubric)
    assert comparison.functional_progress is FunctionalProgress.IMPROVED
    assert comparison.comparison_confidence == "high"
    assert comparison.uncertainty_codes == ()
    delta = next(
        item for item in comparison.criterion_deltas if item.criterion_id == "appointment_confirmed"
    )
    assert delta.delta == "improved"
    assert delta.prior_result is CriterionResult.NOT_SATISFIED
    assert delta.next_result is CriterionResult.SATISFIED
    assert "drie uur" in delta.next_self_evidence
    assert comparison.causal_claim is False


def test_unknown_prior_evidence_is_not_rewritten_as_certain_improvement(
    attempt_document: dict[str, Any], rubric: Rubric
) -> None:
    prior = confirmed_attempt(
        attempt_document,
        attempt_id="attempt-prior-unknown",
        recorded_at=datetime(2026, 10, 22, 15, tzinfo=UTC),
        confirmation=CriterionResult.UNKNOWN,
    )
    next_ = confirmed_attempt(
        attempt_document,
        attempt_id="attempt-next-known",
        recorded_at=datetime(2026, 10, 24, 15, tzinfo=UTC),
        confirmation=CriterionResult.SATISFIED,
        asr_confidence=0.70,
    )
    comparison = compare_confirmed_attempts(prior, next_, rubric)
    assert comparison.functional_progress is FunctionalProgress.NEW_POSITIVE_EVIDENCE
    assert comparison.comparison_confidence == "limited"
    assert "prior_unknown:appointment_confirmed" in comparison.uncertainty_codes
    assert "next_low_asr" in comparison.uncertainty_codes


def test_comparison_rejects_unconfirmed_cross_learner_and_mislinked_attempts(
    attempt_document: dict[str, Any], rubric: Rubric
) -> None:
    prior = confirmed_attempt(
        attempt_document,
        attempt_id="attempt-prior-link",
        recorded_at=datetime(2026, 10, 22, 15, tzinfo=UTC),
        confirmation=CriterionResult.NOT_SATISFIED,
    )
    next_ = confirmed_attempt(
        attempt_document,
        attempt_id="attempt-next-link",
        recorded_at=datetime(2026, 10, 24, 15, tzinfo=UTC),
        confirmation=CriterionResult.SATISFIED,
    )
    unconfirmed = deepcopy(next_)
    unconfirmed["status"] = "awaiting_learner_review"
    with pytest.raises(ValueError, match="confirmed_authentic"):
        compare_confirmed_attempts(prior, unconfirmed, rubric)
    other_learner = deepcopy(next_)
    other_learner["learner_id"] = "other-learner"
    with pytest.raises(ValueError, match="learner_mismatch"):
        compare_confirmed_attempts(prior, other_learner, rubric)
    other_recurrence = deepcopy(next_)
    other_recurrence["context"]["recurrence_id"] = "other-recurrence"
    with pytest.raises(ValueError, match="recurrence_or_rubric"):
        compare_confirmed_attempts(prior, other_recurrence, rubric)
    with pytest.raises(ValueError, match="order_invalid"):
        compare_confirmed_attempts(next_, prior, rubric)


def test_progress_records_skill_projection_once_and_separates_rehearsal_history(
    attempt_document: dict[str, Any], rubric: Rubric
) -> None:
    prior = confirmed_attempt(
        attempt_document,
        attempt_id="attempt-ledger-prior",
        recorded_at=datetime(2026, 10, 22, 15, tzinfo=UTC),
        confirmation=CriterionResult.NOT_SATISFIED,
    )
    next_ = confirmed_attempt(
        attempt_document,
        attempt_id="attempt-ledger-next",
        recorded_at=datetime(2026, 10, 24, 15, tzinfo=UTC),
        confirmation=CriterionResult.SATISFIED,
    )
    service = AuthenticProgressService(rubric)
    first_skills = {"confirmation": SkillState(beta=2, authentic_attempt_count=1)}
    second_skills = {"confirmation": SkillState(alpha=2, beta=2, authentic_attempt_count=2)}
    first = service.record_review(review_result(prior, first_skills))
    second = service.record_review(review_result(next_, second_skills))
    assert first.comparison is None
    assert second.comparison is not None
    assert second.comparison.functional_progress is FunctionalProgress.IMPROVED
    before_duplicate = service.snapshot()
    duplicate = service.record_review(review_result(next_, second_skills))
    assert isinstance(duplicate, ProgressRecord)
    assert duplicate.duplicate is True
    assert duplicate.skill_states == before_duplicate.skill_states
    assert len(service.snapshot().comparisons) == 1
    service.record_rehearsal(
        {
            "rehearsal_id": "rehearsal-fixture-1",
            "recurrence_id": prior["context"]["recurrence_id"],
            "target_function": "confirmation",
            "state": "completed",
            "completed_at": "2026-10-23T14:00:00Z",
        }
    )
    snapshot = service.snapshot()
    assert snapshot.skill_states == second_skills
    assert len(snapshot.authentic_attempts) == 2
    assert len(snapshot.rehearsal_history) == 1
    assert snapshot.functional_wins[0].criterion_id == "appointment_confirmed"
    assert snapshot.causal_claim is False
    assert snapshot.gamified_streaks is False


def test_progress_replay_conflicts_and_out_of_order_attempts_fail_closed(
    attempt_document: dict[str, Any], rubric: Rubric
) -> None:
    prior = confirmed_attempt(
        attempt_document,
        attempt_id="attempt-order-prior",
        recorded_at=datetime(2026, 10, 22, 15, tzinfo=UTC),
        confirmation=CriterionResult.NOT_SATISFIED,
    )
    service = AuthenticProgressService(rubric)
    result = review_result(prior, {})
    service.record_review(result)
    other_learner = deepcopy(prior)
    other_learner["attempt_id"] = "attempt-other-learner"
    other_learner["learner_id"] = "other-learner"
    with pytest.raises(ValueError, match="learner_scope_mismatch"):
        service.record_review(review_result(other_learner, {}))
    changed = deepcopy(prior)
    changed["outcome"]["criteria"][0]["self_evidence_excerpt"] = "Changed evidence"
    with pytest.raises(ValueError, match="replay_conflict"):
        service.record_review(review_result(changed, {}))
    earlier = confirmed_attempt(
        attempt_document,
        attempt_id="attempt-order-earlier",
        recorded_at=datetime(2026, 10, 21, 15, tzinfo=UTC),
        confirmation=CriterionResult.SATISFIED,
    )
    with pytest.raises(ValueError, match="in_order"):
        service.record_review(review_result(earlier, {}))
    with pytest.raises(ValueError, match="rehearsal_history_metadata"):
        service.record_rehearsal({"rehearsal_id": "r1", "transcript": "not allowed"})
