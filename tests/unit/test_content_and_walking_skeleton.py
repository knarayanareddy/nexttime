import json
from copy import deepcopy
from typing import Any

import pytest
from conftest import load_json, successful_input
from nexttime_domain import (
    CriterionEvidence,
    CriterionResult,
    OutcomeStatus,
    evaluate_authentic_attempt,
    rubric_from_mapping,
    walking_skeleton,
)


def test_rubric_content_rejects_missing_or_invalid_sections() -> None:
    with pytest.raises(ValueError, match="version and situation"):
        rubric_from_mapping({})

    document = load_json("packages/rubrics/rubric_v1.json")
    missing = deepcopy(document)
    del missing["situations"]["repair_appointment"]
    with pytest.raises(ValueError, match="Missing rubric"):
        rubric_from_mapping(missing)

    invalid_required = deepcopy(document)
    invalid_required["situations"]["repair_appointment"]["required"] = "goal_stated"
    with pytest.raises(ValueError, match="Invalid required"):
        rubric_from_mapping(invalid_required)

    invalid_optional = deepcopy(document)
    invalid_optional["situations"]["repair_appointment"]["optional"] = [1]
    with pytest.raises(ValueError, match="Invalid optional"):
        rubric_from_mapping(invalid_optional)


def test_missing_criterion_and_inconclusive_goal_remain_unknown() -> None:
    rubric = rubric_from_mapping(load_json("packages/rubrics/rubric_v1.json"))
    missing = successful_input()
    del missing.criteria["appointment_confirmed"]
    assert evaluate_authentic_attempt(rubric, missing).reason_codes == (
        "required_criterion_missing",
    )

    inconclusive = successful_input()
    for key in inconclusive.criteria:
        inconclusive.criteria[key] = CriterionEvidence(CriterionResult.UNKNOWN, False)
    result = evaluate_authentic_attempt(rubric, inconclusive)
    assert result.status is OutcomeStatus.UNKNOWN
    assert result.reason_codes == ("outcome_evidence_inconclusive",)


def test_walking_skeleton_is_labeled_and_does_not_update_skill(capsys: Any) -> None:
    walking_skeleton.main()
    output = json.loads(capsys.readouterr().out)
    assert "FIXTURE ONLY" in output["mode"]
    assert output["outcome"] == "partial"
    assert output["authentic_skill_update"] is False


def test_walking_skeleton_rejects_invalid_fixture(monkeypatch: Any) -> None:
    monkeypatch.setattr(walking_skeleton, "validate_minimized_attempt", lambda _document: ["bad"])
    with pytest.raises(ValueError, match="Invalid minimized fixture"):
        walking_skeleton.main()
