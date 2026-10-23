import json
from pathlib import Path
from typing import Any

import pytest
from nexttime_domain import (
    AttemptEvaluationInput,
    CriterionEvidence,
    CriterionResult,
    Rubric,
    Situation,
    rubric_from_mapping,
)

ROOT = Path(__file__).resolve().parents[1]


def load_json(relative: str) -> dict[str, Any]:
    return json.loads((ROOT / relative).read_text())


@pytest.fixture
def attempt_document() -> dict[str, Any]:
    return load_json("examples/real-world-attempt.example.json")


@pytest.fixture
def rubric() -> Rubric:
    return rubric_from_mapping(load_json("packages/rubrics/rubric_v1.json"))


def successful_input(situation: Situation = Situation.REPAIR) -> AttemptEvaluationInput:
    confirmation = (
        "next_step_confirmed" if situation is Situation.SHIFT else "appointment_confirmed"
    )
    return AttemptEvaluationInput(
        situation=situation,
        criteria={
            "goal_stated": CriterionEvidence(CriterionResult.SATISFIED, True),
            confirmation: CriterionEvidence(CriterionResult.SATISFIED, True),
        },
        asr_confidence=0.95,
        minimization_complete=True,
        interaction_ended=True,
    )
