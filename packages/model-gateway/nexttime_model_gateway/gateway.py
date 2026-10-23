from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from nexttime_domain.minimization import find_prohibited_transcript_fields

_PROHIBITED_OUTPUT_KEYS = frozenset({"chain_of_thought", "reasoning", "raw_prompt"})


class ModelGatewayError(RuntimeError):
    """Typed failure raised by model gateway or provider adapters."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class GatewayFailure:
    code: str
    detail: str
    retryable: bool


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


@dataclass(frozen=True)
class ModelPolicy:
    timeout_ms: int = 4000
    max_output_tokens: int = 800
    max_cost_usd: float = 0.05
    allow_repair_retry: bool = True
    circuit_breaker_threshold: int = 3


@dataclass(frozen=True)
class ProviderInvocationRequest:
    operation_name: str
    prompt_version: str
    schema_version: int
    payload: Mapping[str, Any]
    correlation_id: str
    attempt: int
    repair_feedback: str | None = None
    timeout_ms: int = 4000
    max_output_tokens: int = 800


@dataclass(frozen=True)
class ProviderInvocationResponse:
    payload: Mapping[str, Any]
    provider_request_id: str
    model_id: str
    latency_ms: int
    token_usage: TokenUsage


class ModelProviderPort(Protocol):
    def invoke(self, request: ProviderInvocationRequest) -> ProviderInvocationResponse: ...


class UnconfiguredBedrockProvider:
    """Honest provider stub when AWS Bedrock credentials/runtime are unavailable."""

    def invoke(self, request: ProviderInvocationRequest) -> ProviderInvocationResponse:
        del request
        raise ModelGatewayError(
            "provider_not_configured",
            "AWS Bedrock provider is not configured in this environment",
            retryable=False,
        )


@dataclass(frozen=True)
class GatewayTelemetryEvent:
    correlation_id: str
    operation_name: str
    model_id: str
    prompt_version: str
    schema_version: int
    latency_ms: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    attempts: int
    fallback_used: bool
    error_code: str | None = None


@dataclass(frozen=True)
class ModelGatewayRequest:
    operation_name: str
    prompt_version: str
    schema_version: int
    payload: Mapping[str, Any]
    validate_input: Callable[[Mapping[str, Any]], list[str]]
    validate_output: Callable[[Mapping[str, Any]], list[str]]
    model_policy: ModelPolicy
    correlation_id: str
    deterministic_fallback: Callable[[], Mapping[str, Any]] | None = None


@dataclass(frozen=True)
class ModelGatewayResponse:
    validated_payload: dict[str, Any] | None
    failure: GatewayFailure | None
    provider_request_id: str | None
    model_id: str
    prompt_version: str
    schema_version: int
    latency_ms: int
    token_usage: TokenUsage
    attempts: int
    fallback_used: bool
    telemetry: GatewayTelemetryEvent


def _find_prohibited_output_keys(value: object, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_str = str(key)
            child_path = f"{path}.{key_str}"
            if key_str in _PROHIBITED_OUTPUT_KEYS:
                findings.append(child_path)
            findings.extend(_find_prohibited_output_keys(child, child_path))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            findings.extend(_find_prohibited_output_keys(child, f"{path}[{idx}]"))
    return findings


def _validate_policy(policy: ModelPolicy) -> None:
    if (
        policy.timeout_ms <= 0
        or policy.max_output_tokens <= 0
        or not math.isfinite(policy.max_cost_usd)
        or policy.max_cost_usd <= 0
        or policy.circuit_breaker_threshold < 1
    ):
        raise ModelGatewayError("invalid_model_policy", "Model policy limits must be positive")


def _validate_provider_metrics(
    response: ProviderInvocationResponse,
    policy: ModelPolicy,
) -> GatewayFailure | None:
    usage = response.token_usage
    if (
        response.latency_ms < 0
        or usage.input_tokens < 0
        or usage.output_tokens < 0
        or not math.isfinite(usage.estimated_cost_usd)
        or usage.estimated_cost_usd < 0
    ):
        return GatewayFailure("invalid_provider_metrics", "Negative provider metrics", False)
    if response.latency_ms > policy.timeout_ms:
        return GatewayFailure("provider_timeout_exceeded", "Provider exceeded timeout_ms", True)
    if usage.output_tokens > policy.max_output_tokens:
        return GatewayFailure("output_token_limit_exceeded", "Output exceeded token limit", False)
    if usage.estimated_cost_usd > policy.max_cost_usd:
        return GatewayFailure("cost_limit_exceeded", "Invocation exceeded cost budget", False)
    return None


class ModelGateway:
    """Shared bounded AI gateway with schema validation, one repair retry, and labeled fallback."""

    def __init__(self, provider: ModelProviderPort | None = None) -> None:
        self._provider: ModelProviderPort = provider or UnconfiguredBedrockProvider()
        self._consecutive_failures = 0
        self._telemetry_events: list[GatewayTelemetryEvent] = []

    @property
    def telemetry_events(self) -> tuple[GatewayTelemetryEvent, ...]:
        return tuple(self._telemetry_events)

    @property
    def circuit_open(self) -> bool:
        return self._consecutive_failures > 0

    def reset_circuit(self) -> None:
        self._consecutive_failures = 0

    def generate(self, request: ModelGatewayRequest) -> ModelGatewayResponse:
        if (
            not request.operation_name.strip()
            or not request.prompt_version.strip()
            or not request.correlation_id.strip()
            or request.schema_version < 1
        ):
            raise ModelGatewayError(
                "invalid_gateway_request", "Operation, prompt version, and correlation ID required"
            )
        _validate_policy(request.model_policy)

        prohibited_in = find_prohibited_transcript_fields(dict(request.payload))
        if prohibited_in:
            raise ModelGatewayError(
                "prohibited_transcript_in_gateway_input",
                f"Prohibited transcript fields: {','.join(prohibited_in)}",
            )
        input_issues = request.validate_input(request.payload)
        if input_issues:
            raise ModelGatewayError(
                "input_schema_validation_failed",
                f"Invalid gateway input: {','.join(input_issues)}",
            )

        policy = request.model_policy
        total_latency_ms = 0
        total_in_tokens = 0
        total_out_tokens = 0
        total_cost = 0.0
        attempts = 0
        last_failure: GatewayFailure | None = None
        last_request_id: str | None = None
        last_model_id = "uninvoked"

        if self._consecutive_failures >= policy.circuit_breaker_threshold:
            last_failure = GatewayFailure(
                code="circuit_breaker_open",
                detail="Provider circuit breaker is open after repeated failures",
                retryable=False,
            )
        else:
            max_attempts = 2 if policy.allow_repair_retry else 1
            repair_feedback: str | None = None
            for attempt in range(1, max_attempts + 1):
                attempts = attempt
                try:
                    provider_response = self._provider.invoke(
                        ProviderInvocationRequest(
                            operation_name=request.operation_name,
                            prompt_version=request.prompt_version,
                            schema_version=request.schema_version,
                            payload=deepcopy(dict(request.payload)),
                            correlation_id=request.correlation_id,
                            attempt=attempt,
                            repair_feedback=repair_feedback,
                            timeout_ms=policy.timeout_ms,
                            max_output_tokens=policy.max_output_tokens,
                        )
                    )
                except ModelGatewayError as error:
                    self._consecutive_failures += 1
                    last_failure = GatewayFailure(error.code, str(error), error.retryable)
                    if not error.retryable:
                        break
                    repair_feedback = f"provider_error:{error.code}"
                    continue

                last_request_id = provider_response.provider_request_id
                last_model_id = provider_response.model_id
                total_latency_ms += max(0, provider_response.latency_ms)
                total_in_tokens += max(0, provider_response.token_usage.input_tokens)
                total_out_tokens += max(0, provider_response.token_usage.output_tokens)
                if math.isfinite(provider_response.token_usage.estimated_cost_usd):
                    total_cost = round(
                        total_cost + max(0.0, provider_response.token_usage.estimated_cost_usd), 6
                    )

                metric_failure = _validate_provider_metrics(provider_response, policy)
                if metric_failure is not None:
                    self._consecutive_failures += 1
                    last_failure = metric_failure
                    if not metric_failure.retryable:
                        break
                    repair_feedback = f"metric_violation:{metric_failure.code}"
                    continue

                prohibited_out = _find_prohibited_output_keys(
                    provider_response.payload
                ) + find_prohibited_transcript_fields(dict(provider_response.payload))
                output_issues = (
                    ["prohibited_output_field"]
                    if prohibited_out
                    else request.validate_output(provider_response.payload)
                )
                if not output_issues:
                    self._consecutive_failures = 0
                    usage = TokenUsage(total_in_tokens, total_out_tokens, total_cost)
                    telemetry = GatewayTelemetryEvent(
                        correlation_id=request.correlation_id,
                        operation_name=request.operation_name,
                        model_id=last_model_id,
                        prompt_version=request.prompt_version,
                        schema_version=request.schema_version,
                        latency_ms=total_latency_ms,
                        input_tokens=total_in_tokens,
                        output_tokens=total_out_tokens,
                        estimated_cost_usd=total_cost,
                        attempts=attempts,
                        fallback_used=False,
                        error_code=None,
                    )
                    self._telemetry_events.append(telemetry)
                    return ModelGatewayResponse(
                        validated_payload=deepcopy(dict(provider_response.payload)),
                        failure=None,
                        provider_request_id=last_request_id,
                        model_id=last_model_id,
                        prompt_version=request.prompt_version,
                        schema_version=request.schema_version,
                        latency_ms=total_latency_ms,
                        token_usage=usage,
                        attempts=attempts,
                        fallback_used=False,
                        telemetry=telemetry,
                    )

                repair_codes = ",".join(output_issues)
                last_failure = GatewayFailure(
                    code="output_validation_failed",
                    detail=f"Output validation failed: {repair_codes}",
                    retryable=True,
                )
                repair_feedback = f"schema_repair:{repair_codes}"

        usage = TokenUsage(total_in_tokens, total_out_tokens, total_cost)
        failure = last_failure or GatewayFailure(
            code="gateway_invocation_skipped",
            detail="Provider invocation did not produce a valid response",
            retryable=False,
        )

        if request.deterministic_fallback is not None:
            fallback_payload = deepcopy(dict(request.deterministic_fallback()))
            fallback_issues = request.validate_output(fallback_payload)
            if fallback_issues:
                raise ModelGatewayError(
                    "fallback_output_invalid",
                    f"Deterministic fallback violated output contract: {','.join(fallback_issues)}",
                )
            telemetry = GatewayTelemetryEvent(
                correlation_id=request.correlation_id,
                operation_name=request.operation_name,
                model_id="deterministic-fallback",
                prompt_version=request.prompt_version,
                schema_version=request.schema_version,
                latency_ms=total_latency_ms,
                input_tokens=total_in_tokens,
                output_tokens=total_out_tokens,
                estimated_cost_usd=total_cost,
                attempts=attempts,
                fallback_used=True,
                error_code=failure.code,
            )
            self._telemetry_events.append(telemetry)
            return ModelGatewayResponse(
                validated_payload=fallback_payload,
                failure=failure,
                provider_request_id=last_request_id,
                model_id="deterministic-fallback",
                prompt_version=request.prompt_version,
                schema_version=request.schema_version,
                latency_ms=total_latency_ms,
                token_usage=usage,
                attempts=attempts,
                fallback_used=True,
                telemetry=telemetry,
            )

        telemetry = GatewayTelemetryEvent(
            correlation_id=request.correlation_id,
            operation_name=request.operation_name,
            model_id=last_model_id,
            prompt_version=request.prompt_version,
            schema_version=request.schema_version,
            latency_ms=total_latency_ms,
            input_tokens=total_in_tokens,
            output_tokens=total_out_tokens,
            estimated_cost_usd=total_cost,
            attempts=attempts,
            fallback_used=False,
            error_code=failure.code,
        )
        self._telemetry_events.append(telemetry)
        return ModelGatewayResponse(
            validated_payload=None,
            failure=failure,
            provider_request_id=last_request_id,
            model_id=last_model_id,
            prompt_version=request.prompt_version,
            schema_version=request.schema_version,
            latency_ms=total_latency_ms,
            token_usage=usage,
            attempts=attempts,
            fallback_used=False,
            telemetry=telemetry,
        )
