import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from nexttime_domain.minimization import (
    find_prohibited_transcript_fields,
    validate_minimized_attempt,
)

ROOT = Path(__file__).resolve().parents[2]


def load_json(relative: str) -> dict[str, Any]:
    return json.loads((ROOT / relative).read_text())


def validator() -> Draft202012Validator:
    schema = load_json("schemas/real-world-attempt.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_packaged_schema_is_canonical() -> None:
    assert (ROOT / "packages/contracts/real-world-attempt.schema.json").read_bytes() == (
        ROOT / "schemas/real-world-attempt.schema.json"
    ).read_bytes()


def test_example_is_valid_minimized_and_round_trips(attempt_document: dict[str, Any]) -> None:
    validator().validate(attempt_document)
    assert validate_minimized_attempt(attempt_document) == []
    assert find_prohibited_transcript_fields(attempt_document) == []
    assert json.loads(json.dumps(attempt_document)) == attempt_document


def test_schema_rejects_other_speaker_retention(attempt_document: dict[str, Any]) -> None:
    invalid = deepcopy(attempt_document)
    invalid["processing"]["other_speaker_transcript_retained"] = True
    assert not validator().is_valid(invalid)


def test_revocation_and_confirmation_have_semantic_guards(
    attempt_document: dict[str, Any],
) -> None:
    revoked = deepcopy(attempt_document)
    revoked["consent"]["revoked"] = True
    assert "revoked_consent_requires_deletion" in validate_minimized_attempt(revoked)

    confirmed = deepcopy(attempt_document)
    confirmed["status"] = "confirmed"
    issues = validate_minimized_attempt(confirmed)
    assert "confirmed_attempt_requires_learner_confirmation" in issues


def test_transient_source_cannot_exceed_24_hours(attempt_document: dict[str, Any]) -> None:
    invalid = deepcopy(attempt_document)
    invalid["retention"]["transient_source_delete_after"] = "2026-10-24T15:00:00.000Z"
    assert "transient_retention_exceeds_24_hours" in validate_minimized_attempt(invalid)


def test_prohibited_nested_transcript_fields_are_found(
    attempt_document: dict[str, Any],
) -> None:
    invalid = deepcopy(attempt_document)
    invalid["nested"] = {"other_speaker_transcript_turns": ["not allowed"]}
    assert find_prohibited_transcript_fields(invalid) == ["$.nested.other_speaker_transcript_turns"]
    with pytest.raises(ValidationError):
        validator().validate(invalid)
