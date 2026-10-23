from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from nexttime_domain import (
    MAX_PROPOSED_BREAKDOWNS,
    AttemptReviewService,
    BreakdownCode,
    BreakdownProposalService,
    BreakdownReviewDecision,
    BreakdownValidationError,
    CaptureMode,
    ConsentError,
    CriterionResult,
    DeterministicBreakdownExtractor,
    FixtureRehearsalService,
    MinimizedTranscript,
    OutcomeStatus,
    RawBreakdownCandidate,
    RawCriterionCandidate,
    RawExtractionPayload,
    Rubric,
    SelfExcerpt,
    SensitiveContentError,
    Situation,
    SourceTranscript,
    Speaker,
    TranscriptTurn,
    combine_breakdown_confidence,
    deterministic_ulid,
    minimize_transcript,
    validate_minimized_attempt,
    validate_raw_extraction,
)

LEARNER_ID = "01000000000000000000000002"
CONSENT_ID = "01000000000000000000000004"
RECURRENCE_ID = "01000000000000000000000003"
RECORDED_AT = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
ATTESTED_AT = datetime(2026, 10, 22, 14, 59, tzinfo=UTC)
PROPOSED_AT = datetime(2026, 10, 22, 15, 5, tzinfo=UTC)


class StaticExtractor:
    def __init__(self, payload: RawExtractionPayload) -> None:
        self._payload = payload

    def extract(
        self,
        minimized: MinimizedTranscript,
        rubric: Rubric,
        *,
        correlation_id: str,
    ) -> RawExtractionPayload:
        del minimized, rubric, correlation_id
        return self._payload


def make_minimized(
    turns: tuple[TranscriptTurn, ...] | None = None,
    *,
    situation: Situation = Situation.REPAIR,
    capture_mode: CaptureMode = CaptureMode.CONSENTED_CONVERSATION,
    reference: str = "fixture-bee-ref-001",
    provenance: str = "fixture",
) -> MinimizedTranscript:
    default_turns = (
        TranscriptTurn(
            Speaker.LEARNER,
            "Ik wil een afspraak voor de reparatie.",
            0,
            1800,
            0.91,
        ),
        TranscriptTurn(
            Speaker.OTHER,
            "Vrijdag om tien uur kan de monteur komen.",
            1800,
            3400,
            0.95,
        ),
        TranscriptTurn(
            Speaker.LEARNER,
            "Ja, dank u.",
            3400,
            4500,
            0.91,
        ),
    )
    src = SourceTranscript(
        reference=reference,
        learner_id=LEARNER_ID,
        recorded_at=RECORDED_AT,
        capture_mode=capture_mode,
        situation=situation,
        turns=turns or default_turns,
        consent_attested=True,
        all_participants_consented=(capture_mode is CaptureMode.CONSENTED_CONVERSATION),
        revoked=False,
        retention_policy_present=True,
        provenance=cast(Any, provenance),
    )
    return minimize_transcript(src, minimized_at=RECORDED_AT + timedelta(minutes=2))


def propose_default(
    service: BreakdownProposalService,
    minimized: MinimizedTranscript,
    **overrides: Any,
) -> Any:
    params: dict[str, Any] = {
        "consent_id": CONSENT_ID,
        "consent_attested_at": ATTESTED_AT,
        "all_participants_consented": (
            minimized.capture_mode is CaptureMode.CONSENTED_CONVERSATION
        ),
        "learner_goal": "Confirm a repair appointment time in Dutch.",
        "recurrence_id": RECURRENCE_ID,
        "proposed_at": PROPOSED_AT,
    }
    params.update(overrides)
    return service.propose(minimized, **params)


def test_combine_confidence_and_deterministic_ulid_guards() -> None:
    assert combine_breakdown_confidence(asr_confidence=0.91, model_confidence=0.82) == 0.82
    assert combine_breakdown_confidence(asr_confidence=0.72, model_confidence=0.95) == 0.72
    assert combine_breakdown_confidence(asr_confidence=0.0, model_confidence=1.0) == 0.0

    for bad_asr, bad_model in [(-0.1, 0.5), (0.5, 1.1), (float("nan"), 0.5), (0.5, float("inf"))]:
        with pytest.raises(BreakdownValidationError, match="out_of_bounds"):
            combine_breakdown_confidence(asr_confidence=bad_asr, model_confidence=bad_model)

    ulid_a = deterministic_ulid(namespace="test", seed="abc", timestamp=RECORDED_AT)
    ulid_b = deterministic_ulid(namespace="test", seed="abc", timestamp=RECORDED_AT)
    assert ulid_a == ulid_b
    assert len(ulid_a) == 26
    with pytest.raises(ValueError, match="timezone"):
        deterministic_ulid(namespace="test", seed="abc", timestamp=datetime(2026, 10, 22, 15, 0))


def test_proposes_confirmation_missing_and_flows_into_review_and_rehearsal(
    rubric: Rubric,
) -> None:
    minimized = make_minimized()
    service = BreakdownProposalService(rubric)
    result = propose_default(service, minimized)

    assert result.duplicate is False
    assert result.outcome.status is OutcomeStatus.PARTIAL
    assert result.learner_review_required is True
    assert "proposed_breakdown_below_0_85" in result.review_reason_codes
    assert "proposed_breakdown_requires_confirmation" in result.review_reason_codes
    assert len(result.breakdowns) == 1

    breakdown = result.breakdowns[0]
    assert breakdown.code is BreakdownCode.CONFIRMATION_MISSING
    assert breakdown.self_evidence_excerpt == "Ja, dank u."
    assert breakdown.asr_confidence == 0.91
    assert breakdown.model_confidence == 0.82
    assert breakdown.confidence == 0.82
    assert breakdown.requires_learner_review is True
    assert breakdown.review_status == "proposed"
    assert validate_minimized_attempt(result.attempt_document) == []

    review_service = AttemptReviewService(result.attempt_document, rubric)
    review_service.review_breakdown(
        actor_learner_id=LEARNER_ID,
        expected_version=1,
        breakdown_id=breakdown.breakdown_id,
        decision=BreakdownReviewDecision.CONFIRMED,
        reviewed_at=PROPOSED_AT + timedelta(minutes=10),
    )
    confirmed = review_service.confirm_attempt(
        actor_learner_id=LEARNER_ID,
        expected_version=2,
        reviewed_at=PROPOSED_AT + timedelta(minutes=11),
    )
    assert confirmed.document["status"] == "confirmed"
    assert confirmed.skill_updates == ("confirmation",)

    rehearsal = FixtureRehearsalService(
        confirmed.document,
        actor_learner_id=LEARNER_ID,
        breakdown_id=breakdown.breakdown_id,
        initial_skill_states=confirmed.skill_states,
    )
    assert rehearsal.scenario.target_function == "confirmation"


def test_satisfied_school_admin_attempt_with_repair_has_zero_breakdowns(rubric: Rubric) -> None:
    minimized = make_minimized(
        turns=(
            TranscriptTurn(
                Speaker.LEARNER,
                "Ik wil een afspraak met de schooladministratie.",
                0,
                2000,
                0.93,
            ),
            TranscriptTurn(
                Speaker.LEARNER,
                "Kunt u dat herhalen alstublieft?",
                2500,
                4000,
                0.90,
            ),
            TranscriptTurn(
                Speaker.LEARNER,
                "Ja, ik bevestig maandag om negen uur.",
                4500,
                6500,
                0.94,
            ),
        ),
        situation=Situation.SCHOOL_ADMIN,
        reference="fixture-school-001",
    )
    service = BreakdownProposalService(rubric)
    result = propose_default(
        service,
        minimized,
        learner_goal="Book a school administration appointment.",
        recurrence_id=None,
    )
    assert result.breakdowns == ()
    assert result.outcome.status is OutcomeStatus.SUCCESS
    assert result.learner_review_required is False
    assert "recurrence_id" not in result.attempt_document["context"]
    criterion_ids = [c.criterion_id for c in result.criteria]
    assert criterion_ids == ["goal_stated", "appointment_confirmed", "repair_requested"]


def test_deterministic_extractor_caps_at_three_breakdowns(rubric: Rubric) -> None:
    minimized = make_minimized(
        turns=(
            TranscriptTurn(
                Speaker.LEARNER,
                "Sorry English please, ik snap het niet.",
                0,
                1500,
                0.89,
            ),
            TranscriptTurn(
                Speaker.LEARNER,
                "Uhm hoe laat ook alweer?",
                1600,
                3000,
                0.88,
            ),
            TranscriptTurn(
                Speaker.LEARNER,
                "Laat maar zitten, ik moet nu ophangen.",
                3100,
                4800,
                0.90,
            ),
        ),
        situation=Situation.SHIFT,
        reference="fixture-shift-many-gaps",
    )
    extractor = DeterministicBreakdownExtractor()
    raw = extractor.extract(minimized, rubric, correlation_id="corr-cap-test")
    assert len(raw.breakdowns) == MAX_PROPOSED_BREAKDOWNS

    service = BreakdownProposalService(rubric, extractor=extractor)
    result = propose_default(service, minimized, device_type="apple_watch_bee")
    assert len(result.breakdowns) == 3
    assert [b.code for b in result.breakdowns] == [
        BreakdownCode.COMPREHENSION_REPAIR,
        BreakdownCode.CONFIRMATION_MISSING,
        BreakdownCode.TIME_EXPRESSION,
    ]
    assert result.attempt_document["source"]["device_type"] == "apple_watch_bee"


def test_self_memo_low_asr_and_ambiguous_speaker_require_review(rubric: Rubric) -> None:
    self_memo = make_minimized(
        turns=(
            TranscriptTurn(
                Speaker.LEARNER,
                "Ik wil mijn dienst controleren en ik bevestig dinsdag om acht uur.",
                0,
                2500,
                0.95,
            ),
        ),
        situation=Situation.SHIFT,
        capture_mode=CaptureMode.SELF_MEMO,
        reference="fixture-self-memo-001",
    )
    service = BreakdownProposalService(rubric)
    memo_result = propose_default(service, self_memo)
    assert memo_result.attempt_document["processing"]["diarization_method"] == "self_only"
    assert memo_result.learner_review_required is True
    assert "self_memo_requires_learner_review" in memo_result.review_reason_codes

    low_asr_ambiguous = make_minimized(
        turns=(
            TranscriptTurn(
                Speaker.LEARNER,
                "Ik wil een afspraak voor de reparatie.",
                0,
                1500,
                0.65,
            ),
            TranscriptTurn(
                Speaker.UNKNOWN,
                "Onduidelijke spreker.",
                1600,
                2600,
                0.70,
            ),
        ),
        reference="fixture-low-asr-001",
        provenance="live",
    )
    low_result = propose_default(service, low_asr_ambiguous)
    assert low_result.outcome.status is OutcomeStatus.UNKNOWN
    assert low_result.learner_review_required is True
    assert "asr_confidence_below_0_75" in low_result.review_reason_codes
    assert "ambiguous_speaker_excluded" in low_result.review_reason_codes
    assert all(c.result is CriterionResult.UNKNOWN for c in low_result.criteria)
    assert low_result.breakdowns[0].confidence == 0.65


def test_high_confidence_breakdown_still_keeps_attempt_awaiting_review(rubric: Rubric) -> None:
    minimized = make_minimized(
        turns=(
            TranscriptTurn(
                Speaker.LEARNER,
                "Ik wil een afspraak voor de reparatie.",
                0,
                1500,
                0.95,
            ),
            TranscriptTurn(
                Speaker.LEARNER,
                "Ik bevestig vrijdag om tien uur, maar uhm hoe laat eindigt het?",
                1600,
                4000,
                0.94,
            ),
        ),
        reference="fixture-high-conf-breakdown",
    )
    payload = RawExtractionPayload(
        criteria=(
            RawCriterionCandidate(
                "goal_stated", CriterionResult.SATISFIED, 0, minimized.self_excerpts[0].text, 0.92
            ),
            RawCriterionCandidate(
                "appointment_confirmed",
                CriterionResult.SATISFIED,
                1,
                minimized.self_excerpts[1].text,
                0.91,
            ),
        ),
        breakdowns=(
            RawBreakdownCandidate(
                code=BreakdownCode.TIME_EXPRESSION,
                description="Follow-up end-time question used tentative phrasing.",
                excerpt_index=1,
                self_evidence_excerpt="uhm hoe laat eindigt het?",
                model_confidence=0.90,
            ),
        ),
        model_version="fixture-model-v1",
    )
    service = BreakdownProposalService(rubric, extractor=StaticExtractor(payload))
    result = propose_default(service, minimized)
    assert result.breakdowns[0].confidence == 0.90
    assert result.breakdowns[0].requires_learner_review is False
    assert result.learner_review_required is True
    assert "proposed_breakdown_below_0_85" not in result.review_reason_codes
    assert "proposed_breakdown_requires_confirmation" in result.review_reason_codes
    assert result.attempt_document["status"] == "awaiting_learner_review"


def test_idempotency_owner_access_and_conflicting_replay(rubric: Rubric) -> None:
    minimized = make_minimized(provenance="live")
    service = BreakdownProposalService(rubric)
    first = propose_default(service, minimized)
    assert first.attempt_document["source"]["provenance"] == "live_bee"

    replay = propose_default(service, minimized)
    assert replay.duplicate is True
    assert replay.attempt_id == first.attempt_id
    assert replay.breakdowns == first.breakdowns
    assert replay.attempt_document == first.attempt_document

    fetched = service.get(
        actor_learner_id=LEARNER_ID,
        learner_id=LEARNER_ID,
        source_reference=minimized.source_reference,
    )
    assert fetched.attempt_id == first.attempt_id

    with pytest.raises(PermissionError, match="attempt_access_denied"):
        service.get(
            actor_learner_id="01000000000000000000000099",
            learner_id=LEARNER_ID,
            source_reference=minimized.source_reference,
        )
    with pytest.raises(KeyError, match="breakdown_proposal_not_found"):
        service.get(
            actor_learner_id=LEARNER_ID,
            learner_id=LEARNER_ID,
            source_reference="missing-ref",
        )
    with pytest.raises(ValueError, match="source_reference_reused_with_different_content"):
        propose_default(service, minimized, learner_goal="Different goal on same source ref")
    with pytest.raises(ConsentError, match="Revoked consent"):
        propose_default(service, minimized, consent_revoked=True)


def test_proposal_service_parameter_and_consent_guards(rubric: Rubric) -> None:
    minimized = make_minimized()
    service = BreakdownProposalService(rubric)

    with pytest.raises(ConsentError, match="Every participant"):
        propose_default(service, minimized, all_participants_consented=False)
    with pytest.raises(ConsentError, match="retention policy"):
        propose_default(service, minimized, derived_retention_days=0)
    with pytest.raises(ValueError, match="Invalid learner_id ULID"):
        propose_default(service, replace(minimized, learner_id="bad-id"))
    with pytest.raises(ValueError, match="Invalid consent_id ULID"):
        propose_default(service, minimized, consent_id="bad-id")
    with pytest.raises(ValueError, match="Invalid recurrence_id ULID"):
        propose_default(service, minimized, recurrence_id="bad-id")
    with pytest.raises(ValueError, match="policy_version"):
        propose_default(service, minimized, policy_version="v1")
    with pytest.raises(ValueError, match="Learner goal"):
        propose_default(service, minimized, learner_goal="   ")
    with pytest.raises(ValueError, match="device_type"):
        propose_default(service, minimized, device_type=cast(Any, "phone"))
    with pytest.raises(ValueError, match="Consent attestation timestamp"):
        propose_default(service, minimized, consent_attested_at=datetime(2026, 10, 22, 14, 59))
    with pytest.raises(ValueError, match="Proposal timestamp must include a timezone"):
        propose_default(service, minimized, proposed_at=datetime(2026, 10, 22, 15, 5))
    with pytest.raises(ValueError, match="cannot precede recording"):
        propose_default(service, minimized, proposed_at=RECORDED_AT - timedelta(minutes=1))

    self_memo = make_minimized(
        capture_mode=CaptureMode.SELF_MEMO,
        reference="fixture-self-memo-guard",
    )
    with pytest.raises(ValueError, match="self_only"):
        propose_default(service, self_memo, diarization_method="aws_diarization")


def test_validate_raw_extraction_rejects_untrusted_or_unsafe_candidates(rubric: Rubric) -> None:
    minimized = make_minimized()
    valid_breakdown = RawBreakdownCandidate(
        code=BreakdownCode.CONFIRMATION_MISSING,
        description="The attempt ended without confirming the time.",
        excerpt_index=1,
        self_evidence_excerpt="Ja, dank u.",
        model_confidence=0.82,
    )

    with pytest.raises(BreakdownValidationError, match="other_speaker_transcript_retained"):
        validate_raw_extraction(
            replace(minimized, other_speaker_transcript_retained=cast(Any, True)),
            rubric,
            RawExtractionPayload((), (), "model-v1"),
            timestamp=PROPOSED_AT,
        )
    with pytest.raises(BreakdownValidationError, match="missing_self_excerpts"):
        validate_raw_extraction(
            replace(minimized, self_excerpts=()),
            rubric,
            RawExtractionPayload((), (), "model-v1"),
            timestamp=PROPOSED_AT,
        )
    with pytest.raises(BreakdownValidationError, match="invalid_self_excerpt"):
        validate_raw_extraction(
            replace(minimized, self_excerpts=(SelfExcerpt("tekst", 10, 10, 0.9),)),
            rubric,
            RawExtractionPayload((), (), "model-v1"),
            timestamp=PROPOSED_AT,
        )
    with pytest.raises(BreakdownValidationError, match="invalid_model_version"):
        validate_raw_extraction(
            minimized,
            rubric,
            RawExtractionPayload((), (), "   "),
            timestamp=PROPOSED_AT,
        )

    four_breakdowns = (
        valid_breakdown,
        replace(valid_breakdown, code=BreakdownCode.COMPREHENSION_REPAIR),
        replace(valid_breakdown, code=BreakdownCode.TIME_EXPRESSION),
        replace(valid_breakdown, code=BreakdownCode.CLOSURE),
    )
    with pytest.raises(BreakdownValidationError, match="too_many_breakdowns"):
        validate_raw_extraction(
            minimized,
            rubric,
            RawExtractionPayload((), four_breakdowns, "model-v1"),
            timestamp=PROPOSED_AT,
        )
    with pytest.raises(BreakdownValidationError, match="duplicate_breakdown_code"):
        validate_raw_extraction(
            minimized,
            rubric,
            RawExtractionPayload((), (valid_breakdown, valid_breakdown), "model-v1"),
            timestamp=PROPOSED_AT,
        )
    with pytest.raises(BreakdownValidationError, match="invalid_excerpt_index"):
        validate_raw_extraction(
            minimized,
            rubric,
            RawExtractionPayload((), (replace(valid_breakdown, excerpt_index=9),), "model-v1"),
            timestamp=PROPOSED_AT,
        )
    with pytest.raises(BreakdownValidationError, match="unowned_self_evidence_excerpt"):
        validate_raw_extraction(
            minimized,
            rubric,
            RawExtractionPayload(
                (),
                (
                    replace(
                        valid_breakdown,
                        self_evidence_excerpt="Vrijdag om tien uur kan de monteur komen.",
                    ),
                ),
                "model-v1",
            ),
            timestamp=PROPOSED_AT,
        )
    with pytest.raises(BreakdownValidationError, match="excerpt_length_out_of_bounds"):
        validate_raw_extraction(
            minimized,
            rubric,
            RawExtractionPayload(
                (),
                (replace(valid_breakdown, self_evidence_excerpt="   "),),
                "model-v1",
            ),
            timestamp=PROPOSED_AT,
        )
    with pytest.raises(BreakdownValidationError, match="unsafe_excerpt_characters"):
        markup_min = replace(
            minimized,
            self_excerpts=(SelfExcerpt("Ja <script> dank u", 0, 1000, 0.9),),
        )
        validate_raw_extraction(
            markup_min,
            rubric,
            RawExtractionPayload(
                (),
                (
                    RawBreakdownCandidate(
                        BreakdownCode.UNKNOWN,
                        "Needs learner check.",
                        0,
                        "Ja <script> dank u",
                        0.8,
                    ),
                ),
                "model-v1",
            ),
            timestamp=PROPOSED_AT,
        )

    for bad_desc, expected in [
        ("   ", "description_length_out_of_bounds"),
        ("<b>Bold markup</b>", "unsafe_description_characters"),
        (
            "Learner sounded anxious and lacked a native-like accent.",
            "prohibited_inference_or_copy",
        ),
        ("Low fluency score on CEFR scale.", "prohibited_inference_or_copy"),
    ]:
        with pytest.raises(BreakdownValidationError, match=expected):
            validate_raw_extraction(
                minimized,
                rubric,
                RawExtractionPayload(
                    (),
                    (replace(valid_breakdown, description=bad_desc),),
                    "model-v1",
                ),
                timestamp=PROPOSED_AT,
            )

    for bad_criteria, expected_err in [
        (
            (RawCriterionCandidate("next_step_confirmed", CriterionResult.UNKNOWN, None, "", 0.8),),
            "unsupported_criterion_id",
        ),
        (
            (
                RawCriterionCandidate("goal_stated", CriterionResult.UNKNOWN, None, "", 0.8),
                RawCriterionCandidate("goal_stated", CriterionResult.UNKNOWN, None, "", 0.8),
            ),
            "duplicate_criterion_id",
        ),
        (
            (RawCriterionCandidate("goal_stated", CriterionResult.SATISFIED, None, "Ik wil", 0.9),),
            "criterion_requires_valid_excerpt_index",
        ),
        (
            (
                RawCriterionCandidate(
                    "appointment_confirmed",
                    CriterionResult.SATISFIED,
                    1,
                    "Ja, dank u.",
                    0.9,
                ),
            ),
            "breakdown_contradicts_satisfied_criterion",
        ),
    ]:
        with pytest.raises(BreakdownValidationError, match=expected_err):
            validate_raw_extraction(
                minimized,
                rubric,
                RawExtractionPayload(bad_criteria, (valid_breakdown,), "model-v1"),
                timestamp=PROPOSED_AT,
            )


def test_sensitive_content_is_excluded_before_breakdown_extraction(rubric: Rubric) -> None:
    sensitive = make_minimized(
        turns=(
            TranscriptTurn(
                Speaker.LEARNER,
                "Ik moet naar het ziekenhuis voor een diagnose van de dokter.",
                0,
                2000,
                0.92,
            ),
        ),
        reference="fixture-sensitive-001",
    )
    service = BreakdownProposalService(rubric)
    with pytest.raises(SensitiveContentError, match="sensitive_situation_content_excluded"):
        propose_default(service, sensitive)
