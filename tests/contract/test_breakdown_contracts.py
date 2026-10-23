from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from nexttime_domain import (
    BreakdownProposalService,
    CaptureMode,
    DeterministicBreakdownExtractor,
    Rubric,
    Situation,
    SourceTranscript,
    Speaker,
    TranscriptTurn,
    minimize_transcript,
    validate_minimized_attempt,
)
from nexttime_domain.minimization import find_prohibited_transcript_fields
from nexttime_model_gateway import (
    GatewayBreakdownExtractor,
    serialize_minimized_for_gateway,
    serialize_raw_extraction_payload,
    validate_breakdown_input_payload,
    validate_breakdown_output_payload,
)

ROOT = Path(__file__).resolve().parents[2]
LEARNER_ID = "01000000000000000000000002"
CONSENT_ID = "01000000000000000000000004"
RECURRENCE_ID = "01000000000000000000000003"
RECORDED_AT = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)


def load_schema(relative_path: str) -> Draft202012Validator:
    schema = json.loads((ROOT / relative_path).read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_gateway_input_and_output_schemas_match_runtime_validators(rubric: Rubric) -> None:
    input_validator = load_schema("packages/contracts/breakdown-proposal-input.schema.json")
    output_validator = load_schema("packages/contracts/breakdown-proposal-output.schema.json")

    source = SourceTranscript(
        reference="contract-bee-ref-001",
        learner_id=LEARNER_ID,
        recorded_at=RECORDED_AT,
        capture_mode=CaptureMode.CONSENTED_CONVERSATION,
        situation=Situation.REPAIR,
        turns=(
            TranscriptTurn(
                Speaker.LEARNER,
                "Ik wil een afspraak voor de reparatie.",
                0,
                1600,
                0.91,
            ),
            TranscriptTurn(Speaker.LEARNER, "Ja, dank u.", 2000, 3100, 0.88),
        ),
        consent_attested=True,
        all_participants_consented=True,
        revoked=False,
        retention_policy_present=True,
    )
    minimized = minimize_transcript(source, minimized_at=RECORDED_AT + timedelta(minutes=1))
    gateway_input = serialize_minimized_for_gateway(minimized, rubric)
    input_validator.validate(gateway_input)
    assert validate_breakdown_input_payload(gateway_input) == []

    smuggled_input = deepcopy(gateway_input)
    smuggled_input["other_speaker_turns"] = ["Verboden tekst"]
    assert validate_breakdown_input_payload(smuggled_input) != []
    with pytest.raises(ValidationError):
        input_validator.validate(smuggled_input)

    raw_output = DeterministicBreakdownExtractor().extract(
        minimized, rubric, correlation_id="contract-corr-001"
    )
    serialized_output = serialize_raw_extraction_payload(raw_output)
    output_validator.validate(serialized_output)
    assert validate_breakdown_output_payload(serialized_output) == []

    too_many_output = deepcopy(serialized_output)
    too_many_output["breakdowns"] = too_many_output["breakdowns"] * 4
    assert "too_many_breakdowns" in validate_breakdown_output_payload(too_many_output)
    with pytest.raises(ValidationError):
        output_validator.validate(too_many_output)

    cot_output = deepcopy(serialized_output)
    cot_output["chain_of_thought"] = "not allowed"
    assert validate_breakdown_output_payload(cot_output) != []
    with pytest.raises(ValidationError):
        output_validator.validate(cot_output)


@pytest.mark.parametrize(
    ("situation", "capture_mode", "device_type", "provenance", "turns"),
    [
        (
            Situation.REPAIR,
            CaptureMode.CONSENTED_CONVERSATION,
            "bee_device",
            "fixture",
            (
                TranscriptTurn(
                    Speaker.LEARNER,
                    "Ik wil een afspraak voor de reparatie.",
                    0,
                    1500,
                    0.91,
                ),
                TranscriptTurn(Speaker.LEARNER, "Ja, dank u.", 1800, 2900, 0.89),
            ),
        ),
        (
            Situation.SCHOOL_ADMIN,
            CaptureMode.SELF_MEMO,
            "apple_watch_bee",
            "live",
            (
                TranscriptTurn(
                    Speaker.LEARNER,
                    (
                        "Ik wil een afspraak met de schooladministratie en "
                        "ik bevestig maandag om negen uur."
                    ),
                    0,
                    3200,
                    0.93,
                ),
            ),
        ),
        (
            Situation.SHIFT,
            CaptureMode.CONSENTED_CONVERSATION,
            "apple_watch_bee",
            "fixture",
            (
                TranscriptTurn(
                    Speaker.LEARNER,
                    "Sorry English please, ik snap het niet.",
                    0,
                    1400,
                    0.87,
                ),
                TranscriptTurn(
                    Speaker.LEARNER,
                    "Uhm hoe laat ook alweer?",
                    1500,
                    2800,
                    0.86,
                ),
                TranscriptTurn(
                    Speaker.LEARNER,
                    "Laat maar zitten, ik moet nu ophangen.",
                    3000,
                    4600,
                    0.88,
                ),
            ),
        ),
    ],
)
def test_proposed_attempt_documents_conform_to_canonical_attempt_schema(
    rubric: Rubric,
    situation: Situation,
    capture_mode: CaptureMode,
    device_type: str,
    provenance: str,
    turns: tuple[TranscriptTurn, ...],
) -> None:
    attempt_validator = load_schema("schemas/real-world-attempt.schema.json")
    source = SourceTranscript(
        reference=f"contract-{situation.value}-{capture_mode.value}",
        learner_id=LEARNER_ID,
        recorded_at=RECORDED_AT,
        capture_mode=capture_mode,
        situation=situation,
        turns=turns,
        consent_attested=True,
        all_participants_consented=(capture_mode is CaptureMode.CONSENTED_CONVERSATION),
        revoked=False,
        retention_policy_present=True,
        provenance=cast(Any, provenance),
    )
    minimized = minimize_transcript(source, minimized_at=RECORDED_AT + timedelta(minutes=2))
    service = BreakdownProposalService(rubric, extractor=GatewayBreakdownExtractor())
    proposal = service.propose(
        minimized,
        consent_id=CONSENT_ID,
        consent_attested_at=RECORDED_AT - timedelta(minutes=1),
        all_participants_consented=(capture_mode is CaptureMode.CONSENTED_CONVERSATION),
        learner_goal="Complete the recurring Dutch communication task.",
        device_type=cast(Any, device_type),
        recurrence_id=RECURRENCE_ID,
        proposed_at=RECORDED_AT + timedelta(minutes=5),
    )

    assert len(proposal.breakdowns) <= 3
    attempt_validator.validate(proposal.attempt_document)
    assert validate_minimized_attempt(proposal.attempt_document) == []
    assert find_prohibited_transcript_fields(proposal.attempt_document) == []
    assert json.loads(json.dumps(proposal.attempt_document)) == proposal.attempt_document
