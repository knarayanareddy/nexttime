from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthResponse(StrictModel):
    status: Literal["ok"]
    service: str
    version: str


class ConsentCreateRequest(StrictModel):
    consent_id: str = Field(min_length=26, max_length=26)
    capture_mode: Literal["consented_conversation", "self_memo"]
    policy_version: str = Field(default="consent_v1", pattern=r"^consent_v[0-9]+$")
    all_participants_consented: bool
    attested_at: datetime


class ConsentResponse(StrictModel):
    consent_id: str
    learner_id: str
    capture_mode: str
    policy_version: str
    all_participants_consented: bool
    attested_at: str
    revoked: bool
    revoked_at: str | None
    version: int


class StageReceiptResponse(StrictModel):
    stage: str
    deleted_count: int
    verified_empty: bool
    completed_at: str
    receipt_id: str
    error_code: str | None


class DeletionJobResponse(StrictModel):
    job_id: str
    learner_id: str
    scope: str
    target_attempt_ids: list[str]
    consent_id: str | None
    status: str
    stage_receipts: list[StageReceiptResponse]
    alarm_triggered: bool
    alarm_codes: list[str]
    created_at: str
    updated_at: str
    completed_at: str | None


class ConsentRevokeResponse(StrictModel):
    consent: ConsentResponse
    deletion_job: DeletionJobResponse


class BridgeSegmentPayload(StrictModel):
    segment_id: str = Field(min_length=1, max_length=64)
    speaker_label: str | None = Field(default=None, max_length=64)
    text: str = Field(min_length=1, max_length=2000)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=1)
    asr_confidence: float = Field(ge=0.0, le=1.0)
    speaker_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    overlapping_speech: bool = False


class AttemptIngestRequest(StrictModel):
    learner_id: str = Field(min_length=26, max_length=26)
    consent_id: str = Field(min_length=26, max_length=26)
    bee_recording_reference: str = Field(min_length=1, max_length=256)
    recorded_at: datetime
    capture_mode: Literal["consented_conversation", "self_memo"]
    device_type: Literal["bee_device", "apple_watch_bee"] = "bee_device"
    provenance: Literal["live", "fixture"] = "fixture"
    situation: Literal["repair_appointment", "school_admin_appointment", "shift_clarification"]
    learner_goal: str = Field(min_length=1, max_length=300)
    separation_method: (
        Literal["bee_speaker_labels", "aws_diarization", "learner_labeled", "self_only"] | None
    ) = None
    enrolled_diarization_speaker_id: str | None = None
    recurrence_id: str | None = Field(default=None, min_length=26, max_length=26)
    segments: list[BridgeSegmentPayload] = Field(min_length=1, max_length=200)


class AttemptIngestResponse(StrictModel):
    status: Literal["ingested", "duplicate"]
    attempt_id: str
    learner_review_required: bool
    attempt: dict[str, Any]


class AttemptListResponse(StrictModel):
    items: list[dict[str, Any]]
    next_cursor: str | None


class BreakdownDecisionInput(StrictModel):
    breakdown_id: str = Field(min_length=26, max_length=26)
    decision: Literal["confirmed", "rejected"]
    redact_excerpt: bool = False


class CriterionCorrectionInput(StrictModel):
    criterion_id: str = Field(min_length=1, max_length=64)
    result: Literal["satisfied", "not_satisfied", "unknown"]
    self_evidence_excerpt: str = Field(max_length=500)
    self_evidence_start_ms: int | None = Field(default=None, ge=0)
    self_evidence_end_ms: int | None = Field(default=None, ge=1)


class AttemptReviewRequest(StrictModel):
    expected_version: int = Field(ge=1)
    breakdown_decisions: list[BreakdownDecisionInput] = Field(default_factory=list)
    criterion_corrections: list[CriterionCorrectionInput] = Field(default_factory=list)
    confirm_attempt: bool = True
    interaction_ended: bool = True
    assistance_used: bool = False


class AttemptReviewResponse(StrictModel):
    attempt: dict[str, Any]
    skill_updates: list[str]


class AttemptExcludeRequest(StrictModel):
    expected_version: int = Field(ge=1)
    reason: Literal["learner_excluded", "consent_revoked"] = "learner_excluded"
    delete_now: bool = False


class RecurrenceCreateRequest(StrictModel):
    attempt_id: str = Field(min_length=26, max_length=26)
    scheduled_for: datetime
    rehearsal_at: datetime | None = None
    time_zone: str = Field(default="Europe/Amsterdam", min_length=1, max_length=64)
    learner_confirmed: bool = True
    notification_behavior: Literal["local_notification", "open_template_only"] = (
        "local_notification"
    )


class CalendarLinkRequest(StrictModel):
    calendar_event_reference: str = Field(min_length=1, max_length=256)
    scheduled_for: datetime | None = None


class RecurrenceResponse(StrictModel):
    recurrence_id: str
    learner_id: str
    attempt_id: str
    situation: str
    scheduled_for: str
    rehearsal_at: str | None
    time_zone: str
    source: Literal["learner_marked", "explicit_calendar_link"]
    calendar_event_reference: str | None
    quiet_hours: bool
    selection_required: bool
    notification_behavior: str | None


class RehearsalCreateRequest(StrictModel):
    breakdown_id: str = Field(min_length=26, max_length=26)


class CounterpartAudioClip(StrictModel):
    turn_id: str
    signed_url: str
    expires_at: str


class ScenarioTurnResponse(StrictModel):
    turn_id: str
    counterpart_text_nl: str
    counterpart_text_en: str
    expected_functions: list[str]
    accepted_phrase_ids: list[str]
    repair_if_missing: str
    counterpart_audio: CounterpartAudioClip


class SupportPhraseResponse(StrictModel):
    phrase_id: str
    text_nl: str
    text_en: str


class RehearsalSessionResponse(StrictModel):
    rehearsal_id: str
    recurrence_id: str
    attempt_id: str
    learner_id: str
    situation: str
    target_function: str
    estimated_seconds: int
    state: str
    current_turn_id: str | None
    committed_turn_count: int
    non_speaking_practice: bool
    template_version: str
    model_generated: bool
    counterpart_persona: str
    voice_id: str
    speaking_rate: str
    turns: list[ScenarioTurnResponse]
    support_phrases: list[SupportPhraseResponse]
    safety_exclusions: list[str]


class RehearsalTurnSubmitRequest(StrictModel):
    turn_event_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=32)
    sequence: int = Field(ge=1)
    transcript: str = Field(default="", max_length=500)
    phrase_id: str | None = Field(default=None, max_length=64)
    confidence: float = Field(ge=0.0, le=1.0)
    mode: Literal["speech", "text", "tap"] = "speech"
    save_self_excerpt: bool = False


class RehearsalTurnSubmitResponse(StrictModel):
    rehearsal_id: str
    state: str
    current_turn_id: str | None
    mapped_function: str | None
    support: Literal["repair", "easier_alternative"] | None
    committed_turn_count: int
    non_speaking_practice: bool
    duplicate: bool
    raw_audio_deleted: bool
    transcript_delete_after: str | None
    authentic_mastery_updated: Literal[False]


class RehearsalCompleteRequest(StrictModel):
    final_turn_sequence: int = Field(ge=0)
    exit_early: bool = False


class SkillStateResponse(StrictModel):
    skill_code: str
    alpha: float
    beta: float
    mastery: float
    uncertainty: float
    authentic_attempt_count: int


class CriterionDeltaResponse(StrictModel):
    criterion_id: str
    prior_result: str
    next_result: str
    delta: str
    prior_self_evidence: str
    next_self_evidence: str


class OutcomeComparisonResponse(StrictModel):
    prior_attempt_id: str
    next_attempt_id: str
    learner_id: str
    recurrence_id: str
    situation: str
    rubric_version: str
    criterion_deltas: list[CriterionDeltaResponse]
    functional_progress: str
    comparison_confidence: str
    uncertainty_codes: list[str]
    causal_claim: Literal[False]


class FunctionalWinResponse(StrictModel):
    prior_attempt_id: str
    next_attempt_id: str
    criterion_id: str
    kind: str
    self_evidence_excerpt: str


class ProgressHistoryResponse(StrictModel):
    learner_id: str
    authentic_attempts: list[dict[str, Any]]
    comparisons: list[OutcomeComparisonResponse]
    functional_wins: list[FunctionalWinResponse]
    skill_states: list[SkillStateResponse]
    rehearsal_history: list[dict[str, Any]]
    causal_claim: Literal[False]
    gamified_streaks: Literal[False]


class ExportResponse(StrictModel):
    schema_version: Literal[1]
    learner_id: str
    exported_at: str
    consents: list[dict[str, Any]]
    attempts: list[dict[str, Any]]
    recurrences: list[dict[str, Any]]
    rehearsal_history: list[dict[str, Any]]
    comparisons: list[dict[str, Any]]
    skill_states: dict[str, dict[str, Any]]
    other_speaker_transcript_included: Literal[False]


class CalendarIcsSyncRequest(StrictModel):
    ics_payload: str = Field(min_length=15, max_length=262_144)
    default_time_zone: str = Field(default="Europe/Amsterdam", min_length=1, max_length=64)
    horizon_days: int = Field(default=14, ge=1, le=60)
    provenance: Literal["fixture", "live"] = "fixture"


class CalendarCandidateResponse(StrictModel):
    candidate_id: str
    learner_id: str
    provider: Literal["icalendar", "google_calendar"]
    calendar_event_reference: str
    situation: str
    matched_attempt_id: str
    matched_breakdown_id: str
    scheduled_for: str
    proposed_rehearsal_at: str
    time_zone: str
    quiet_hours: bool
    confirmation_state: Literal["awaiting_learner_confirmation", "confirmed", "dismissed"]
    provenance: Literal["fixture", "live"]
    created_at: str


class CalendarSyncSummaryResponse(StrictModel):
    learner_id: str
    provider: Literal["icalendar", "google_calendar"]
    candidates: list[CalendarCandidateResponse]
    excluded_sensitive_count: int
    ignored_unrelated_count: int
    cancelled_count: int
    next_sync_token: str | None
    provenance: Literal["fixture", "live"]


class CalendarCandidateListResponse(StrictModel):
    items: list[CalendarCandidateResponse]


class CalendarCandidateConfirmRequest(StrictModel):
    rehearsal_at: datetime | None = None
    notification_behavior: Literal["local_notification", "open_template_only"] | None = None


class PreMeetingRehearsalCardResponse(StrictModel):
    card_id: str
    learner_id: str
    candidate_id: str
    recurrence_id: str
    attempt_id: str
    breakdown_id: str
    situation: str
    target_function: str
    scheduled_for: str
    rehearsal_at: str
    time_zone: str
    estimated_seconds: int
    quiet_hours: bool
    notification_behavior: str
    event_source: Literal["explicit_calendar_link"]
    calendar_event_reference: str
    status: Literal["queued", "cancelled"]
    provenance: Literal["fixture", "live"]


class RehearsalCardListResponse(StrictModel):
    items: list[PreMeetingRehearsalCardResponse]


class PcmChunkPayload(StrictModel):
    sequence_id: int = Field(ge=1)
    pcm_base64: str = Field(min_length=4, max_length=100_000)
    sample_rate_hz: int = 16_000
    channels: int = 1
    sample_width_bytes: int = 2


class TranscribeChunksRequest(StrictModel):
    turn_event_id: str = Field(min_length=1, max_length=128)
    streaming_transcription_consented: bool
    capture_origin: str = Field(default="iphone_rehearsal_ptt", max_length=64)
    continuous_stream: bool = False
    save_self_excerpt: bool = False
    chunks: list[PcmChunkPayload] = Field(min_length=1, max_length=64)


class ChunkDeletionReceiptResponse(StrictModel):
    sequence_id: int
    bytes_zeroed: int
    deleted_immediately: Literal[True]
    deleted_at: str


class TurnTranscriptionResponse(StrictModel):
    turn_event_id: str
    rehearsal_id: str
    learner_id: str
    transcript: str
    confidence: float
    below_confidence_threshold: bool
    partial_count: int
    chunk_receipts: list[ChunkDeletionReceiptResponse]
    retained_pcm_bytes: Literal[0]
    transcript_delete_after: str | None
    provider: Literal["aws_transcribe_streaming"]
    provenance: Literal["fixture", "live"]


class HapticEventRequest(StrictModel):
    source_event_id: str = Field(min_length=1, max_length=128)
    trigger: Literal[
        "ptt_recording_started",
        "ptt_recording_released",
        "counterpart_turn_ready",
        "support_repair_offered",
        "turn_budget_boundary",
        "rehearsal_completed",
        "mid_speech_pacing_interrupt",
    ]
    turn_id: str | None = Field(default=None, max_length=32)
    target_device: str = Field(default="ios_rehearsal_client", max_length=64)
    haptics_enabled: bool = True
    reduced_motion: bool = False


class HapticPulseSequenceResponse(StrictModel):
    pattern: Literal["SHORT_DOUBLE", "STEADY_BUZZ"]
    pulse_durations_ms: list[int]
    gap_durations_ms: list[int]
    total_duration_ms: int


class HapticFeedbackEventResponse(StrictModel):
    event_id: str
    rehearsal_id: str
    turn_id: str | None
    trigger: str
    pattern: Literal["SHORT_DOUBLE", "STEADY_BUZZ"] | None
    sequence: HapticPulseSequenceResponse | None
    target_device: Literal["ios_rehearsal_client"]
    emitted: bool
    suppression_reason: str | None
    visual_caption_key: str
    voiceover_announcement: str
    duplicate: bool
    created_at: str
