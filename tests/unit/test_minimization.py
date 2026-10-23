from copy import deepcopy
from typing import Any

from nexttime_domain.minimization import (
    find_prohibited_transcript_fields,
    validate_minimized_attempt,
)


def test_minimized_example_and_recursive_scan(attempt_document: dict[str, Any]) -> None:
    assert validate_minimized_attempt(attempt_document) == []
    assert find_prohibited_transcript_fields(attempt_document) == []
    invalid = deepcopy(attempt_document)
    invalid["items"] = [{"other_speaker_transcript_raw": "not allowed"}]
    assert find_prohibited_transcript_fields(invalid) == ["$.items[0].other_speaker_transcript_raw"]


def test_minimization_semantic_failures(attempt_document: dict[str, Any]) -> None:
    invalid = deepcopy(attempt_document)
    invalid["processing"]["other_speaker_transcript_retained"] = True
    invalid["consent"]["revoked"] = True
    invalid["status"] = "confirmed"
    invalid["processing"]["redaction_status"] = "pending"
    invalid["retention"]["transient_source_delete_after"] = "2026-10-24T15:00:00.000Z"
    assert validate_minimized_attempt(invalid) == [
        "confirmed_attempt_requires_learner_confirmation",
        "confirmed_attempt_requires_minimization",
        "other_speaker_transcript_retained",
        "revoked_consent_requires_deletion",
        "transient_retention_exceeds_24_hours",
    ]


def test_deletion_cannot_precede_recording(attempt_document: dict[str, Any]) -> None:
    invalid = deepcopy(attempt_document)
    invalid["retention"]["transient_source_delete_after"] = "2026-10-22T14:00:00.000Z"
    assert validate_minimized_attempt(invalid) == ["transient_deletion_precedes_recording"]
