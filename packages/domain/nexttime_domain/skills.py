from .models import CriterionResult, SkillState


def update_skill_from_authentic_attempt(
    state: SkillState,
    result: CriterionResult,
    *,
    confirmed_authentic_attempt: bool,
    rejected_breakdown: bool = False,
) -> SkillState:
    if not confirmed_authentic_attempt or rejected_breakdown or result is CriterionResult.UNKNOWN:
        return state
    if result is CriterionResult.SATISFIED:
        return SkillState(state.alpha + 1, state.beta, state.authentic_attempt_count + 1)
    return SkillState(state.alpha, state.beta + 1, state.authentic_attempt_count + 1)


def apply_partial_authentic_evidence(
    state: SkillState, *, confirmed_authentic_attempt: bool
) -> SkillState:
    if not confirmed_authentic_attempt:
        return state
    return SkillState(state.alpha + 0.5, state.beta + 0.5, state.authentic_attempt_count + 1)
