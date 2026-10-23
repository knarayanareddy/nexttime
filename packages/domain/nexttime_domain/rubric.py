from __future__ import annotations

from collections.abc import Mapping

from .models import (
    AttemptEvaluationInput,
    CriterionResult,
    OutcomeEvaluation,
    OutcomeStatus,
    Rubric,
    Situation,
)


def rubric_from_mapping(document: Mapping[str, object]) -> Rubric:
    version = document.get("version")
    raw_situations = document.get("situations")
    if not isinstance(version, str) or not isinstance(raw_situations, dict):
        raise ValueError("Rubric requires a version and situation mapping")

    required: dict[Situation, tuple[str, ...]] = {}
    optional: dict[Situation, tuple[str, ...]] = {}
    for situation in Situation:
        raw = raw_situations.get(situation.value)
        if not isinstance(raw, dict):
            raise ValueError(f"Missing rubric for {situation.value}")
        required_values = raw.get("required")
        optional_values = raw.get("optional")
        if not isinstance(required_values, list) or not all(
            isinstance(item, str) for item in required_values
        ):
            raise ValueError(f"Invalid required criteria for {situation.value}")
        if not isinstance(optional_values, list) or not all(
            isinstance(item, str) for item in optional_values
        ):
            raise ValueError(f"Invalid optional criteria for {situation.value}")
        required[situation] = tuple(required_values)
        optional[situation] = tuple(optional_values)
    return Rubric(version=version, required=required, optional=optional)


def evaluate_authentic_attempt(
    rubric: Rubric, attempt: AttemptEvaluationInput
) -> OutcomeEvaluation:
    if not 0.0 <= attempt.asr_confidence <= 1.0:
        raise ValueError("ASR confidence must be between zero and one")
    if not attempt.minimization_complete:
        raise ValueError("Only minimized attempts may be evaluated")

    uncertainty_reasons: list[str] = []
    if attempt.asr_confidence < 0.75:
        uncertainty_reasons.append("asr_confidence_below_0_75")
    if attempt.learner_disputed:
        uncertainty_reasons.append("learner_disputed_extraction")
    if attempt.diarization_ambiguous:
        uncertainty_reasons.append("diarization_ambiguous")
    if uncertainty_reasons:
        return OutcomeEvaluation(OutcomeStatus.UNKNOWN, True, tuple(uncertainty_reasons))

    required_ids = rubric.required[attempt.situation]
    allowed_ids = set(required_ids) | set(rubric.optional[attempt.situation])
    if set(attempt.criteria) - allowed_ids:
        raise ValueError("Attempt contains a criterion outside the selected situation rubric")

    for criterion_id in required_ids:
        evidence = attempt.criteria.get(criterion_id)
        if evidence is None:
            return OutcomeEvaluation(OutcomeStatus.UNKNOWN, True, ("required_criterion_missing",))
        if evidence.result is not CriterionResult.UNKNOWN and not evidence.evidence_present:
            return OutcomeEvaluation(OutcomeStatus.UNKNOWN, True, ("required_evidence_missing",))

    if attempt.assistance_used:
        return OutcomeEvaluation(OutcomeStatus.PARTIAL, True, ("explicit_assistance_used",))

    required = [attempt.criteria[criterion_id] for criterion_id in required_ids]
    if all(item.result is CriterionResult.SATISFIED for item in required):
        return OutcomeEvaluation(OutcomeStatus.SUCCESS, False, ("all_required_criteria_satisfied",))

    if any(item.result is CriterionResult.NOT_SATISFIED for item in required):
        if attempt.interaction_ended:
            return OutcomeEvaluation(
                OutcomeStatus.FAILED, False, ("required_criterion_not_satisfied",)
            )
        return OutcomeEvaluation(OutcomeStatus.UNKNOWN, True, ("interaction_still_open",))

    goal = attempt.criteria.get("goal_stated")
    if goal and goal.result is CriterionResult.SATISFIED:
        return OutcomeEvaluation(OutcomeStatus.PARTIAL, True, ("required_confirmation_unresolved",))
    return OutcomeEvaluation(OutcomeStatus.UNKNOWN, True, ("outcome_evidence_inconclusive",))
