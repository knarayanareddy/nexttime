from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from .breakdowns import deterministic_ulid


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Haptic event timestamp must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class HapticPulsePattern(StrEnum):
    SHORT_DOUBLE = "SHORT_DOUBLE"
    STEADY_BUZZ = "STEADY_BUZZ"


class RehearsalPacingTrigger(StrEnum):
    PTT_RECORDING_STARTED = "ptt_recording_started"
    PTT_RECORDING_RELEASED = "ptt_recording_released"
    COUNTERPART_TURN_READY = "counterpart_turn_ready"
    SUPPORT_REPAIR_OFFERED = "support_repair_offered"
    TURN_BUDGET_BOUNDARY = "turn_budget_boundary"
    REHEARSAL_COMPLETED = "rehearsal_completed"
    MID_SPEECH_PACING_INTERRUPT = "mid_speech_pacing_interrupt"


@dataclass(frozen=True)
class HapticPreferences:
    haptics_enabled: bool = True
    reduced_motion: bool = False


@dataclass(frozen=True)
class HapticPulseSequence:
    pattern: HapticPulsePattern
    pulse_durations_ms: tuple[int, ...]
    gap_durations_ms: tuple[int, ...]
    total_duration_ms: int


@dataclass(frozen=True)
class HapticFeedbackEvent:
    event_id: str
    rehearsal_id: str
    turn_id: str | None
    trigger: RehearsalPacingTrigger
    pattern: HapticPulsePattern | None
    sequence: HapticPulseSequence | None
    target_device: Literal["ios_rehearsal_client"]
    emitted: bool
    suppression_reason: str | None
    visual_caption_key: str
    voiceover_announcement: str
    duplicate: bool
    created_at: str


_SEQUENCES: dict[HapticPulsePattern, HapticPulseSequence] = {
    HapticPulsePattern.SHORT_DOUBLE: HapticPulseSequence(
        pattern=HapticPulsePattern.SHORT_DOUBLE,
        pulse_durations_ms=(60, 60),
        gap_durations_ms=(80,),
        total_duration_ms=200,
    ),
    HapticPulsePattern.STEADY_BUZZ: HapticPulseSequence(
        pattern=HapticPulsePattern.STEADY_BUZZ,
        pulse_durations_ms=(180,),
        gap_durations_ms=(),
        total_duration_ms=180,
    ),
}

_TRIGGER_SPECS: dict[RehearsalPacingTrigger, tuple[HapticPulsePattern | None, str, str]] = {
    RehearsalPacingTrigger.PTT_RECORDING_STARTED: (
        HapticPulsePattern.STEADY_BUZZ,
        "recording_active",
        "Recording started. Speak in Dutch and release when finished.",
    ),
    RehearsalPacingTrigger.PTT_RECORDING_RELEASED: (
        HapticPulsePattern.SHORT_DOUBLE,
        "processing_speech",
        "Recording stopped. Processing speech.",
    ),
    RehearsalPacingTrigger.COUNTERPART_TURN_READY: (
        HapticPulsePattern.SHORT_DOUBLE,
        "counterpart_ready",
        "Counterpart prompt ready.",
    ),
    RehearsalPacingTrigger.SUPPORT_REPAIR_OFFERED: (
        HapticPulsePattern.SHORT_DOUBLE,
        "phrase_support_inline",
        "Support phrase available. Retry when ready.",
    ),
    RehearsalPacingTrigger.TURN_BUDGET_BOUNDARY: (
        HapticPulsePattern.STEADY_BUZZ,
        "final_turn_window",
        "Final turn of your 90-second practice.",
    ),
    RehearsalPacingTrigger.REHEARSAL_COMPLETED: (
        HapticPulsePattern.SHORT_DOUBLE,
        "practice_completed",
        "Practice completed.",
    ),
    RehearsalPacingTrigger.MID_SPEECH_PACING_INTERRUPT: (
        None,
        "recording_active",
        "Recording continues uninterrupted.",
    ),
}

_DEFAULT_PREFERENCES = HapticPreferences()


def pulse_sequence_for(pattern: HapticPulsePattern) -> HapticPulseSequence:
    return _SEQUENCES[pattern]


def map_trigger_to_pattern(trigger: RehearsalPacingTrigger) -> HapticPulsePattern | None:
    return _TRIGGER_SPECS[trigger][0]


class RehearsalHapticGenerator:
    """Maps bounded iPhone rehearsal turn-boundary triggers to supplementary haptic pulses.

    Enforces SPEC.md §1.3, §4.1, §8.2, INT-NT-003, and ui/DESIGN.md §6–7:
    - Never targets unverified Bee wearable haptic commands or live conversations.
    - Never interrupts mid-speech or fires repeated haptics while recording is active.
    - Respects learner haptic/accessibility settings and always pairs with visual/VoiceOver cues.
    """

    def __init__(self, *, rehearsal_id: str) -> None:
        cleaned = rehearsal_id.strip()
        if not cleaned:
            raise ValueError("rehearsal_id is required")
        self._rehearsal_id = cleaned
        self._recording_active = False
        self._events_by_key: dict[str, tuple[tuple[object, ...], HapticFeedbackEvent]] = {}

    @property
    def recording_active(self) -> bool:
        return self._recording_active

    def generate(
        self,
        *,
        source_event_id: str,
        trigger: RehearsalPacingTrigger,
        occurred_at: datetime,
        turn_id: str | None = None,
        target_device: str = "ios_rehearsal_client",
        preferences: HapticPreferences = _DEFAULT_PREFERENCES,
    ) -> HapticFeedbackEvent:
        cleaned_source = source_event_id.strip()
        if not cleaned_source:
            raise ValueError("source_event_id is required")
        if target_device != "ios_rehearsal_client":
            raise ValueError("unverified_bee_haptic_command_prohibited")
        occurred_iso = _iso_utc(occurred_at)

        signature = (
            trigger,
            turn_id,
            target_device,
            preferences.haptics_enabled,
            preferences.reduced_motion,
        )
        existing = self._events_by_key.get(cleaned_source)
        if existing is not None:
            saved_sig, saved_event = existing
            if saved_sig != signature:
                raise ValueError("haptic_event_replay_conflict")
            return replace(saved_event, duplicate=True)

        pattern, caption_key, voiceover = _TRIGGER_SPECS[trigger]
        suppression_reason: str | None = None

        if trigger is RehearsalPacingTrigger.MID_SPEECH_PACING_INTERRUPT:
            suppression_reason = "real_time_interruption_prohibited"
            pattern = None
        elif (
            self._recording_active and trigger is not RehearsalPacingTrigger.PTT_RECORDING_RELEASED
        ):
            suppression_reason = "repeated_haptic_during_recording_prohibited"
            pattern = None
        elif not preferences.haptics_enabled:
            suppression_reason = "system_haptics_disabled"

        # Transition push-to-talk recording state only on valid start/release boundaries
        if trigger is RehearsalPacingTrigger.PTT_RECORDING_STARTED and not self._recording_active:
            self._recording_active = True
        elif trigger is RehearsalPacingTrigger.PTT_RECORDING_RELEASED:
            self._recording_active = False

        emitted = suppression_reason is None and pattern is not None
        sequence = _SEQUENCES[pattern] if (emitted and pattern is not None) else None
        event_id = deterministic_ulid(
            namespace="haptic-event",
            seed=f"{self._rehearsal_id}:{cleaned_source}",
            timestamp=occurred_at,
        )

        event = HapticFeedbackEvent(
            event_id=event_id,
            rehearsal_id=self._rehearsal_id,
            turn_id=turn_id,
            trigger=trigger,
            pattern=pattern if emitted else None,
            sequence=sequence,
            target_device="ios_rehearsal_client",
            emitted=emitted,
            suppression_reason=suppression_reason,
            visual_caption_key=caption_key,
            voiceover_announcement=voiceover,
            duplicate=False,
            created_at=occurred_iso,
        )
        self._events_by_key[cleaned_source] = (signature, event)
        return event
