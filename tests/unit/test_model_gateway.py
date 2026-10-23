from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from nexttime_domain import (
    BreakdownCode,
    BreakdownProposalService,
    BreakdownValidationError,
    CaptureMode,
    MinimizedTranscript,
    Rubric,
    Situation,
    SourceTranscript,
    Speaker,
    TranscriptTurn,
    minimize_transcript,
)
from nexttime_model_gateway import (
    GatewayBreakdownExtractor,
    ModelGateway,
    ModelGatewayError,
    ModelGatewayRequest,
    ModelPolicy,
    ProviderInvocationRequest,
    ProviderInvocationResponse,
    TokenUsage,
    validate_breakdown_input_payload,
    validate_breakdown_output_payload,
)

LEARNER_ID = "01000000000000000000000002"
CONSENT_ID = "01000000000000000000000004"
RECORDED_AT = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)


class SequencedProvider:
    def __init__(self, steps: Sequence[ProviderInvocationResponse | Exception]) -> None:
        self._steps = list(steps)
        self.requests: list[ProviderInvocationRequest] = []

    def invoke(self, request: ProviderInvocationRequest) -> ProviderInvocationResponse:
        self.requests.append(request)
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def sample_minimized() -> MinimizedTranscript:
    source = SourceTranscript(
        reference="fixture-gateway-ref-001",
        learner_id=LEARNER_ID,
        recorded_at=RECORDED_AT,
        capture_mode=CaptureMode.CONSENTED_CONVERSATION,
        situation=Situation.REPAIR,
        turns=(
            TranscriptTurn(
                Speaker.LEARNER,
                "Ik wil een afspraak voor de reparatie via learner@example.test.",
                0,
                1800,
                0.91,
            ),
            TranscriptTurn(
                Speaker.OTHER,
                "Bystander sentence that must never reach the gateway.",
                1800,
                3200,
                0.95,
            ),
            TranscriptTurn(
                Speaker.LEARNER,
                "Ja, dank u.",
                3200,
                4200,
                0.90,
            ),
        ),
        consent_attested=True,
        all_participants_consented=True,
        revoked=False,
        retention_policy_present=True,
    )
    return minimize_transcript(source, minimized_at=RECORDED_AT + timedelta(minutes=1))


def valid_provider_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "criteria": [
            {
                "criterion_id": "goal_stated",
                "result": "satisfied",
                "excerpt_index": 0,
                "self_evidence_excerpt": "Ik wil een afspraak voor de reparatie",
                "model_confidence": 0.90,
            },
            {
                "criterion_id": "appointment_confirmed",
                "result": "unknown",
                "excerpt_index": None,
                "self_evidence_excerpt": "",
                "model_confidence": 0.70,
            },
        ],
        "breakdowns": [
            {
                "code": "confirmation_missing",
                "description": "The proposed appointment time was not repeated back in Dutch.",
                "excerpt_index": 1,
                "self_evidence_excerpt": "Ja, dank u.",
                "model_confidence": 0.83,
            }
        ],
    }


def provider_response(
    payload: dict[str, Any],
    *,
    latency_ms: int = 220,
    input_tokens: int = 180,
    output_tokens: int = 95,
    cost_usd: float = 0.002,
) -> ProviderInvocationResponse:
    return ProviderInvocationResponse(
        payload=payload,
        provider_request_id="req-fixture-001",
        model_id="bedrock.claude-haiku-fixture",
        latency_ms=latency_ms,
        token_usage=TokenUsage(input_tokens, output_tokens, cost_usd),
    )


def test_gateway_happy_path_extracts_bounded_proposal_without_leaking_pii(
    rubric: Rubric,
) -> None:
    provider = SequencedProvider([provider_response(valid_provider_payload())])
    adapter = GatewayBreakdownExtractor(ModelGateway(provider))
    service = BreakdownProposalService(rubric, extractor=adapter)

    result = service.propose(
        sample_minimized(),
        consent_id=CONSENT_ID,
        consent_attested_at=RECORDED_AT - timedelta(minutes=1),
        all_participants_consented=True,
        learner_goal="Confirm repair time.",
        proposed_at=RECORDED_AT + timedelta(minutes=5),
    )

    assert result.fallback_used is False
    assert result.fallback_reason is None
    assert result.model_version == "bedrock.claude-haiku-fixture:breakdown-propose-prompt-v1"
    assert len(result.breakdowns) == 1
    assert result.breakdowns[0].code is BreakdownCode.CONFIRMATION_MISSING
    assert result.breakdowns[0].confidence == 0.83

    sent_payload = provider.requests[0].payload
    assert "Bystander" not in repr(sent_payload)
    assert "learner@example.test" not in repr(sent_payload)
    assert "[redacted]" in sent_payload["self_excerpts"][0]["text"]

    telemetry = adapter.telemetry_events[0]
    assert telemetry.fallback_used is False
    assert telemetry.attempts == 1
    assert "afspraak" not in repr(telemetry)
    assert "Ja, dank u" not in repr(telemetry)


def test_gateway_repairs_once_after_too_many_breakdowns_or_chain_of_thought(
    rubric: Rubric,
) -> None:
    bad_payload = valid_provider_payload()
    bad_payload["breakdowns"] = bad_payload["breakdowns"] * 4
    provider = SequencedProvider(
        [
            provider_response(bad_payload),
            provider_response(valid_provider_payload()),
        ]
    )
    adapter = GatewayBreakdownExtractor(ModelGateway(provider))
    raw = adapter.extract(sample_minimized(), rubric, correlation_id="corr-repair-1")

    assert raw.fallback_used is False
    assert len(provider.requests) == 2
    assert provider.requests[1].repair_feedback == "schema_repair:too_many_breakdowns"
    assert adapter.telemetry_events[0].attempts == 2

    cot_payload = valid_provider_payload()
    cot_payload["chain_of_thought"] = "hidden reasoning"
    provider_cot = SequencedProvider(
        [
            provider_response(cot_payload),
            provider_response(valid_provider_payload()),
        ]
    )
    adapter_cot = GatewayBreakdownExtractor(ModelGateway(provider_cot))
    raw_cot = adapter_cot.extract(sample_minimized(), rubric, correlation_id="corr-cot")
    assert raw_cot.fallback_used is False
    assert provider_cot.requests[1].repair_feedback == "schema_repair:prohibited_output_field"


def test_gateway_falls_back_when_domain_validation_or_provider_fails(rubric: Rubric) -> None:
    hallucinated = valid_provider_payload()
    hallucinated["breakdowns"][0]["self_evidence_excerpt"] = "Hallucinated bystander quote"

    prohibited_copy = valid_provider_payload()
    prohibited_copy["breakdowns"][0]["description"] = "Speaker did not sound native-like."

    provider = SequencedProvider(
        [
            provider_response(hallucinated),
            provider_response(prohibited_copy),
        ]
    )
    adapter = GatewayBreakdownExtractor(ModelGateway(provider))
    raw = adapter.extract(sample_minimized(), rubric, correlation_id="corr-fallback-1")

    assert raw.fallback_used is True
    assert raw.fallback_reason == "output_validation_failed"
    assert raw.model_version == "deterministic-breakdown-v1"
    assert len(raw.breakdowns) == 1
    assert adapter.telemetry_events[0].fallback_used is True
    assert adapter.telemetry_events[0].error_code == "output_validation_failed"

    unconfigured_adapter = GatewayBreakdownExtractor()
    unconfigured_raw = unconfigured_adapter.extract(
        sample_minimized(), rubric, correlation_id="corr-unconfigured"
    )
    assert unconfigured_raw.fallback_used is True
    assert unconfigured_raw.fallback_reason == "provider_not_configured"
    assert unconfigured_adapter.telemetry_events[0].attempts == 1


def test_gateway_metric_limits_retryable_errors_and_circuit_breaker(rubric: Rubric) -> None:
    policy = ModelPolicy(
        timeout_ms=500,
        max_output_tokens=200,
        max_cost_usd=0.01,
        allow_repair_retry=True,
        circuit_breaker_threshold=2,
    )
    provider = SequencedProvider(
        [
            ModelGatewayError("transient_throttled", "Rate limited", retryable=True),
            provider_response(valid_provider_payload(), latency_ms=900),
            provider_response(valid_provider_payload()),
        ]
    )
    gateway = ModelGateway(provider)
    adapter = GatewayBreakdownExtractor(gateway, model_policy=policy)

    first = adapter.extract(sample_minimized(), rubric, correlation_id="corr-cb-1")
    assert first.fallback_used is True
    assert first.fallback_reason == "provider_timeout_exceeded"
    assert gateway.circuit_open is True

    tripped = adapter.extract(sample_minimized(), rubric, correlation_id="corr-cb-2")
    assert tripped.fallback_used is True
    assert tripped.fallback_reason == "circuit_breaker_open"
    assert len(provider.requests) == 2

    gateway.reset_circuit()
    assert gateway.circuit_open is False
    recovered = adapter.extract(sample_minimized(), rubric, correlation_id="corr-cb-3")
    assert recovered.fallback_used is False

    for bad_resp, expected_code in [
        (
            provider_response(valid_provider_payload(), output_tokens=250),
            "output_token_limit_exceeded",
        ),
        (provider_response(valid_provider_payload(), cost_usd=0.05), "cost_limit_exceeded"),
        (provider_response(valid_provider_payload(), latency_ms=-1), "invalid_provider_metrics"),
    ]:
        metric_gateway = ModelGateway(SequencedProvider([bad_resp]))
        metric_adapter = GatewayBreakdownExtractor(metric_gateway, model_policy=policy)
        metric_result = metric_adapter.extract(
            sample_minimized(), rubric, correlation_id=f"corr-{expected_code}"
        )
        assert metric_result.fallback_used is True
        assert metric_result.fallback_reason == expected_code


def test_gateway_without_fallback_and_input_validation_errors(rubric: Rubric) -> None:
    gateway = ModelGateway()
    no_fallback_response = gateway.generate(
        ModelGatewayRequest(
            operation_name="breakdown_propose.v1",
            prompt_version="v1",
            schema_version=1,
            payload={"ok": True},
            validate_input=lambda _p: [],
            validate_output=lambda _p: [],
            model_policy=ModelPolicy(allow_repair_retry=False),
            correlation_id="corr-no-fallback",
            deterministic_fallback=None,
        )
    )
    assert no_fallback_response.validated_payload is None
    assert no_fallback_response.failure is not None
    assert no_fallback_response.failure.code == "provider_not_configured"
    assert no_fallback_response.fallback_used is False

    with pytest.raises(ModelGatewayError, match="Operation, prompt version"):
        gateway.generate(
            ModelGatewayRequest(
                operation_name="",
                prompt_version="v1",
                schema_version=1,
                payload={},
                validate_input=lambda _p: [],
                validate_output=lambda _p: [],
                model_policy=ModelPolicy(),
                correlation_id="corr",
            )
        )
    with pytest.raises(ModelGatewayError, match="Model policy limits"):
        gateway.generate(
            ModelGatewayRequest(
                operation_name="op",
                prompt_version="v1",
                schema_version=1,
                payload={},
                validate_input=lambda _p: [],
                validate_output=lambda _p: [],
                model_policy=ModelPolicy(timeout_ms=0),
                correlation_id="corr",
            )
        )
    with pytest.raises(ModelGatewayError, match="Prohibited transcript"):
        gateway.generate(
            ModelGatewayRequest(
                operation_name="op",
                prompt_version="v1",
                schema_version=1,
                payload={"other_speaker_transcript_raw": "secret"},
                validate_input=lambda _p: [],
                validate_output=lambda _p: [],
                model_policy=ModelPolicy(),
                correlation_id="corr",
            )
        )
    with pytest.raises(ModelGatewayError, match="Invalid gateway input"):
        gateway.generate(
            ModelGatewayRequest(
                operation_name="op",
                prompt_version="v1",
                schema_version=1,
                payload={"schema_version": 2},
                validate_input=lambda _p: ["bad_version"],
                validate_output=lambda _p: [],
                model_policy=ModelPolicy(),
                correlation_id="corr",
            )
        )
    with pytest.raises(ModelGatewayError, match="Deterministic fallback violated"):
        gateway.generate(
            ModelGatewayRequest(
                operation_name="op",
                prompt_version="v1",
                schema_version=1,
                payload={},
                validate_input=lambda _p: [],
                validate_output=lambda _p: ["invalid_fallback"],
                model_policy=ModelPolicy(),
                correlation_id="corr",
                deterministic_fallback=lambda: {"bad": True},
            )
        )

    tampered = replace(
        sample_minimized(),
        other_speaker_transcript_retained=cast(Any, True),
    )
    with pytest.raises(BreakdownValidationError, match="other_speaker_transcript_retained"):
        GatewayBreakdownExtractor().extract(tampered, rubric, correlation_id="corr-tampered")


def test_input_and_output_validator_edge_branches() -> None:
    assert validate_breakdown_input_payload({"extra": 1}) == ["invalid_input_keys"]
    bad_input = {
        "schema_version": True,
        "situation": "unknown",
        "capture_mode": "covert",
        "rubric_version": "bad",
        "required_criteria": [],
        "optional_criteria": ["goal_stated", "goal_stated"],
        "asr_confidence": True,
        "other_speaker_transcript_retained": True,
        "self_excerpts": [],
    }
    issues = validate_breakdown_input_payload(bad_input)
    assert "invalid_schema_version" in issues
    assert "invalid_situation" in issues
    assert "invalid_capture_mode" in issues
    assert "invalid_rubric_version" in issues
    assert "invalid_required_criteria" in issues
    assert "invalid_optional_criteria" in issues
    assert "invalid_asr_confidence" in issues
    assert "other_speaker_transcript_retained" in issues
    assert "invalid_self_excerpts" in issues

    bad_excerpt_key = {**bad_input, "self_excerpts": [{"bad": 1}]}
    assert "invalid_self_excerpt_item" in validate_breakdown_input_payload(bad_excerpt_key)
    bad_excerpt_val = {
        **bad_input,
        "self_excerpts": [
            {"excerpt_index": -1, "text": "", "start_ms": 0, "end_ms": 0, "confidence": 2.0}
        ],
    }
    assert "invalid_self_excerpt_item" in validate_breakdown_input_payload(bad_excerpt_val)

    assert validate_breakdown_output_payload({"extra": 1}) == ["invalid_output_keys"]
    bad_out = {
        "schema_version": 2,
        "criteria": "not-a-list",
        "breakdowns": "not-a-list",
    }
    out_issues = validate_breakdown_output_payload(bad_out)
    assert "invalid_schema_version" in out_issues
    assert "invalid_criteria_collection" in out_issues
    assert "invalid_breakdowns_collection" in out_issues

    bad_items_out = {
        "schema_version": 1,
        "criteria": [{"bad": 1}],
        "breakdowns": [{"bad": 1}],
    }
    assert "invalid_criterion_item" in validate_breakdown_output_payload(bad_items_out)
    assert "invalid_breakdown_item" in validate_breakdown_output_payload(bad_items_out)

    bad_values_out = {
        "schema_version": 1,
        "criteria": [
            {
                "criterion_id": "goal_stated",
                "result": "satisfied",
                "excerpt_index": -1,
                "self_evidence_excerpt": "ok",
                "model_confidence": 0.9,
            }
        ],
        "breakdowns": [
            {
                "code": "confirmation_missing",
                "description": "",
                "excerpt_index": 0,
                "self_evidence_excerpt": "ok",
                "model_confidence": 0.9,
            }
        ],
    }
    assert "invalid_criterion_item" in validate_breakdown_output_payload(bad_values_out)
    assert "invalid_breakdown_item" in validate_breakdown_output_payload(bad_values_out)
