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


def test_calendar_audio_chunk_and_haptic_privacy_and_safety_boundaries() -> None:
    import asyncio

    from nexttime_calendar import CalendarSyncService, ICalendarFeedAdapter
    from nexttime_domain import RehearsalHapticGenerator, RehearsalPacingTrigger
    from nexttime_rehearsal import (
        ConsentedAudioChunkTranscriber,
        CovertCaptureProhibitedError,
        DeterministicFixtureTranscribeTransport,
        PcmAudioChunk,
    )

    now = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    learner_id = "01000000000000000000000002"

    # 1. Calendar sync strips raw PII titles/descriptions and blocks sensitive meetings (SEC-NT-006)
    ics = (
        "BEGIN:VCALENDAR\n"
        "BEGIN:VEVENT\n"
        "UID:priv-uid-secret-123\n"
        "SUMMARY:Reparatie cv-ketel met Pieter de Vries Tel +31612345678\n"
        "DESCRIPTION:Sleutel onder deurmat Hoofdstraat 99\n"
        "DTSTART:20261026T140000Z\n"
        "END:VEVENT\n"
        "BEGIN:VEVENT\n"
        "UID:priv-uid-lawyer-456\n"
        "SUMMARY:Advocaat rechtbank verblijfsvergunning\n"
        "DTSTART:20261026T160000Z\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )
    sync = CalendarSyncService()
    confirmed_attempt = {
        "attempt_id": "01000000000000000000000001",
        "learner_id": learner_id,
        "status": "confirmed",
        "consent": {"revoked": False},
        "source": {"recorded_at": "2026-10-22T14:30:00Z"},
        "context": {"situation": "repair_appointment", "recurrence_id": None},
        "breakdowns": [
            {
                "breakdown_id": "01000000000000000000000005",
                "code": "confirmation_missing",
                "review_status": "confirmed",
            }
        ],
    }
    summary = asyncio.run(
        sync.sync_candidates(
            learner_id=learner_id,
            provider="icalendar",
            source=ICalendarFeedAdapter(ics),
            confirmed_attempts=[confirmed_attempt],
            now=now,
        )
    )
    assert summary.excluded_sensitive_count == 1
    assert len(summary.candidates) == 1
    dump = repr(summary.candidates)
    for forbidden_secret in (
        "Pieter de Vries",
        "31612345678",
        "Hoofdstraat",
        "priv-uid-secret-123",
    ):
        assert forbidden_secret not in dump

    # 2. Covert/continuous microphone audio chunks are blocked AND zeroed in place (SEC-NT-004)
    transcriber = ConsentedAudioChunkTranscriber(
        transport=DeterministicFixtureTranscribeTransport()
    )
    raw_pcm = PcmAudioChunk.from_bytes(sequence_id=1, pcm_bytes=b"\xfe\xdc\xba\x98" * 256)
    with pytest.raises(CovertCaptureProhibitedError):
        asyncio.run(
            transcriber.transcribe_ptt_turn(
                learner_id=learner_id,
                rehearsal_id="reh_sec_1",
                turn_event_id="turn_sec_1",
                chunks=[raw_pcm],
                streaming_transcription_consented=True,
                continuous_stream=True,
                transcribed_at=now,
            )
        )
    assert len(raw_pcm.pcm_buffer) == 0

    # 3. Unverified Bee haptic commands and mid-speech interruptions are rejected/suppressed
    haptics = RehearsalHapticGenerator(rehearsal_id="reh_sec_1")
    with pytest.raises(ValueError, match="unverified_bee_haptic_command_prohibited"):
        haptics.generate(
            source_event_id="sec-hap-1",
            trigger=RehearsalPacingTrigger.PTT_RECORDING_STARTED,
            occurred_at=now,
            target_device="bee_cli_haptic",
        )
    mid_speech = haptics.generate(
        source_event_id="sec-hap-2",
        trigger=RehearsalPacingTrigger.MID_SPEECH_PACING_INTERRUPT,
        occurred_at=now,
    )
    assert mid_speech.emitted is False
    assert mid_speech.suppression_reason == "real_time_interruption_prohibited"
