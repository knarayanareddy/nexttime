from dataclasses import replace

import pytest
from conftest import successful_input
from nexttime_domain import (
    CriterionEvidence,
    CriterionResult,
    OutcomeStatus,
    Rubric,
    Situation,
    evaluate_authentic_attempt,
)


@pytest.mark.parametrize("situation", list(Situation))
def test_sufficient_required_evidence_is_success(rubric: Rubric, situation: Situation) -> None:
    result = evaluate_authentic_attempt(rubric, successful_input(situation))
    assert result.status is OutcomeStatus.SUCCESS
    assert result.confirmation_required is False


def test_low_confidence_remains_unknown(rubric: Rubric) -> None:
    result = evaluate_authentic_attempt(rubric, replace(successful_input(), asr_confidence=0.74))
    assert result.status is OutcomeStatus.UNKNOWN
    assert result.reason_codes == ("asr_confidence_below_0_75",)
    assert result.confirmation_required


def test_goal_without_confirmation_is_partial(rubric: Rubric) -> None:
    attempt = successful_input()
    attempt.criteria["appointment_confirmed"] = CriterionEvidence(CriterionResult.UNKNOWN, False)
    result = evaluate_authentic_attempt(rubric, attempt)
    assert result.status is OutcomeStatus.PARTIAL
    assert result.reason_codes == ("required_confirmation_unresolved",)


def test_ended_attempt_with_failed_required_criterion_fails(rubric: Rubric) -> None:
    attempt = successful_input()
    attempt.criteria["appointment_confirmed"] = CriterionEvidence(
        CriterionResult.NOT_SATISFIED, True
    )
    assert evaluate_authentic_attempt(rubric, attempt).status is OutcomeStatus.FAILED


def test_open_attempt_cannot_be_final_failure(rubric: Rubric) -> None:
    attempt = replace(successful_input(), interaction_ended=False)
    attempt.criteria["appointment_confirmed"] = CriterionEvidence(
        CriterionResult.NOT_SATISFIED, True
    )
    result = evaluate_authentic_attempt(rubric, attempt)
    assert result.status is OutcomeStatus.UNKNOWN
    assert result.reason_codes == ("interaction_still_open",)


def test_assistance_is_partial_not_independent_success(rubric: Rubric) -> None:
    result = evaluate_authentic_attempt(rubric, replace(successful_input(), assistance_used=True))
    assert result.status is OutcomeStatus.PARTIAL
    assert result.reason_codes == ("explicit_assistance_used",)


def test_missing_required_evidence_is_unknown(rubric: Rubric) -> None:
    attempt = successful_input()
    attempt.criteria["appointment_confirmed"] = CriterionEvidence(CriterionResult.SATISFIED, False)
    result = evaluate_authentic_attempt(rubric, attempt)
    assert result.status is OutcomeStatus.UNKNOWN
    assert result.reason_codes == ("required_evidence_missing",)


def test_disputed_and_ambiguous_sources_require_review(rubric: Rubric) -> None:
    disputed = evaluate_authentic_attempt(
        rubric, replace(successful_input(), learner_disputed=True)
    )
    ambiguous = evaluate_authentic_attempt(
        rubric, replace(successful_input(), diarization_ambiguous=True)
    )
    assert disputed.status is OutcomeStatus.UNKNOWN
    assert ambiguous.status is OutcomeStatus.UNKNOWN


def test_invalid_inputs_fail_closed(rubric: Rubric) -> None:
    with pytest.raises(ValueError, match="ASR confidence"):
        evaluate_authentic_attempt(rubric, replace(successful_input(), asr_confidence=1.1))
    with pytest.raises(ValueError, match="minimized"):
        evaluate_authentic_attempt(rubric, replace(successful_input(), minimization_complete=False))
    attempt = successful_input()
    attempt.criteria["unsupported"] = CriterionEvidence(CriterionResult.UNKNOWN, False)
    with pytest.raises(ValueError, match="outside"):
        evaluate_authentic_attempt(rubric, attempt)
