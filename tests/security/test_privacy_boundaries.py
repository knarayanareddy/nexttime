from copy import deepcopy
from typing import Any

from nexttime_domain.minimization import validate_minimized_attempt


def test_confirmed_records_must_be_minimized_and_confirmed(
    attempt_document: dict[str, Any],
) -> None:
    invalid = deepcopy(attempt_document)
    invalid["status"] = "confirmed"
    invalid["processing"]["redaction_status"] = "pending"
    issues = validate_minimized_attempt(invalid)
    assert "confirmed_attempt_requires_minimization" in issues
    assert "confirmed_attempt_requires_learner_confirmation" in issues


def test_retention_cannot_precede_source(attempt_document: dict[str, Any]) -> None:
    invalid = deepcopy(attempt_document)
    invalid["retention"]["transient_source_delete_after"] = "2026-10-22T14:00:00.000Z"
    assert "transient_deletion_precedes_recording" in validate_minimized_attempt(invalid)
