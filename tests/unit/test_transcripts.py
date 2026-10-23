from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from nexttime_domain import (
    CaptureMode,
    ConsentError,
    Situation,
    SourceTranscript,
    Speaker,
    TranscriptMinimizationService,
    TranscriptTurn,
    minimize_transcript,
)


def source(**changes: object) -> SourceTranscript:
    value = SourceTranscript(
        reference="fixture-bee-recording-002",
        learner_id="fixture-learner",
        recorded_at=datetime(2026, 10, 22, 15, 0, tzinfo=UTC),
        capture_mode=CaptureMode.CONSENTED_CONVERSATION,
        situation=Situation.REPAIR,
        turns=(
            TranscriptTurn(
                Speaker.LEARNER, "Ik wil een afspraak voor de reparatie.", 0, 1200, 0.91
            ),
            TranscriptTurn(Speaker.OTHER, "De loodgieter komt om negen uur.", 1200, 2400, 0.93),
            TranscriptTurn(
                Speaker.LEARNER,
                "Stuur naar learner@example.test of +31 20 123 4567.",
                2400,
                3600,
                0.89,
            ),
        ),
        consent_attested=True,
        all_participants_consented=True,
        revoked=False,
        retention_policy_present=True,
    )
    return replace(value, **changes)  # type: ignore[arg-type]


def test_minimization_excludes_other_speaker_and_marks_redaction_review() -> None:
    original = source()
    minimized_at = original.recorded_at + timedelta(hours=1)
    result = minimize_transcript(original, minimized_at=minimized_at)
    assert result.status == "review_required"
    assert result.other_speaker_transcript_retained is False
    assert len(result.self_excerpts) == 2
    assert "De loodgieter" not in repr(result)
    assert "learner@example.test" not in repr(result)
    assert "+31 20" not in repr(result)
    assert result.self_excerpts[1].text == "Stuur naar [redacted] of [redacted]."
    assert "deterministic_redaction_applied" in result.reason_codes
    assert "self_excerpt_review_required" in result.reason_codes
    assert (result.transient_source_delete_after - minimized_at).total_seconds() == 86_400


def test_self_memo_excludes_other_and_ambiguous_turns() -> None:
    memo = source(
        capture_mode=CaptureMode.SELF_MEMO,
        all_participants_consented=False,
        turns=(
            TranscriptTurn(Speaker.LEARNER, "Ik heb om een afspraak gevraagd.", 0, 1000, 0.88),
            TranscriptTurn(
                Speaker.OTHER, "Bystander text that must not persist.", 1000, 2000, 0.90
            ),
            TranscriptTurn(
                Speaker.UNKNOWN, "Ambiguous text that must not persist.", 2000, 3000, 0.80
            ),
        ),
    )
    result = minimize_transcript(memo)
    assert len(result.self_excerpts) == 1
    assert "Bystander" not in repr(result)
    assert "Ambiguous text" not in repr(result)
    assert "other_speaker_like_turn_excluded_from_self_memo" in result.reason_codes
    assert "ambiguous_speaker_excluded" in result.reason_codes
    assert "self_memo_requires_learner_review" in result.reason_codes


def test_consent_prerequisites_fail_closed() -> None:
    for changes in [
        {"consent_attested": False},
        {"revoked": True},
        {"retention_policy_present": False},
        {"all_participants_consented": False},
    ]:
        with pytest.raises(ConsentError):
            minimize_transcript(source(**changes))


def test_unknown_speaker_and_low_confidence_require_review() -> None:
    result = minimize_transcript(
        source(
            turns=(
                TranscriptTurn(Speaker.LEARNER, "Ik wil een afspraak.", 0, 1000, 0.70),
                TranscriptTurn(Speaker.UNKNOWN, "Onzekere stem.", 1000, 2000, 0.90),
            )
        )
    )
    assert result.asr_confidence == 0.70
    assert "asr_confidence_below_0_75" in result.reason_codes
    assert "ambiguous_speaker_excluded" in result.reason_codes


def test_invalid_turns_and_missing_learner_turns_are_rejected() -> None:
    with pytest.raises(ValueError, match="timing"):
        minimize_transcript(source(turns=(TranscriptTurn(Speaker.LEARNER, "Hallo", 10, 10, 0.9),)))
    with pytest.raises(ValueError, match="confidence"):
        minimize_transcript(source(turns=(TranscriptTurn(Speaker.LEARNER, "Hallo", 0, 10, 1.1),)))
    with pytest.raises(ValueError, match="No unambiguous learner turns"):
        minimize_transcript(source(turns=(TranscriptTurn(Speaker.OTHER, "Ander", 0, 10, 0.9),)))


def test_duplicate_reference_is_idempotent_but_conflicting_reuse_fails() -> None:
    service = TranscriptMinimizationService()
    original = source()
    first = service.minimize(original)
    assert service.minimize(original) is first
    assert service.get(original.learner_id, original.reference) is first
    with pytest.raises(ValueError, match="source_reference_reused"):
        service.minimize(source(turns=(TranscriptTurn(Speaker.LEARNER, "Changed", 0, 1000, 0.9),)))
    with pytest.raises(KeyError):
        service.get("another-learner", original.reference)


def test_cached_source_still_checks_revocation_and_consent() -> None:
    service = TranscriptMinimizationService()
    original = source()
    service.minimize(original)
    with pytest.raises(ConsentError, match="Revoked consent"):
        service.minimize(replace(original, revoked=True))
    with pytest.raises(ConsentError, match="attestation"):
        service.minimize(replace(original, consent_attested=False))


def test_naive_minimization_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="Minimization timestamp"):
        minimize_transcript(source(), minimized_at=datetime(2026, 10, 22, 16, 0))
