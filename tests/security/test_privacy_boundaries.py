from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from nexttime_domain import (
    BreakdownCode,
    BreakdownValidationError,
    RawBreakdownCandidate,
    RawExtractionPayload,
    Rubric,
    SelfExcerpt,
    SensitiveContentError,
    ensure_non_sensitive_excerpts,
    validate_minimized_attempt,
    validate_raw_extraction,
)
from nexttime_domain.transcripts import CaptureMode, MinimizedTranscript, Situation


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


def test_sensitive_topics_and_prohibited_identity_inferences_fail_closed(
    rubric: Rubric,
) -> None:
    for sensitive_text in (
        "Mijn advocaat belt over de rechtbank.",
        "De verblijfsvergunning bij immigration is afgewezen.",
        "Mijn bankrekening en IBAN staan rood bij schuldsanering.",
    ):
        with pytest.raises(SensitiveContentError):
            ensure_non_sensitive_excerpts((SelfExcerpt(sensitive_text, 0, 1500, 0.9),))

    recorded = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    minimized = MinimizedTranscript(
        source_reference="sec-ref-001",
        learner_id="01000000000000000000000002",
        recorded_at=recorded,
        situation=Situation.REPAIR,
        capture_mode=CaptureMode.CONSENTED_CONVERSATION,
        self_excerpts=(SelfExcerpt("Ja, dank u.", 0, 1200, 0.9),),
        asr_confidence=0.9,
        status="review_required",
        reason_codes=("self_excerpt_review_required",),
        other_speaker_transcript_retained=False,
        transient_source_delete_after=recorded + timedelta(hours=24),
        provenance="fixture",
    )
    with pytest.raises(BreakdownValidationError, match="prohibited_inference_or_copy"):
        validate_raw_extraction(
            minimized,
            rubric,
            RawExtractionPayload(
                criteria=(),
                breakdowns=(
                    RawBreakdownCandidate(
                        code=BreakdownCode.UNKNOWN,
                        description="Inferred emotional state and ethnicity from speech.",
                        excerpt_index=0,
                        self_evidence_excerpt="Ja, dank u.",
                        model_confidence=0.8,
                    ),
                ),
                model_version="test-v1",
            ),
            timestamp=recorded,
        )
