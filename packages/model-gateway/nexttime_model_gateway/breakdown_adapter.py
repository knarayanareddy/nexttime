from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, Final

from nexttime_domain.breakdowns import (
    MAX_PROPOSED_BREAKDOWNS,
    BreakdownCode,
    BreakdownValidationError,
    DeterministicBreakdownExtractor,
    RawBreakdownCandidate,
    RawCriterionCandidate,
    RawExtractionPayload,
    ensure_non_sensitive_excerpts,
    validate_raw_extraction,
)
from nexttime_domain.models import CriterionResult, Rubric, Situation
from nexttime_domain.transcripts import (
    CaptureMode,
    MinimizedTranscript,
    redact_deterministic_identifiers,
)

from .gateway import (
    GatewayTelemetryEvent,
    ModelGateway,
    ModelGatewayError,
    ModelGatewayRequest,
    ModelPolicy,
)

BREAKDOWN_OPERATION_NAME: Final[str] = "breakdown_propose.v1"
DEFAULT_PROMPT_VERSION: Final[str] = "breakdown-propose-prompt-v1"

_RUBRIC_VERSION_PATTERN = re.compile(r"^rubric_v[0-9]+$")
_ALLOWED_CRITERIA = frozenset(
    {
        "goal_stated",
        "repair_requested",
        "details_understood",
        "appointment_confirmed",
        "next_step_confirmed",
        "independent_completion",
    }
)
_ALLOWED_SITUATIONS = frozenset(item.value for item in Situation)
_ALLOWED_CAPTURE_MODES = frozenset(item.value for item in CaptureMode)
_ALLOWED_RESULTS = frozenset(item.value for item in CriterionResult)
_ALLOWED_BREAKDOWN_CODES = frozenset(item.value for item in BreakdownCode)

_INPUT_KEYS = frozenset(
    {
        "schema_version",
        "situation",
        "capture_mode",
        "rubric_version",
        "required_criteria",
        "optional_criteria",
        "asr_confidence",
        "other_speaker_transcript_retained",
        "self_excerpts",
    }
)
_EXCERPT_KEYS = frozenset({"excerpt_index", "text", "start_ms", "end_ms", "confidence"})
_OUTPUT_KEYS = frozenset({"schema_version", "criteria", "breakdowns"})
_CRITERION_OUTPUT_KEYS = frozenset(
    {"criterion_id", "result", "excerpt_index", "self_evidence_excerpt", "model_confidence"}
)
_BREAKDOWN_OUTPUT_KEYS = frozenset(
    {"code", "description", "excerpt_index", "self_evidence_excerpt", "model_confidence"}
)


def _is_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int)


def _is_unit_float(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
    )


def _validate_criterion_id_list(value: object, *, min_items: int, issue_code: str) -> list[str]:
    if not isinstance(value, list) or len(value) < min_items:
        return [issue_code]
    if len(set(value)) != len(value) or not all(
        isinstance(item, str) and item in _ALLOWED_CRITERIA for item in value
    ):
        return [issue_code]
    return []


def validate_breakdown_input_payload(payload: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    if set(payload.keys()) != _INPUT_KEYS:
        return ["invalid_input_keys"]
    if not _is_int(payload["schema_version"]) or payload["schema_version"] != 1:
        issues.append("invalid_schema_version")
    if payload["situation"] not in _ALLOWED_SITUATIONS:
        issues.append("invalid_situation")
    if payload["capture_mode"] not in _ALLOWED_CAPTURE_MODES:
        issues.append("invalid_capture_mode")
    rubric_version = payload["rubric_version"]
    if not isinstance(rubric_version, str) or not _RUBRIC_VERSION_PATTERN.match(rubric_version):
        issues.append("invalid_rubric_version")
    issues.extend(
        _validate_criterion_id_list(
            payload["required_criteria"], min_items=1, issue_code="invalid_required_criteria"
        )
    )
    issues.extend(
        _validate_criterion_id_list(
            payload["optional_criteria"], min_items=0, issue_code="invalid_optional_criteria"
        )
    )
    if not _is_unit_float(payload["asr_confidence"]):
        issues.append("invalid_asr_confidence")
    if payload["other_speaker_transcript_retained"] is not False:
        issues.append("other_speaker_transcript_retained")

    excerpts = payload["self_excerpts"]
    if not isinstance(excerpts, list) or not 1 <= len(excerpts) <= 50:
        issues.append("invalid_self_excerpts")
    else:
        for item in excerpts:
            if not isinstance(item, Mapping) or set(item.keys()) != _EXCERPT_KEYS:
                issues.append("invalid_self_excerpt_item")
                break
            if (
                not _is_int(item["excerpt_index"])
                or item["excerpt_index"] < 0
                or not isinstance(item["text"], str)
                or not 1 <= len(item["text"]) <= 500
                or not _is_int(item["start_ms"])
                or item["start_ms"] < 0
                or not _is_int(item["end_ms"])
                or item["end_ms"] < 1
                or not _is_unit_float(item["confidence"])
            ):
                issues.append("invalid_self_excerpt_item")
                break
    return sorted(set(issues))


def validate_breakdown_output_payload(payload: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    if set(payload.keys()) != _OUTPUT_KEYS:
        return ["invalid_output_keys"]
    if not _is_int(payload["schema_version"]) or payload["schema_version"] != 1:
        issues.append("invalid_schema_version")

    criteria = payload["criteria"]
    if not isinstance(criteria, list) or len(criteria) > 6:
        issues.append("invalid_criteria_collection")
    else:
        for item in criteria:
            if not isinstance(item, Mapping) or set(item.keys()) != _CRITERION_OUTPUT_KEYS:
                issues.append("invalid_criterion_item")
                break
            excerpt_index = item["excerpt_index"]
            valid_index = excerpt_index is None or (_is_int(excerpt_index) and excerpt_index >= 0)
            if (
                item["criterion_id"] not in _ALLOWED_CRITERIA
                or item["result"] not in _ALLOWED_RESULTS
                or not valid_index
                or not isinstance(item["self_evidence_excerpt"], str)
                or len(item["self_evidence_excerpt"]) > 500
                or not _is_unit_float(item["model_confidence"])
            ):
                issues.append("invalid_criterion_item")
                break

    breakdowns = payload["breakdowns"]
    if not isinstance(breakdowns, list):
        issues.append("invalid_breakdowns_collection")
    elif len(breakdowns) > MAX_PROPOSED_BREAKDOWNS:
        issues.append("too_many_breakdowns")
    else:
        for item in breakdowns:
            if not isinstance(item, Mapping) or set(item.keys()) != _BREAKDOWN_OUTPUT_KEYS:
                issues.append("invalid_breakdown_item")
                break
            if (
                item["code"] not in _ALLOWED_BREAKDOWN_CODES
                or not isinstance(item["description"], str)
                or not 1 <= len(item["description"]) <= 300
                or not _is_int(item["excerpt_index"])
                or item["excerpt_index"] < 0
                or not isinstance(item["self_evidence_excerpt"], str)
                or not 1 <= len(item["self_evidence_excerpt"]) <= 500
                or not _is_unit_float(item["model_confidence"])
            ):
                issues.append("invalid_breakdown_item")
                break
    return sorted(set(issues))


def serialize_minimized_for_gateway(
    minimized: MinimizedTranscript,
    rubric: Rubric,
) -> dict[str, Any]:
    if minimized.other_speaker_transcript_retained is not False:
        raise BreakdownValidationError("other_speaker_transcript_retained")
    ensure_non_sensitive_excerpts(minimized.self_excerpts)
    excerpts_payload: list[dict[str, Any]] = []
    for index, excerpt in enumerate(minimized.self_excerpts):
        redacted_text, _ = redact_deterministic_identifiers(excerpt.text)
        excerpts_payload.append(
            {
                "excerpt_index": index,
                "text": redacted_text,
                "start_ms": excerpt.start_ms,
                "end_ms": excerpt.end_ms,
                "confidence": round(excerpt.confidence, 4),
            }
        )
    return {
        "schema_version": 1,
        "situation": minimized.situation.value,
        "capture_mode": minimized.capture_mode.value,
        "rubric_version": rubric.version,
        "required_criteria": list(rubric.required[minimized.situation]),
        "optional_criteria": list(rubric.optional[minimized.situation]),
        "asr_confidence": round(minimized.asr_confidence, 4),
        "other_speaker_transcript_retained": False,
        "self_excerpts": excerpts_payload,
    }


def serialize_raw_extraction_payload(raw: RawExtractionPayload) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "criteria": [
            {
                "criterion_id": item.criterion_id,
                "result": item.result.value,
                "excerpt_index": item.excerpt_index,
                "self_evidence_excerpt": item.self_evidence_excerpt,
                "model_confidence": item.model_confidence,
            }
            for item in raw.criteria
        ],
        "breakdowns": [
            {
                "code": item.code.value,
                "description": item.description,
                "excerpt_index": item.excerpt_index,
                "self_evidence_excerpt": item.self_evidence_excerpt,
                "model_confidence": item.model_confidence,
            }
            for item in raw.breakdowns
        ],
    }


def parse_raw_extraction_payload(
    payload: Mapping[str, Any],
    *,
    model_version: str,
    fallback_used: bool = False,
    fallback_reason: str | None = None,
) -> RawExtractionPayload:
    criteria = tuple(
        RawCriterionCandidate(
            criterion_id=str(item["criterion_id"]),
            result=CriterionResult(str(item["result"])),
            excerpt_index=(
                int(item["excerpt_index"]) if item["excerpt_index"] is not None else None
            ),
            self_evidence_excerpt=str(item["self_evidence_excerpt"]),
            model_confidence=float(item["model_confidence"]),
        )
        for item in payload["criteria"]
    )
    breakdowns = tuple(
        RawBreakdownCandidate(
            code=BreakdownCode(str(item["code"])),
            description=str(item["description"]),
            excerpt_index=int(item["excerpt_index"]),
            self_evidence_excerpt=str(item["self_evidence_excerpt"]),
            model_confidence=float(item["model_confidence"]),
        )
        for item in payload["breakdowns"]
    )
    return RawExtractionPayload(
        criteria=criteria,
        breakdowns=breakdowns,
        model_version=model_version,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
    )


class GatewayBreakdownExtractor:
    """Adapter implementing BreakdownExtractorPort via ModelGateway and deterministic fallback."""

    def __init__(
        self,
        gateway: ModelGateway | None = None,
        *,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        model_policy: ModelPolicy | None = None,
        fallback_extractor: DeterministicBreakdownExtractor | None = None,
    ) -> None:
        self._gateway = gateway or ModelGateway()
        self._prompt_version = prompt_version
        self._policy = model_policy or ModelPolicy()
        self._fallback = fallback_extractor or DeterministicBreakdownExtractor()

    @property
    def telemetry_events(self) -> tuple[GatewayTelemetryEvent, ...]:
        return self._gateway.telemetry_events

    def extract(
        self,
        minimized: MinimizedTranscript,
        rubric: Rubric,
        *,
        correlation_id: str,
    ) -> RawExtractionPayload:
        input_payload = serialize_minimized_for_gateway(minimized, rubric)

        def _validate_output_with_domain(candidate: Mapping[str, Any]) -> list[str]:
            schema_issues = validate_breakdown_output_payload(candidate)
            if schema_issues:
                return schema_issues
            try:
                parsed = parse_raw_extraction_payload(
                    candidate,
                    model_version="gateway-validation-probe",
                )
                validate_raw_extraction(
                    minimized,
                    rubric,
                    parsed,
                    timestamp=minimized.recorded_at,
                )
            except BreakdownValidationError as error:
                return [f"domain:{error}"]
            return []

        def _fallback() -> dict[str, Any]:
            deterministic_raw = self._fallback.extract(
                minimized,
                rubric,
                correlation_id=correlation_id,
            )
            return serialize_raw_extraction_payload(deterministic_raw)

        response = self._gateway.generate(
            ModelGatewayRequest(
                operation_name=BREAKDOWN_OPERATION_NAME,
                prompt_version=self._prompt_version,
                schema_version=1,
                payload=input_payload,
                validate_input=validate_breakdown_input_payload,
                validate_output=_validate_output_with_domain,
                model_policy=self._policy,
                correlation_id=correlation_id,
                deterministic_fallback=_fallback,
            )
        )
        if response.validated_payload is None:
            detail = response.failure.detail if response.failure is not None else "unknown"
            raise ModelGatewayError("breakdown_extraction_failed", detail)

        resolved_model_version = (
            self._fallback.model_version
            if response.fallback_used
            else f"{response.model_id}:{response.prompt_version}"
        )
        fallback_reason = (
            response.failure.code
            if response.fallback_used and response.failure is not None
            else None
        )
        return parse_raw_extraction_payload(
            response.validated_payload,
            model_version=resolved_model_version,
            fallback_used=response.fallback_used,
            fallback_reason=fallback_reason,
        )
