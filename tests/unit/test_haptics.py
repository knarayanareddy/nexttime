from __future__ import annotations

from datetime import UTC, datetime

import pytest
from nexttime_domain import (
    HapticPreferences,
    HapticPulsePattern,
    RehearsalHapticGenerator,
    RehearsalPacingTrigger,
    map_trigger_to_pattern,
    pulse_sequence_for,
)


def test_pulse_sequences_and_trigger_pattern_mapping() -> None:
    short_double = pulse_sequence_for(HapticPulsePattern.SHORT_DOUBLE)
    assert short_double.pulse_durations_ms == (60, 60)
    assert short_double.gap_durations_ms == (80,)
    assert short_double.total_duration_ms == 200

    steady_buzz = pulse_sequence_for(HapticPulsePattern.STEADY_BUZZ)
    assert steady_buzz.pulse_durations_ms == (180,)
    assert steady_buzz.gap_durations_ms == ()
    assert steady_buzz.total_duration_ms == 180

    assert (
        map_trigger_to_pattern(RehearsalPacingTrigger.PTT_RECORDING_STARTED)
        is HapticPulsePattern.STEADY_BUZZ
    )
    assert (
        map_trigger_to_pattern(RehearsalPacingTrigger.TURN_BUDGET_BOUNDARY)
        is HapticPulsePattern.STEADY_BUZZ
    )
    assert (
        map_trigger_to_pattern(RehearsalPacingTrigger.PTT_RECORDING_RELEASED)
        is HapticPulsePattern.SHORT_DOUBLE
    )
    assert (
        map_trigger_to_pattern(RehearsalPacingTrigger.COUNTERPART_TURN_READY)
        is HapticPulsePattern.SHORT_DOUBLE
    )
    assert (
        map_trigger_to_pattern(RehearsalPacingTrigger.SUPPORT_REPAIR_OFFERED)
        is HapticPulsePattern.SHORT_DOUBLE
    )
    assert (
        map_trigger_to_pattern(RehearsalPacingTrigger.REHEARSAL_COMPLETED)
        is HapticPulsePattern.SHORT_DOUBLE
    )
    assert map_trigger_to_pattern(RehearsalPacingTrigger.MID_SPEECH_PACING_INTERRUPT) is None


def test_haptic_generator_ptt_lifecycle_and_no_repeated_recording_pulses() -> None:
    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    gen = RehearsalHapticGenerator(rehearsal_id="reh_001")
    assert not gen.recording_active

    started = gen.generate(
        source_event_id="evt-1",
        trigger=RehearsalPacingTrigger.PTT_RECORDING_STARTED,
        occurred_at=now,
        turn_id="turn_1",
    )
    assert gen.recording_active
    assert started.emitted is True
    assert started.pattern is HapticPulsePattern.STEADY_BUZZ
    assert started.sequence is not None
    assert started.sequence.total_duration_ms == 180
    assert started.suppression_reason is None
    assert started.duplicate is False

    # While recording is active, repeated haptic patterns MUST be suppressed (ui/DESIGN.md §6)
    repeated = gen.generate(
        source_event_id="evt-2",
        trigger=RehearsalPacingTrigger.TURN_BUDGET_BOUNDARY,
        occurred_at=now,
        turn_id="turn_1",
    )
    assert repeated.emitted is False
    assert repeated.pattern is None
    assert repeated.sequence is None
    assert repeated.suppression_reason == "repeated_haptic_during_recording_prohibited"
    assert gen.recording_active

    # Releasing push-to-talk emits SHORT_DOUBLE and clears recording_active
    released = gen.generate(
        source_event_id="evt-3",
        trigger=RehearsalPacingTrigger.PTT_RECORDING_RELEASED,
        occurred_at=now,
        turn_id="turn_1",
    )
    assert not gen.recording_active
    assert released.emitted is True
    assert released.pattern is HapticPulsePattern.SHORT_DOUBLE
    assert released.sequence is not None
    assert released.sequence.pulse_durations_ms == (60, 60)

    # Subsequent turn-boundary cues emit cleanly once recording has ended
    completed = gen.generate(
        source_event_id="evt-4",
        trigger=RehearsalPacingTrigger.REHEARSAL_COMPLETED,
        occurred_at=now,
    )
    assert completed.emitted is True
    assert completed.pattern is HapticPulsePattern.SHORT_DOUBLE


def test_haptic_generator_prohibits_bee_wearable_and_mid_speech_interruption() -> None:
    now = datetime(2026, 10, 24, 12, 5, tzinfo=UTC)
    gen = RehearsalHapticGenerator(rehearsal_id="reh_002")

    # Never invent or target Bee wearable haptic commands (SPEC.md §4.1, INT-NT-003)
    with pytest.raises(ValueError, match="unverified_bee_haptic_command_prohibited"):
        gen.generate(
            source_event_id="evt-bee",
            trigger=RehearsalPacingTrigger.COUNTERPART_TURN_READY,
            occurred_at=now,
            target_device="bee_wearable",
        )

    # Real-time mid-speech pacing interruption is prohibited (SPEC.md §1.3, §8.2)
    interrupted = gen.generate(
        source_event_id="evt-mid",
        trigger=RehearsalPacingTrigger.MID_SPEECH_PACING_INTERRUPT,
        occurred_at=now,
    )
    assert interrupted.emitted is False
    assert interrupted.pattern is None
    assert interrupted.sequence is None
    assert interrupted.suppression_reason == "real_time_interruption_prohibited"
    assert interrupted.visual_caption_key == "recording_active"
    assert interrupted.voiceover_announcement


def test_haptic_preferences_idempotency_and_validation() -> None:
    now = datetime(2026, 10, 24, 12, 10, tzinfo=UTC)
    with pytest.raises(ValueError, match="rehearsal_id is required"):
        RehearsalHapticGenerator(rehearsal_id="   ")

    gen = RehearsalHapticGenerator(rehearsal_id="reh_003")

    with pytest.raises(ValueError, match="source_event_id is required"):
        gen.generate(
            source_event_id="  ",
            trigger=RehearsalPacingTrigger.COUNTERPART_TURN_READY,
            occurred_at=now,
        )

    with pytest.raises(ValueError, match="timezone"):
        gen.generate(
            source_event_id="evt-naive",
            trigger=RehearsalPacingTrigger.COUNTERPART_TURN_READY,
            occurred_at=datetime(2026, 10, 24, 12, 10),
        )

    # Disabled haptics suppress tactile pulse while retaining visual + VoiceOver cues
    muted = gen.generate(
        source_event_id="evt-muted",
        trigger=RehearsalPacingTrigger.SUPPORT_REPAIR_OFFERED,
        occurred_at=now,
        turn_id="turn_2",
        preferences=HapticPreferences(haptics_enabled=False, reduced_motion=True),
    )
    assert muted.emitted is False
    assert muted.pattern is None
    assert muted.sequence is None
    assert muted.suppression_reason == "system_haptics_disabled"
    assert muted.visual_caption_key == "phrase_support_inline"
    assert "Support phrase" in muted.voiceover_announcement

    # Exact replay returns duplicate=True with same event_id
    replayed = gen.generate(
        source_event_id="evt-muted",
        trigger=RehearsalPacingTrigger.SUPPORT_REPAIR_OFFERED,
        occurred_at=now,
        turn_id="turn_2",
        preferences=HapticPreferences(haptics_enabled=False, reduced_motion=True),
    )
    assert replayed.duplicate is True
    assert replayed.event_id == muted.event_id

    # Conflicting replay with same source_event_id raises error
    with pytest.raises(ValueError, match="haptic_event_replay_conflict"):
        gen.generate(
            source_event_id="evt-muted",
            trigger=RehearsalPacingTrigger.REHEARSAL_COMPLETED,
            occurred_at=now,
        )
