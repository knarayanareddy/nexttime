from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from nexttime_domain import (
    AttemptReviewService,
    CaptureMode,
    ConsentError,
    LearnerTurnLabel,
    RawSpeakerSegment,
    Rubric,
    SeparationMethod,
    Situation,
    Speaker,
    SpeakerSeparationPolicy,
    separate_and_minimize_transcript,
    separate_speaker_turns,
)


def test_bee_speaker_labels_separates_learner_and_other() -> None:
    segments = (
        RawSpeakerSegment(
            segment_id="seg-1",
            raw_speaker_label="self",
            text="Goedemiddag, ik bel voor een reparatieafspraak.",
            start_ms=0,
            end_ms=2600,
            asr_confidence=0.92,
            speaker_confidence=0.95,
        ),
        RawSpeakerSegment(
            segment_id="seg-2",
            raw_speaker_label="agent",
            text="Welke dag schikt u?",
            start_ms=2800,
            end_ms=4200,
            asr_confidence=0.96,
            speaker_confidence=0.97,
        ),
        RawSpeakerSegment(
            segment_id="seg-3",
            raw_speaker_label="user",
            text="Kan het vrijdag om tien uur?",
            start_ms=4500,
            end_ms=6500,
            asr_confidence=0.90,
            speaker_confidence=0.90,
        ),
    )
    result = separate_speaker_turns(
        segments,
        capture_mode=CaptureMode.CONSENTED_CONVERSATION,
        method=SeparationMethod.BEE_SPEAKER_LABELS,
    )
    assert result.learner_turn_count == 2
    assert result.excluded_other_turn_count == 1
    assert result.ambiguous_turn_count == 0
    assert not result.diarization_ambiguous
    assert not result.review_required
    assert [t.speaker for t in result.turns] == [
        Speaker.LEARNER,
        Speaker.OTHER,
        Speaker.LEARNER,
    ]


def test_temporal_crosstalk_low_speaker_confidence_and_unknown_label_downgrade() -> None:
    segments = (
        RawSpeakerSegment(
            segment_id="seg-a",
            raw_speaker_label="learner",
            text="Ik wil graag bevestigen.",
            start_ms=1000,
            end_ms=3000,
            asr_confidence=0.91,
            speaker_confidence=0.92,
        ),
        RawSpeakerSegment(
            segment_id="seg-b",
            raw_speaker_label="counterpart",
            text="Even kijken in de agenda.",
            start_ms=2500,
            end_ms=4000,
            asr_confidence=0.93,
            speaker_confidence=0.94,
        ),
        RawSpeakerSegment(
            segment_id="seg-c",
            raw_speaker_label="learner",
            text="Vrijdag ochtend?",
            start_ms=4200,
            end_ms=5200,
            asr_confidence=0.91,
            speaker_confidence=0.55,
        ),
        RawSpeakerSegment(
            segment_id="seg-d",
            raw_speaker_label="mystery_cluster",
            text="Ja dat staat genoteerd.",
            start_ms=5500,
            end_ms=6800,
            asr_confidence=0.91,
            speaker_confidence=0.90,
        ),
    )
    result = separate_speaker_turns(
        segments,
        capture_mode=CaptureMode.CONSENTED_CONVERSATION,
        method=SeparationMethod.BEE_SPEAKER_LABELS,
    )
    assert result.ambiguous_turn_count == 4
    assert result.diarization_ambiguous
    assert result.review_required
    assert "ambiguous_turn_requires_review" in result.reason_codes
    assert "overlapping_speaker_segments" in result.reason_codes
    assert "unreliable_bee_speaker_label" in result.reason_codes
    assert "unrecognized_bee_speaker_label" in result.reason_codes


def test_aws_diarization_unanchored_and_enrolled_modes() -> None:
    segments = (
        RawSpeakerSegment(
            segment_id="d1",
            raw_speaker_label="spk_0",
            text="Ik wil een afspraak bij school.",
            start_ms=0,
            end_ms=2200,
            asr_confidence=0.91,
            speaker_confidence=0.89,
        ),
        RawSpeakerSegment(
            segment_id="d2",
            raw_speaker_label="spk_1",
            text="Kom maandag om negen uur.",
            start_ms=2300,
            end_ms=3900,
            asr_confidence=0.94,
            speaker_confidence=0.93,
        ),
        RawSpeakerSegment(
            segment_id="d3",
            raw_speaker_label=None,
            text="Dank u wel.",
            start_ms=4100,
            end_ms=4900,
            asr_confidence=0.90,
            speaker_confidence=0.50,
        ),
    )
    unanchored = separate_speaker_turns(
        segments,
        capture_mode=CaptureMode.CONSENTED_CONVERSATION,
        method=SeparationMethod.AWS_DIARIZATION,
    )
    assert unanchored.learner_turn_count == 0
    assert unanchored.ambiguous_turn_count == 3
    assert "unanchored_diarization_cluster" in unanchored.reason_codes

    anchored = separate_speaker_turns(
        segments,
        capture_mode=CaptureMode.CONSENTED_CONVERSATION,
        method=SeparationMethod.AWS_DIARIZATION,
        policy=SpeakerSeparationPolicy(enrolled_diarization_speaker_id="SPK_0"),
    )
    assert anchored.learner_turn_count == 1
    assert anchored.excluded_other_turn_count == 1
    assert anchored.ambiguous_turn_count == 1
    assert anchored.review_required


def test_learner_labeled_and_self_only_modes() -> None:
    segments = (
        RawSpeakerSegment(
            segment_id="s1",
            raw_speaker_label="unknown_0",
            text="Welke dienst heb ik morgen?",
            start_ms=0,
            end_ms=2000,
            asr_confidence=0.90,
            speaker_confidence=0.60,
        ),
        RawSpeakerSegment(
            segment_id="s2",
            raw_speaker_label="unknown_1",
            text="Je begint om acht uur.",
            start_ms=2200,
            end_ms=3600,
            asr_confidence=0.92,
            speaker_confidence=0.60,
        ),
        RawSpeakerSegment(
            segment_id="s3",
            raw_speaker_label="unknown_2",
            text="Oke tot morgen.",
            start_ms=3800,
            end_ms=4800,
            asr_confidence=0.88,
            speaker_confidence=0.60,
        ),
    )
    labeled = separate_speaker_turns(
        segments,
        capture_mode=CaptureMode.CONSENTED_CONVERSATION,
        method=SeparationMethod.LEARNER_LABELED,
        learner_turn_labels=(
            LearnerTurnLabel(segment_id="s1", assigned_speaker="learner"),
            LearnerTurnLabel(segment_id="s2", assigned_speaker="other"),
        ),
    )
    assert labeled.learner_turn_count == 1
    assert labeled.excluded_other_turn_count == 1
    assert labeled.ambiguous_turn_count == 1
    assert labeled.review_required

    memo_seg = (
        RawSpeakerSegment(
            segment_id="m1",
            raw_speaker_label="   ",
            text="Ik bel morgen over de reparatie om tien uur.",
            start_ms=0,
            end_ms=2900,
            asr_confidence=0.93,
            speaker_confidence=1.0,
        ),
        RawSpeakerSegment(
            segment_id="m2",
            raw_speaker_label="other",
            text="Achtergrond stem.",
            start_ms=3000,
            end_ms=4000,
            asr_confidence=0.93,
            speaker_confidence=1.0,
        ),
        RawSpeakerSegment(
            segment_id="m3",
            raw_speaker_label="mystery",
            text="Onduidelijke stem.",
            start_ms=4100,
            end_ms=5000,
            asr_confidence=0.93,
            speaker_confidence=0.40,
        ),
    )
    memo_res = separate_speaker_turns(
        memo_seg,
        capture_mode=CaptureMode.SELF_MEMO,
        method=SeparationMethod.SELF_ONLY,
    )
    assert memo_res.learner_turn_count == 1
    assert memo_res.excluded_other_turn_count == 1
    assert memo_res.ambiguous_turn_count == 1
    assert memo_res.review_required
    assert "self_memo_requires_learner_review" in memo_res.reason_codes


def test_validation_errors_and_separate_and_minimize_pipeline() -> None:
    valid_seg = RawSpeakerSegment(
        segment_id="v1",
        raw_speaker_label="self",
        text="Ik wil de afspraak om tien uur bevestigen.",
        start_ms=0,
        end_ms=2500,
        asr_confidence=0.91,
    )
    other_seg = RawSpeakerSegment(
        segment_id="v2",
        raw_speaker_label="other",
        text="Geheime zin van de andere spreker.",
        start_ms=2800,
        end_ms=4500,
        asr_confidence=0.95,
    )

    for bad_seg, match in (
        (
            RawSpeakerSegment("x1", "self", "Hallo", 2000, 1000, 0.9),
            "timing is invalid",
        ),
        (
            RawSpeakerSegment("   ", "self", "Hallo", 0, 1000, 0.9),
            "Segment ID cannot be empty",
        ),
        (
            RawSpeakerSegment("x2", "self", "   ", 0, 1000, 0.9),
            "text cannot be empty",
        ),
        (
            RawSpeakerSegment("x3", "self", "Hallo", 0, 1000, 1.5),
            "confidences must be between zero and one",
        ),
    ):
        with pytest.raises(ValueError, match=match):
            separate_speaker_turns(
                (bad_seg,),
                capture_mode=CaptureMode.CONSENTED_CONVERSATION,
                method=SeparationMethod.BEE_SPEAKER_LABELS,
            )

    with pytest.raises(ValueError, match="At least one raw speaker segment"):
        separate_speaker_turns(
            (),
            capture_mode=CaptureMode.CONSENTED_CONVERSATION,
            method=SeparationMethod.BEE_SPEAKER_LABELS,
        )
    with pytest.raises(ValueError, match="Duplicate segment_id"):
        separate_speaker_turns(
            (valid_seg, valid_seg),
            capture_mode=CaptureMode.CONSENTED_CONVERSATION,
            method=SeparationMethod.BEE_SPEAKER_LABELS,
        )
    with pytest.raises(ValueError, match="confidence threshold"):
        separate_speaker_turns(
            (valid_seg,),
            capture_mode=CaptureMode.CONSENTED_CONVERSATION,
            method=SeparationMethod.BEE_SPEAKER_LABELS,
            policy=SpeakerSeparationPolicy(min_speaker_confidence=1.2),
        )
    with pytest.raises(ValueError, match="self_memo_rejects_conversation_diarization"):
        separate_speaker_turns(
            (valid_seg,),
            capture_mode=CaptureMode.SELF_MEMO,
            method=SeparationMethod.BEE_SPEAKER_LABELS,
        )
    with pytest.raises(ValueError, match="consented_conversation_rejects_self_only"):
        separate_speaker_turns(
            (valid_seg,),
            capture_mode=CaptureMode.CONSENTED_CONVERSATION,
            method=SeparationMethod.SELF_ONLY,
        )
    with pytest.raises(ValueError, match="require learner_labeled"):
        separate_speaker_turns(
            (valid_seg,),
            capture_mode=CaptureMode.CONSENTED_CONVERSATION,
            method=SeparationMethod.BEE_SPEAKER_LABELS,
            learner_turn_labels=(LearnerTurnLabel("v1", "learner"),),
        )
    with pytest.raises(ValueError, match="unknown segment_id"):
        separate_speaker_turns(
            (valid_seg,),
            capture_mode=CaptureMode.CONSENTED_CONVERSATION,
            method=SeparationMethod.LEARNER_LABELED,
            learner_turn_labels=(LearnerTurnLabel("missing", "learner"),),
        )
    with pytest.raises(ValueError, match="Duplicate learner turn label"):
        separate_speaker_turns(
            (valid_seg,),
            capture_mode=CaptureMode.CONSENTED_CONVERSATION,
            method=SeparationMethod.LEARNER_LABELED,
            learner_turn_labels=(
                LearnerTurnLabel("v1", "learner"),
                LearnerTurnLabel("v1", "exclude"),
            ),
        )

    recorded = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    with pytest.raises(ConsentError):
        separate_and_minimize_transcript(
            reference="ref-no-all-consent",
            learner_id="01000000000000000000000002",
            recorded_at=recorded,
            capture_mode=CaptureMode.CONSENTED_CONVERSATION,
            situation=Situation.REPAIR,
            segments=(valid_seg, other_seg),
            method=SeparationMethod.BEE_SPEAKER_LABELS,
            consent_attested=True,
            all_participants_consented=False,
            revoked=False,
            retention_policy_present=True,
        )

    sep, minimized = separate_and_minimize_transcript(
        reference="ref-ok-1",
        learner_id="01000000000000000000000002",
        recorded_at=recorded,
        capture_mode=CaptureMode.CONSENTED_CONVERSATION,
        situation=Situation.REPAIR,
        segments=(valid_seg, other_seg),
        method=SeparationMethod.BEE_SPEAKER_LABELS,
        consent_attested=True,
        all_participants_consented=True,
        revoked=False,
        retention_policy_present=True,
    )
    assert sep.excluded_other_turn_count == 1
    assert not minimized.other_speaker_transcript_retained
    assert len(minimized.self_excerpts) == 1
    assert "Geheime zin" not in minimized.self_excerpts[0].text


def test_review_service_redact_breakdown_excerpt(
    attempt_document: dict[str, Any],
    rubric: Rubric,
) -> None:
    doc = deepcopy(attempt_document)
    doc["status"] = "awaiting_learner_review"
    doc["version"] = 1
    learner_id = str(doc["learner_id"])
    breakdown_id = str(doc["breakdowns"][0]["breakdown_id"])

    svc = AttemptReviewService(doc, rubric)
    res = svc.redact_breakdown_excerpt(
        actor_learner_id=learner_id,
        expected_version=1,
        breakdown_id=breakdown_id,
        reviewed_at=datetime(2026, 10, 24, 10, 0, tzinfo=UTC),
    )
    assert res.document["breakdowns"][0]["self_evidence_excerpt"] == "[redacted]"
    assert res.document["version"] == 2
    # Idempotent repeat redaction keeps version unchanged
    res2 = svc.redact_breakdown_excerpt(
        actor_learner_id=learner_id,
        expected_version=2,
        breakdown_id=breakdown_id,
        reviewed_at=datetime(2026, 10, 24, 10, 1, tzinfo=UTC),
    )
    assert res2.document["version"] == 2
