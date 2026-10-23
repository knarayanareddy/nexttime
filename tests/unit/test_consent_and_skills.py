import pytest
from nexttime_domain import (
    ConsentError,
    CriterionResult,
    SkillState,
    apply_partial_authentic_evidence,
    require_processing_consent,
    update_skill_from_authentic_attempt,
)


def test_consent_and_retention_are_processing_prerequisites() -> None:
    require_processing_consent(consent_attested=True, revoked=False, retention_policy_present=True)
    for kwargs in [
        {"consent_attested": False, "revoked": False, "retention_policy_present": True},
        {"consent_attested": True, "revoked": True, "retention_policy_present": True},
        {"consent_attested": True, "revoked": False, "retention_policy_present": False},
    ]:
        with pytest.raises(ConsentError):
            require_processing_consent(**kwargs)


def test_only_confirmed_authentic_attempts_change_skill_state() -> None:
    initial = SkillState()
    unchanged = update_skill_from_authentic_attempt(
        initial, CriterionResult.SATISFIED, confirmed_authentic_attempt=False
    )
    assert unchanged == initial
    assert unchanged.mastery == 0.5

    satisfied = update_skill_from_authentic_attempt(
        initial, CriterionResult.SATISFIED, confirmed_authentic_attempt=True
    )
    assert satisfied.alpha == 2
    assert satisfied.authentic_attempt_count == 1

    not_satisfied = update_skill_from_authentic_attempt(
        initial, CriterionResult.NOT_SATISFIED, confirmed_authentic_attempt=True
    )
    assert not_satisfied.beta == 2

    partial = apply_partial_authentic_evidence(initial, confirmed_authentic_attempt=True)
    assert partial.alpha == 1.5
    assert partial.beta == 1.5
    assert partial.mastery == 0.5


def test_unknown_and_rejected_evidence_never_update_skill() -> None:
    initial = SkillState()
    assert (
        update_skill_from_authentic_attempt(
            initial, CriterionResult.UNKNOWN, confirmed_authentic_attempt=True
        )
        == initial
    )
    assert (
        update_skill_from_authentic_attempt(
            initial,
            CriterionResult.SATISFIED,
            confirmed_authentic_attempt=True,
            rejected_breakdown=True,
        )
        == initial
    )
    assert apply_partial_authentic_evidence(initial, confirmed_authentic_attempt=False) == initial
