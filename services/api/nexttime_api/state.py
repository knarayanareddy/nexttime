from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nexttime_bee_adapter import BeeTranscript, LocalSpoolBeeAdapter
from nexttime_bee_bridge import BeeIngestionBridge, CaptureMetadata
from nexttime_calendar import (
    CalendarAdapterError,
    CalendarRecurrenceCandidate,
    CalendarSyncService,
    ICalendarFeedAdapter,
    PreMeetingRehearsalCard,
)
from nexttime_domain import (
    AttemptReviewService,
    AuthenticProgressService,
    BreakdownReviewDecision,
    CaptureMode,
    ConsentError,
    ConsentGrant,
    CriterionCorrection,
    CriterionResult,
    DeletionJob,
    FixtureRehearsalService,
    HapticFeedbackEvent,
    HapticPreferences,
    NotificationBehavior,
    PacingHapticsEngine,
    PracticeMode,
    RawSpeakerSegment,
    RehearsalPacingTrigger,
    Rubric,
    SeparationMethod,
    Situation,
    SkillState,
    SourceTranscript,
    Speaker,
    SpeakerSeparationPolicy,
    TranscriptTurn,
    build_learner_export,
    deterministic_ulid,
    minimize_transcript,
    rubric_from_mapping,
)
from nexttime_minimization import DeletionPipelineWorker, InMemoryArtifactStore
from nexttime_model_gateway import GatewayBreakdownExtractor, ModelGateway
from nexttime_rehearsal import (
    AudioChunkTranscriber,
    CovertCaptureProhibitedError,
    DeterministicFixtureTranscribeTransport,
    PcmAudioChunk,
    TranscribeTransportError,
)

from .auth import TokenAuthenticator
from .idempotency import IdempotencyStore
from .problem import ApiProblemError
from .schemas import (
    AttemptExcludeRequest,
    AttemptIngestRequest,
    AttemptReviewRequest,
    CalendarCandidateConfirmRequest,
    CalendarIcsSyncRequest,
    CalendarLinkRequest,
    ConsentCreateRequest,
    HapticEventRequest,
    RecurrenceCreateRequest,
    RehearsalCompleteRequest,
    RehearsalCreateRequest,
    RehearsalTurnSubmitRequest,
    TranscribeChunksRequest,
)

ROOT = Path(__file__).resolve().parents[3]


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApiProblemError(
            status=422,
            code="timezone_required",
            title="Timezone-aware timestamp required",
            detail="All API timestamps must include an explicit UTC offset.",
        )
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _map_dutch_transcript_to_phrase_id(
    transcript: str, accepted_phrase_ids: tuple[str, ...]
) -> str | None:
    lowered = transcript.strip().lower()
    if not lowered:
        return None
    keyword_rules: dict[str, tuple[str, ...]] = {
        "goal_repair_basic": ("reparatie", "monteur", "loodgieter"),
        "goal_school_basic": ("schooladministratie", "school"),
        "goal_shift_basic": ("dienst", "volgende stap", "rooster"),
        "confirm_time": (
            "bevestig",
            "datum en de tijd",
            "vrijdag om",
            "maandag om",
            "dinsdag om",
            "om tien uur",
            "om negen uur",
            "om acht uur",
            "om drie uur",
        ),
    }
    for phrase_id in accepted_phrase_ids:
        markers = keyword_rules.get(phrase_id, ())
        needs_afspraak = phrase_id.startswith("goal_") and phrase_id != "goal_shift_basic"
        if needs_afspraak and "afspraak" not in lowered:
            continue
        if any(marker in lowered for marker in markers):
            return phrase_id
    return None


@dataclass
class RehearsalSessionEntry:
    rehearsal_id: str
    recurrence_id: str
    attempt_id: str
    learner_id: str
    breakdown_id: str
    service: FixtureRehearsalService
    haptics: PacingHapticsEngine = field(
        default_factory=lambda: PacingHapticsEngine(rehearsal_id="pending")
    )
    committed_sequences: list[int] = field(default_factory=list)
    recorded_in_progress: bool = False
    ephemeral_simulator: bool = False


class ApiRuntimeState:
    """Self-contained application state coordinating domain services and deletion workers."""

    def __init__(
        self,
        *,
        rubric: Rubric | None = None,
        allow_structured_local_tokens: bool = True,
        clock: datetime | None = None,
        model_gateway: ModelGateway | None = None,
    ) -> None:
        if rubric is None:
            raw_rubric = json.loads((ROOT / "packages" / "rubrics" / "rubric_v1.json").read_text())
            rubric = rubric_from_mapping(raw_rubric)
        self.rubric = rubric
        self.authenticator = TokenAuthenticator(
            allow_structured_local_tokens=allow_structured_local_tokens
        )
        self.idempotency = IdempotencyStore()
        self.store = InMemoryArtifactStore()
        self.deletion_worker = DeletionPipelineWorker(self.store)
        self.bridge = BeeIngestionBridge(
            adapter=LocalSpoolBeeAdapter(ROOT / ".nonexistent-default-spool"),
            store=self.store,
            deletion_worker=self.deletion_worker,
            rubric=self.rubric,
        )
        self.model_gateway = model_gateway or ModelGateway()
        self.breakdown_extractor = GatewayBreakdownExtractor(self.model_gateway)
        self.calendar_sync = CalendarSyncService()
        self.chunk_transcriber = AudioChunkTranscriber(
            transport=DeterministicFixtureTranscribeTransport()
        )
        self._progress_by_learner: dict[str, AuthenticProgressService] = {}
        self._rehearsals: dict[str, RehearsalSessionEntry] = {}
        self._rehearsal_by_recurrence: dict[tuple[str, str], str] = {}
        self._signing_secret = secrets.token_bytes(32)
        self._fixed_clock = clock

    def now(self) -> datetime:
        return (self._fixed_clock or datetime.now(UTC)).astimezone(UTC)

    def _progress_for(self, learner_id: str) -> AuthenticProgressService:
        service = self._progress_by_learner.get(learner_id)
        if service is None:
            service = AuthenticProgressService(self.rubric)
            self._progress_by_learner[learner_id] = service
        return service

    @staticmethod
    def _serialize_job(job: DeletionJob) -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "learner_id": job.learner_id,
            "scope": job.scope.value,
            "target_attempt_ids": list(job.target_attempt_ids),
            "consent_id": job.consent_id,
            "status": job.status.value,
            "stage_receipts": [
                {
                    "stage": r.stage.value,
                    "deleted_count": r.deleted_count,
                    "verified_empty": r.verified_empty,
                    "completed_at": r.completed_at,
                    "receipt_id": r.receipt_id,
                    "error_code": r.error_code,
                }
                for r in job.stage_receipts
            ],
            "alarm_triggered": job.alarm_triggered,
            "alarm_codes": list(job.alarm_codes),
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "completed_at": job.completed_at,
        }

    @staticmethod
    def _serialize_consent(grant: ConsentGrant) -> dict[str, Any]:
        return {
            "consent_id": grant.consent_id,
            "learner_id": grant.learner_id,
            "capture_mode": grant.capture_mode.value,
            "policy_version": grant.policy_version,
            "all_participants_consented": grant.all_participants_consented,
            "attested_at": grant.attested_at,
            "revoked": grant.revoked,
            "revoked_at": grant.revoked_at,
            "version": grant.version,
        }

    def create_consent(self, *, actor_id: str, req: ConsentCreateRequest) -> dict[str, Any]:
        existing = self.store.consents.get(req.consent_id)
        if existing is not None:
            if existing.learner_id != actor_id:
                raise ApiProblemError(
                    status=403,
                    code="access_denied",
                    title="Access denied",
                    detail="Cannot access another learner's consent record.",
                )
            return self._serialize_consent(existing)
        try:
            grant = ConsentGrant.create(
                consent_id=req.consent_id,
                learner_id=actor_id,
                capture_mode=CaptureMode(req.capture_mode),
                policy_version=req.policy_version,
                all_participants_consented=req.all_participants_consented,
                attested_at=req.attested_at,
            )
        except ConsentError as error:
            raise ApiProblemError(
                status=422,
                code="consent_invalid",
                title="Invalid consent configuration",
                detail=str(error),
            ) from error
        except ValueError as error:
            raise ApiProblemError(
                status=422,
                code="validation_failed",
                title="Invalid consent fields",
                detail=str(error),
            ) from error
        self.store.save_consent(grant)
        return self._serialize_consent(grant)

    def revoke_consent(self, *, actor_id: str, consent_id: str) -> dict[str, Any]:
        try:
            grant, job = self.deletion_worker.revoke_consent_and_delete(
                actor_learner_id=actor_id,
                consent_id=consent_id,
                revoked_at=self.now(),
            )
        except KeyError as error:
            raise ApiProblemError(
                status=404,
                code="consent_not_found",
                title="Consent not found",
                detail="The requested consent record does not exist.",
            ) from error
        except PermissionError as error:
            raise ApiProblemError(
                status=403,
                code="access_denied",
                title="Access denied",
                detail="Cannot revoke another learner's consent.",
            ) from error
        for rid in [
            rid
            for rid, entry in self._rehearsals.items()
            if entry.attempt_id in job.target_attempt_ids
        ]:
            del self._rehearsals[rid]
        return {
            "consent": self._serialize_consent(grant),
            "deletion_job": self._serialize_job(job),
        }

    async def ingest_attempt(self, req: AttemptIngestRequest) -> dict[str, Any]:
        _iso_utc(req.recorded_at)
        transcript = BeeTranscript(
            reference=req.bee_recording_reference,
            recorded_at=req.recorded_at.astimezone(UTC),
            device_type=req.device_type,
            provenance=req.provenance,
            segments=tuple(
                RawSpeakerSegment(
                    segment_id=seg.segment_id,
                    raw_speaker_label=seg.speaker_label,
                    text=seg.text,
                    start_ms=seg.start_ms,
                    end_ms=seg.end_ms,
                    asr_confidence=seg.asr_confidence,
                    speaker_confidence=seg.speaker_confidence,
                    overlapping_speech=seg.overlapping_speech,
                )
                for seg in req.segments
            ),
        )
        metadata = CaptureMetadata(
            learner_id=req.learner_id,
            consent_id=req.consent_id,
            capture_mode=CaptureMode(req.capture_mode),
            situation=Situation(req.situation),
            learner_goal=req.learner_goal,
            separation_method=(
                SeparationMethod(req.separation_method)
                if req.separation_method is not None
                else None
            ),
            recurrence_id=req.recurrence_id,
            separation_policy=SpeakerSeparationPolicy(
                enrolled_diarization_speaker_id=req.enrolled_diarization_speaker_id
            ),
        )
        outcome, proposal = await self.bridge.ingest_transcript(
            transcript,
            metadata,
            ingested_at=self.now(),
        )
        if outcome.status in {"ingested", "duplicate"} and proposal is not None:
            stored_doc = deepcopy(
                self.store.attempts.get(proposal.attempt_id, proposal.attempt_document)
            )
            return {
                "status": outcome.status,
                "attempt_id": proposal.attempt_id,
                "learner_review_required": proposal.learner_review_required,
                "attempt": stored_doc,
            }
        if outcome.status == "stopped_consent_revoked":
            raise ApiProblemError(
                status=403,
                code="consent_revoked",
                title="Consent revoked",
                detail="Consent has been revoked; ingestion is blocked and deletion started.",
            )
        if outcome.status == "rejected_missing_consent":
            raise ApiProblemError(
                status=403,
                code=outcome.error_code or "consent_required",
                title="Consent attestation required",
                detail="Processing requires an active learner consent grant matching capture mode.",
            )
        raise ApiProblemError(
            status=422,
            code=outcome.error_code or "minimization_failed",
            title="Transcript ingestion rejected",
            detail=(
                "Transcript failed minimization or safety validation and transient data was purged."
            ),
        )

    def _require_owned_attempt(self, *, actor_id: str, attempt_id: str) -> dict[str, Any]:
        doc = self.store.attempts.get(attempt_id)
        if doc is None:
            tomb = self.store.tombstones.get(attempt_id)
            if tomb is not None and tomb.learner_id != actor_id:
                raise ApiProblemError(
                    status=403,
                    code="access_denied",
                    title="Access denied",
                    detail="Cannot access another learner's attempt.",
                )
            raise ApiProblemError(
                status=404,
                code="attempt_not_found",
                title="Attempt not found",
                detail="The requested attempt does not exist.",
            )
        if doc.get("learner_id") != actor_id:
            raise ApiProblemError(
                status=403,
                code="access_denied",
                title="Access denied",
                detail="Cannot access another learner's attempt.",
            )
        return doc

    def list_attempts(
        self,
        *,
        actor_id: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        owned = sorted(
            (
                deepcopy(doc)
                for doc in self.store.attempts.values()
                if doc.get("learner_id") == actor_id
                and doc.get("status") not in {"excluded", "deletion_pending", "deleted"}
            ),
            key=lambda item: (item["source"]["recorded_at"], item["attempt_id"]),
        )
        start = 0
        if cursor is not None:
            matched = next(
                (i + 1 for i, item in enumerate(owned) if item["attempt_id"] == cursor),
                None,
            )
            if matched is None:
                raise ApiProblemError(
                    status=400,
                    code="invalid_cursor",
                    title="Invalid pagination cursor",
                    detail="The supplied pagination cursor is unknown.",
                )
            start = matched
        window = owned[start : start + limit]
        next_cursor = window[-1]["attempt_id"] if start + limit < len(owned) else None
        return {"items": window, "next_cursor": next_cursor}

    def get_attempt(self, *, actor_id: str, attempt_id: str) -> dict[str, Any]:
        return deepcopy(self._require_owned_attempt(actor_id=actor_id, attempt_id=attempt_id))

    def review_attempt(
        self,
        *,
        actor_id: str,
        attempt_id: str,
        req: AttemptReviewRequest,
        if_match: str | None = None,
    ) -> dict[str, Any]:
        doc = self._require_owned_attempt(actor_id=actor_id, attempt_id=attempt_id)
        if if_match is not None:
            cleaned = if_match.strip().strip('"')
            if not cleaned.isdigit() or int(cleaned) != req.expected_version:
                raise ApiProblemError(
                    status=409,
                    code="stale_version",
                    title="Version conflict",
                    detail="If-Match header conflicts with expected_version.",
                )

        current_skills = self.store.skill_states.get(actor_id, {})
        review_service = AttemptReviewService(
            doc,
            self.rubric,
            initial_skill_states=current_skills,
        )
        now = self.now()
        running_version = req.expected_version

        try:
            for item in req.breakdown_decisions:
                if item.redact_excerpt:
                    res = review_service.redact_breakdown_excerpt(
                        actor_learner_id=actor_id,
                        expected_version=running_version,
                        breakdown_id=item.breakdown_id,
                        reviewed_at=now,
                    )
                    running_version = res.document["version"]
                res = review_service.review_breakdown(
                    actor_learner_id=actor_id,
                    expected_version=running_version,
                    breakdown_id=item.breakdown_id,
                    decision=BreakdownReviewDecision(item.decision),
                    reviewed_at=now,
                )
                running_version = res.document["version"]

            if req.confirm_attempt:
                corrections = tuple(
                    CriterionCorrection(
                        criterion_id=c.criterion_id,
                        result=CriterionResult(c.result),
                        self_evidence_excerpt=c.self_evidence_excerpt,
                        self_evidence_start_ms=c.self_evidence_start_ms,
                        self_evidence_end_ms=c.self_evidence_end_ms,
                    )
                    for c in req.criterion_corrections
                )
                final_result = review_service.confirm_attempt(
                    actor_learner_id=actor_id,
                    expected_version=running_version,
                    reviewed_at=now,
                    corrections=corrections,
                    interaction_ended=req.interaction_ended,
                    assistance_used=req.assistance_used,
                )
            else:
                final_result = review_service._result()  # noqa: SLF001
        except ValueError as error:
            msg = str(error)
            if "stale_attempt_version" in msg:
                raise ApiProblemError(
                    status=409,
                    code="stale_version",
                    title="Attempt version conflict",
                    detail="Attempt was modified concurrently; reload and retry.",
                ) from error
            raise ApiProblemError(
                status=422,
                code="invalid_review_transition",
                title="Invalid review transition",
                detail=msg,
            ) from error

        self.store.attempts[attempt_id] = deepcopy(final_result.document)
        if final_result.document["status"] == "confirmed":
            self.store.skill_states[actor_id] = dict(final_result.skill_states)
            progress_record = self._progress_for(actor_id).record_review(final_result)
            if progress_record.comparison is not None:
                comp = progress_record.comparison
                comp_key = f"{comp.prior_attempt_id}:{comp.next_attempt_id}"
                self.store.comparisons[comp_key] = {
                    "prior_attempt_id": comp.prior_attempt_id,
                    "next_attempt_id": comp.next_attempt_id,
                    "learner_id": comp.learner_id,
                    "recurrence_id": comp.recurrence_id,
                    "situation": comp.situation,
                    "rubric_version": comp.rubric_version,
                    "functional_progress": comp.functional_progress.value,
                    "comparison_confidence": comp.comparison_confidence,
                    "uncertainty_codes": list(comp.uncertainty_codes),
                    "causal_claim": False,
                }

        return {
            "attempt": deepcopy(final_result.document),
            "skill_updates": list(final_result.skill_updates),
        }

    def exclude_attempt(
        self,
        *,
        actor_id: str,
        attempt_id: str,
        req: AttemptExcludeRequest,
    ) -> dict[str, Any]:
        doc = self._require_owned_attempt(actor_id=actor_id, attempt_id=attempt_id)
        review_service = AttemptReviewService(doc, self.rubric)
        now = self.now()
        try:
            excluded = review_service.exclude_attempt(
                actor_learner_id=actor_id,
                expected_version=req.expected_version,
                reviewed_at=now,
                reason=req.reason,
            )
        except ValueError as error:
            if "stale_attempt_version" in str(error):
                raise ApiProblemError(
                    status=409,
                    code="stale_version",
                    title="Attempt version conflict",
                    detail="Attempt version does not match expected_version.",
                ) from error
            raise ApiProblemError(
                status=422,
                code="invalid_exclusion",
                title="Cannot exclude attempt",
                detail=str(error),
            ) from error

        self.store.attempts[attempt_id] = deepcopy(excluded.document)
        if req.delete_now or req.reason == "consent_revoked":
            self.deletion_worker.delete_attempt(
                actor_learner_id=actor_id,
                attempt_id=attempt_id,
                requested_at=now,
            )
        return {
            "attempt": deepcopy(excluded.document),
            "skill_updates": [],
        }

    def create_recurrence(self, *, actor_id: str, req: RecurrenceCreateRequest) -> dict[str, Any]:
        doc = self._require_owned_attempt(actor_id=actor_id, attempt_id=req.attempt_id)
        if doc["status"] != "confirmed":
            raise ApiProblemError(
                status=422,
                code="attempt_not_confirmed",
                title="Confirmed attempt required",
                detail="A recurrence can only be created from a confirmed attempt.",
            )
        try:
            tz = ZoneInfo(req.time_zone)
        except ZoneInfoNotFoundError as error:
            raise ApiProblemError(
                status=422,
                code="invalid_time_zone",
                title="Invalid IANA time zone",
                detail=f"Unknown time zone: {req.time_zone}",
            ) from error

        now = self.now()
        scheduled_utc = _parse_iso(_iso_utc(req.scheduled_for))
        if scheduled_utc <= now:
            raise ApiProblemError(
                status=422,
                code="occurrence_in_past",
                title="Invalid occurrence timestamp",
                detail="Scheduled occurrence must be in the future.",
            )
        chosen_rehearsal_utc = (
            _parse_iso(_iso_utc(req.rehearsal_at))
            if req.rehearsal_at is not None
            else scheduled_utc - timedelta(minutes=60)
        )
        if chosen_rehearsal_utc < now or chosen_rehearsal_utc > scheduled_utc:
            raise ApiProblemError(
                status=422,
                code="invalid_rehearsal_window",
                title="Invalid rehearsal timestamp",
                detail="Rehearsal time must fall between now and the scheduled occurrence.",
            )

        local_rehearsal = chosen_rehearsal_utc.astimezone(tz)
        quiet = local_rehearsal.hour >= 21 or local_rehearsal.hour < 8
        selection_required = not req.learner_confirmed or (
            quiet and req.notification_behavior == "local_notification"
        )

        recurrence_id = doc["context"].get("recurrence_id") or deterministic_ulid(
            namespace="recurrence",
            seed=f"{actor_id}:{req.attempt_id}:{_iso_utc(scheduled_utc)}",
            timestamp=now,
        )
        if not doc["context"].get("recurrence_id"):
            doc["context"]["recurrence_id"] = recurrence_id
            prog_attempts = self._progress_for(actor_id)._attempts  # noqa: SLF001
            if req.attempt_id in prog_attempts:
                prog_attempts[req.attempt_id]["context"]["recurrence_id"] = recurrence_id
        record: dict[str, Any] = {
            "recurrence_id": recurrence_id,
            "learner_id": actor_id,
            "attempt_id": req.attempt_id,
            "situation": doc["context"]["situation"],
            "scheduled_for": _iso_utc(scheduled_utc),
            "rehearsal_at": None if selection_required else _iso_utc(chosen_rehearsal_utc),
            "candidate_rehearsal_at": _iso_utc(chosen_rehearsal_utc),
            "time_zone": req.time_zone,
            "source": "learner_marked",
            "calendar_event_reference": None,
            "quiet_hours": quiet,
            "selection_required": selection_required,
            "notification_behavior": (None if selection_required else req.notification_behavior),
            "created_at": _iso_utc(now),
        }
        self.store.recurrences[recurrence_id] = record
        return self._public_recurrence(record)

    @staticmethod
    def _public_recurrence(record: dict[str, Any]) -> dict[str, Any]:
        return {
            k: v for k, v in record.items() if k not in {"candidate_rehearsal_at", "created_at"}
        }

    def _require_owned_recurrence(self, *, actor_id: str, recurrence_id: str) -> dict[str, Any]:
        rec = self.store.recurrences.get(recurrence_id)
        if rec is None:
            raise ApiProblemError(
                status=404,
                code="recurrence_not_found",
                title="Recurrence not found",
                detail="The requested recurrence does not exist.",
            )
        if rec.get("learner_id") != actor_id:
            raise ApiProblemError(
                status=403,
                code="access_denied",
                title="Access denied",
                detail="Cannot access another learner's recurrence.",
            )
        return rec

    def link_calendar_event(
        self,
        *,
        actor_id: str,
        recurrence_id: str,
        req: CalendarLinkRequest,
    ) -> dict[str, Any]:
        rec = self._require_owned_recurrence(actor_id=actor_id, recurrence_id=recurrence_id)
        rec["source"] = "explicit_calendar_link"
        rec["calendar_event_reference"] = req.calendar_event_reference.strip()
        if req.scheduled_for is not None:
            scheduled_utc = _parse_iso(_iso_utc(req.scheduled_for))
            if scheduled_utc <= self.now():
                raise ApiProblemError(
                    status=422,
                    code="occurrence_in_past",
                    title="Invalid occurrence timestamp",
                    detail="Linked calendar event must be in the future.",
                )
            rec["scheduled_for"] = _iso_utc(scheduled_utc)
        return self._public_recurrence(rec)

    def _signed_counterpart_audio(
        self,
        *,
        turn_id: str,
        text_nl: str,
        voice_id: str,
        speaking_rate: str,
    ) -> dict[str, str]:
        expires = self.now() + timedelta(minutes=15)
        expires_epoch = int(expires.timestamp())
        clip_hash = hashlib.sha256(f"{voice_id}:{speaking_rate}:{text_nl}".encode()).hexdigest()[
            :24
        ]
        sig = hmac.new(
            self._signing_secret,
            f"{clip_hash}:{expires_epoch}".encode(),
            hashlib.sha256,
        ).hexdigest()[:32]
        return {
            "turn_id": turn_id,
            "signed_url": (
                f"https://audio.nexttime.local/v1/counterpart-clips/{clip_hash}"
                f"?expires={expires_epoch}&sig={sig}"
            ),
            "expires_at": _iso_utc(expires),
        }

    def _serialize_rehearsal(self, entry: RehearsalSessionEntry) -> dict[str, Any]:
        scenario = entry.service.scenario
        state = entry.service.state
        committed = len(entry.committed_sequences)
        current_turn_id = (
            scenario.turns[committed].turn_id
            if state.value in {"scheduled", "in_progress"} and committed < len(scenario.turns)
            else None
        )
        return {
            "rehearsal_id": entry.rehearsal_id,
            "recurrence_id": entry.recurrence_id,
            "attempt_id": entry.attempt_id,
            "learner_id": entry.learner_id,
            "situation": scenario.situation.value,
            "target_function": scenario.target_function,
            "estimated_seconds": scenario.estimated_seconds,
            "state": state.value,
            "current_turn_id": current_turn_id,
            "committed_turn_count": committed,
            "non_speaking_practice": bool(
                self.store.rehearsal_sessions.get(entry.rehearsal_id, {}).get(
                    "non_speaking_practice", False
                )
            ),
            "template_version": scenario.template_version,
            "model_generated": scenario.model_generated,
            "counterpart_persona": scenario.counterpart_persona,
            "voice_id": scenario.voice_id,
            "speaking_rate": scenario.speaking_rate,
            "turns": [
                {
                    "turn_id": t.turn_id,
                    "counterpart_text_nl": t.counterpart_text_nl,
                    "counterpart_text_en": t.counterpart_text_en,
                    "expected_functions": list(t.expected_functions),
                    "accepted_phrase_ids": list(t.accepted_phrase_ids),
                    "repair_if_missing": t.repair_if_missing,
                    "counterpart_audio": self._signed_counterpart_audio(
                        turn_id=t.turn_id,
                        text_nl=t.counterpart_text_nl,
                        voice_id=scenario.voice_id,
                        speaking_rate=scenario.speaking_rate,
                    ),
                }
                for t in scenario.turns
            ],
            "support_phrases": [
                {
                    "phrase_id": sp.phrase_id,
                    "text_nl": sp.text_nl,
                    "text_en": sp.text_en,
                }
                for sp in scenario.support_phrases
            ],
            "safety_exclusions": list(scenario.safety_exclusions),
        }

    def create_rehearsal(
        self,
        *,
        actor_id: str,
        recurrence_id: str,
        req: RehearsalCreateRequest,
    ) -> dict[str, Any]:
        rec = self._require_owned_recurrence(actor_id=actor_id, recurrence_id=recurrence_id)
        if rec["selection_required"] or rec["rehearsal_at"] is None:
            raise ApiProblemError(
                status=422,
                code="rehearsal_schedule_unconfirmed",
                title="Rehearsal schedule selection required",
                detail=(
                    "Confirm rehearsal time or notification behavior before generating rehearsal."
                ),
            )
        pair_key = (recurrence_id, req.breakdown_id)
        existing_id = self._rehearsal_by_recurrence.get(pair_key)
        if existing_id is not None and existing_id in self._rehearsals:
            return self._serialize_rehearsal(self._rehearsals[existing_id])

        doc = self._require_owned_attempt(actor_id=actor_id, attempt_id=rec["attempt_id"])
        try:
            rehearsal_svc = FixtureRehearsalService(
                doc,
                actor_learner_id=actor_id,
                breakdown_id=req.breakdown_id,
                initial_skill_states=self.store.skill_states.get(actor_id, {}),
            )
        except ValueError as error:
            raise ApiProblemError(
                status=422,
                code="invalid_rehearsal_target",
                title="Cannot generate rehearsal for breakdown",
                detail=str(error),
            ) from error

        tz = ZoneInfo(rec["time_zone"])
        marked_at = _parse_iso(rec["created_at"]).astimezone(tz)
        occurrence_at = _parse_iso(rec["scheduled_for"]).astimezone(tz)
        selected_at = _parse_iso(rec["rehearsal_at"]).astimezone(tz)
        notif_behavior = NotificationBehavior(rec["notification_behavior"] or "open_template_only")
        rehearsal_svc.mark_recurrence(
            actor_learner_id=actor_id,
            occurrence_at=occurrence_at,
            marked_at=marked_at,
        )
        rehearsal_svc.schedule(
            actor_learner_id=actor_id,
            learner_confirmed=True,
            selected_at=selected_at,
            notification_behavior=notif_behavior,
        )

        rehearsal_id = deterministic_ulid(
            namespace="rehearsal",
            seed=f"{actor_id}:{recurrence_id}:{req.breakdown_id}",
            timestamp=self.now(),
        )
        entry = RehearsalSessionEntry(
            rehearsal_id=rehearsal_id,
            recurrence_id=recurrence_id,
            attempt_id=rec["attempt_id"],
            learner_id=actor_id,
            breakdown_id=req.breakdown_id,
            service=rehearsal_svc,
            haptics=PacingHapticsEngine(rehearsal_id=rehearsal_id),
        )
        self._rehearsals[rehearsal_id] = entry
        self._rehearsal_by_recurrence[pair_key] = rehearsal_id
        self.store.rehearsal_sessions[rehearsal_id] = {
            "rehearsal_id": rehearsal_id,
            "recurrence_id": recurrence_id,
            "attempt_id": rec["attempt_id"],
            "learner_id": actor_id,
            "target_function": rehearsal_svc.scenario.target_function,
            "state": rehearsal_svc.state.value,
            "non_speaking_practice": False,
        }
        self.store.put_cache_entry(
            cache_key=f"scenario:{rehearsal_id}",
            learner_id=actor_id,
            attempt_id=rec["attempt_id"],
            payload={"template_version": rehearsal_svc.scenario.template_version},
        )
        return self._serialize_rehearsal(entry)

    def _require_owned_rehearsal(
        self, *, actor_id: str, rehearsal_id: str
    ) -> RehearsalSessionEntry:
        entry = self._rehearsals.get(rehearsal_id)
        if entry is None:
            raise ApiProblemError(
                status=404,
                code="rehearsal_not_found",
                title="Rehearsal not found",
                detail="The requested rehearsal session does not exist.",
            )
        if entry.learner_id != actor_id:
            raise ApiProblemError(
                status=403,
                code="access_denied",
                title="Access denied",
                detail="Cannot access another learner's rehearsal session.",
            )
        return entry

    def get_rehearsal(self, *, actor_id: str, rehearsal_id: str) -> dict[str, Any]:
        entry = self._require_owned_rehearsal(actor_id=actor_id, rehearsal_id=rehearsal_id)
        return self._serialize_rehearsal(entry)

    def _record_rehearsal_in_progress_once(
        self, entry: RehearsalSessionEntry, *, state_override: str | None = None
    ) -> None:
        if entry.recorded_in_progress:
            return
        entry.recorded_in_progress = True
        final_state = state_override or entry.service.state.value
        session_meta = self.store.rehearsal_sessions.get(entry.rehearsal_id, {})
        session_meta["state"] = final_state
        self._progress_for(entry.learner_id).record_rehearsal(
            {
                "rehearsal_id": entry.rehearsal_id,
                "recurrence_id": entry.recurrence_id,
                "target_function": entry.service.scenario.target_function,
                "state": final_state,
                "completed_at": _iso_utc(self.now()),
            }
        )

    def submit_rehearsal_turn(
        self,
        *,
        actor_id: str,
        rehearsal_id: str,
        req: RehearsalTurnSubmitRequest,
    ) -> dict[str, Any]:
        entry = self._require_owned_rehearsal(actor_id=actor_id, rehearsal_id=rehearsal_id)
        if entry.service.state.value == "scheduled":
            entry.service.begin(actor_learner_id=actor_id)

        expected_seq = len(entry.committed_sequences) + 1
        scenario_turns = entry.service.scenario.turns
        matching_turn = next((t for t in scenario_turns if t.turn_id == req.turn_id), None)
        accepted_ids = matching_turn.accepted_phrase_ids if matching_turn is not None else ()
        resolved_phrase_id = req.phrase_id or _map_dutch_transcript_to_phrase_id(
            req.transcript, accepted_ids
        )

        if req.sequence != expected_seq and req.turn_event_id not in entry.service._events:  # noqa: SLF001
            raise ApiProblemError(
                status=409,
                code="invalid_turn_sequence",
                title="Out-of-order rehearsal turn",
                detail=f"Expected turn sequence {expected_seq}, got {req.sequence}.",
            )

        try:
            turn_result = entry.service.submit_turn(
                actor_learner_id=actor_id,
                event_id=req.turn_event_id,
                turn_id=req.turn_id,
                phrase_id=resolved_phrase_id,
                confidence=req.confidence,
                mode=PracticeMode(req.mode),
            )
        except ValueError as error:
            msg = str(error)
            if "turn_event_replay_conflict" in msg or "stale_rehearsal_turn" in msg:
                raise ApiProblemError(
                    status=409,
                    code=msg,
                    title="Rehearsal turn conflict",
                    detail="Turn event conflicts with current rehearsal turn state.",
                ) from error
            raise ApiProblemError(
                status=422,
                code="invalid_rehearsal_turn",
                title="Cannot process rehearsal turn",
                detail=msg,
            ) from error

        if not turn_result.duplicate and turn_result.mapped_function is not None:
            entry.committed_sequences.append(req.sequence)

        artifact = self.store.record_rehearsal_turn_artifacts(
            turn_record_id=f"{rehearsal_id}:{req.turn_event_id}",
            learner_id=actor_id,
            rehearsal_id=rehearsal_id,
            attempt_id=entry.attempt_id,
            learner_transcript=req.transcript or (resolved_phrase_id or ""),
            recorded_at=self.now(),
            raw_audio_bytes=(b"transient-ptt-pcm" if req.mode == "speech" else None),
            save_self_excerpt=req.save_self_excerpt,
        )
        session_meta = self.store.rehearsal_sessions[rehearsal_id]
        session_meta["state"] = turn_result.state.value
        session_meta["non_speaking_practice"] = turn_result.non_speaking_practice

        if turn_result.state.value in {"completed", "abandoned"}:
            self._record_rehearsal_in_progress_once(entry)

        return {
            "rehearsal_id": rehearsal_id,
            "state": turn_result.state.value,
            "current_turn_id": turn_result.current_turn_id,
            "mapped_function": turn_result.mapped_function,
            "support": turn_result.support,
            "committed_turn_count": turn_result.committed_turn_count,
            "non_speaking_practice": turn_result.non_speaking_practice,
            "duplicate": turn_result.duplicate,
            "raw_audio_deleted": bool(artifact["raw_audio_deleted"]),
            "transcript_delete_after": artifact["delete_after"],
            "authentic_mastery_updated": False,
        }

    def complete_rehearsal(
        self,
        *,
        actor_id: str,
        rehearsal_id: str,
        req: RehearsalCompleteRequest,
    ) -> dict[str, Any]:
        entry = self._require_owned_rehearsal(actor_id=actor_id, rehearsal_id=rehearsal_id)
        committed = len(entry.committed_sequences)
        if req.final_turn_sequence != committed:
            raise ApiProblemError(
                status=409,
                code="final_sequence_mismatch",
                title="Turn sequence mismatch",
                detail=f"Expected final_turn_sequence={committed}.",
            )
        if req.exit_early:
            self._record_rehearsal_in_progress_once(entry, state_override="abandoned")
            self.store.rehearsal_sessions[rehearsal_id]["state"] = "abandoned"
            serialized = self._serialize_rehearsal(entry)
            serialized["state"] = "abandoned"
            serialized["current_turn_id"] = None
            return serialized

        if entry.service.state.value != "completed":
            raise ApiProblemError(
                status=422,
                code="rehearsal_turns_incomplete",
                title="Rehearsal turns incomplete",
                detail=(
                    "All scenario turns must complete before finalizing, or set exit_early=true."
                ),
            )
        self._record_rehearsal_in_progress_once(entry)
        return self._serialize_rehearsal(entry)

    def get_progress(self, *, actor_id: str) -> dict[str, Any]:
        snapshot = self._progress_for(actor_id).snapshot()
        skills_map: dict[str, SkillState] = dict(
            self.store.skill_states.get(actor_id, snapshot.skill_states)
        )
        skill_items = [
            {
                "skill_code": code,
                "alpha": state.alpha,
                "beta": state.beta,
                "mastery": round(state.mastery, 4),
                "uncertainty": round(1.0 / (state.alpha + state.beta), 4),
                "authentic_attempt_count": state.authentic_attempt_count,
            }
            for code, state in sorted(skills_map.items())
        ]
        comparisons = [
            {
                "prior_attempt_id": comp.prior_attempt_id,
                "next_attempt_id": comp.next_attempt_id,
                "learner_id": comp.learner_id,
                "recurrence_id": comp.recurrence_id,
                "situation": comp.situation,
                "rubric_version": comp.rubric_version,
                "criterion_deltas": [
                    {
                        "criterion_id": d.criterion_id,
                        "prior_result": d.prior_result.value,
                        "next_result": d.next_result.value,
                        "delta": d.delta,
                        "prior_self_evidence": d.prior_self_evidence,
                        "next_self_evidence": d.next_self_evidence,
                    }
                    for d in comp.criterion_deltas
                ],
                "functional_progress": comp.functional_progress.value,
                "comparison_confidence": comp.comparison_confidence,
                "uncertainty_codes": list(comp.uncertainty_codes),
                "causal_claim": False,
            }
            for comp in snapshot.comparisons
        ]
        wins = [
            {
                "prior_attempt_id": w.prior_attempt_id,
                "next_attempt_id": w.next_attempt_id,
                "criterion_id": w.criterion_id,
                "kind": w.kind,
                "self_evidence_excerpt": w.self_evidence_excerpt,
            }
            for w in snapshot.functional_wins
        ]
        active_Ids = set(self.store.attempts.keys())
        visible_attempts = [
            a for a in snapshot.authentic_attempts if a.get("attempt_id") in active_Ids
        ]
        return {
            "learner_id": actor_id,
            "authentic_attempts": visible_attempts,
            "comparisons": comparisons,
            "functional_wins": wins,
            "skill_states": skill_items,
            "rehearsal_history": list(snapshot.rehearsal_history),
            "causal_claim": False,
            "gamified_streaks": False,
        }

    def export_learner_data(self, *, actor_id: str, recent_auth: bool) -> dict[str, Any]:
        try:
            pkg = build_learner_export(
                actor_learner_id=actor_id,
                learner_id=actor_id,
                recent_auth_verified=recent_auth,
                exported_at=self.now(),
                consents=list(self.store.consents.values()),
                attempts=list(self.store.attempts.values()),
                recurrences=[self._public_recurrence(r) for r in self.store.recurrences.values()],
                rehearsal_history=list(self._progress_for(actor_id).snapshot().rehearsal_history),
                comparisons=list(self.store.comparisons.values()),
                skill_states=self.store.skill_states.get(actor_id, {}),
            )
        except PermissionError as error:
            raise ApiProblemError(
                status=403,
                code=str(error),
                title="Recent authentication required",
                detail="Exporting learner data requires a recently authenticated session.",
            ) from error
        return {
            "schema_version": pkg.schema_version,
            "learner_id": pkg.learner_id,
            "exported_at": pkg.exported_at,
            "consents": list(pkg.consents),
            "attempts": list(pkg.attempts),
            "recurrences": list(pkg.recurrences),
            "rehearsal_history": list(pkg.rehearsal_history),
            "comparisons": list(pkg.comparisons),
            "skill_states": dict(pkg.skill_states),
            "other_speaker_transcript_included": False,
        }

    def delete_learner_account(self, *, actor_id: str, recent_auth: bool) -> dict[str, Any]:
        try:
            job = self.deletion_worker.delete_account(
                actor_learner_id=actor_id,
                learner_id=actor_id,
                recent_auth_verified=recent_auth,
                requested_at=self.now(),
            )
        except PermissionError as error:
            raise ApiProblemError(
                status=403,
                code=str(error),
                title="Recent authentication required",
                detail="Account deletion requires a recently authenticated learner session.",
            ) from error

        self._progress_by_learner.pop(actor_id, None)
        self.calendar_sync.purge_learner(actor_id)
        for rid in [rid for rid, entry in self._rehearsals.items() if entry.learner_id == actor_id]:
            del self._rehearsals[rid]
        return self._serialize_job(job)

    @staticmethod
    def _serialize_calendar_candidate(candidate: CalendarRecurrenceCandidate) -> dict[str, Any]:
        return {
            "candidate_id": candidate.candidate_id,
            "learner_id": candidate.learner_id,
            "provider": candidate.provider,
            "calendar_event_reference": candidate.calendar_event_reference,
            "situation": candidate.situation.value,
            "matched_attempt_id": candidate.matched_attempt_id,
            "matched_breakdown_id": candidate.matched_breakdown_id,
            "scheduled_for": candidate.scheduled_for,
            "proposed_rehearsal_at": candidate.proposed_rehearsal_at,
            "time_zone": candidate.time_zone,
            "quiet_hours": candidate.quiet_hours,
            "confirmation_state": candidate.confirmation_state,
            "provenance": candidate.provenance,
            "created_at": candidate.created_at,
        }

    @staticmethod
    def _serialize_rehearsal_card(card: PreMeetingRehearsalCard) -> dict[str, Any]:
        return {
            "card_id": card.card_id,
            "learner_id": card.learner_id,
            "candidate_id": card.candidate_id,
            "recurrence_id": card.recurrence_id,
            "attempt_id": card.attempt_id,
            "breakdown_id": card.breakdown_id,
            "situation": card.situation.value,
            "target_function": card.target_function,
            "scheduled_for": card.scheduled_for,
            "rehearsal_at": card.rehearsal_at,
            "time_zone": card.time_zone,
            "estimated_seconds": card.estimated_seconds,
            "quiet_hours": card.quiet_hours,
            "notification_behavior": card.notification_behavior.value,
            "event_source": card.event_source,
            "calendar_event_reference": card.calendar_event_reference,
            "status": card.status,
            "provenance": card.provenance,
        }

    async def sync_ics_calendar(
        self,
        *,
        actor_id: str,
        req: CalendarIcsSyncRequest,
    ) -> dict[str, Any]:
        try:
            ZoneInfo(req.default_time_zone)
        except ZoneInfoNotFoundError as error:
            raise ApiProblemError(
                status=422,
                code="invalid_time_zone",
                title="Invalid IANA time zone",
                detail=f"Unknown time zone '{req.default_time_zone}'.",
            ) from error

        adapter = ICalendarFeedAdapter(
            req.ics_payload,
            default_time_zone=req.default_time_zone,
            provenance=req.provenance,
        )
        now = self.now()
        try:
            summary = await self.calendar_sync.sync_candidates(
                learner_id=actor_id,
                provider="icalendar",
                source=adapter,
                confirmed_attempts=list(self.store.attempts.values()),
                now=now,
                horizon_days=req.horizon_days,
            )
        except CalendarAdapterError as error:
            raise ApiProblemError(
                status=422,
                code=error.code,
                title="Invalid iCalendar payload",
                detail=error.detail,
            ) from error

        return {
            "learner_id": summary.learner_id,
            "provider": summary.provider,
            "candidates": [self._serialize_calendar_candidate(c) for c in summary.candidates],
            "excluded_sensitive_count": summary.excluded_sensitive_count,
            "ignored_unrelated_count": summary.ignored_unrelated_count,
            "cancelled_count": summary.cancelled_count,
            "next_sync_token": summary.next_sync_token,
            "provenance": summary.provenance,
        }

    def list_calendar_candidates(self, *, actor_id: str) -> dict[str, Any]:
        items = [
            self._serialize_calendar_candidate(c)
            for c in self.calendar_sync.list_candidates(actor_learner_id=actor_id)
        ]
        return {"items": items}

    def confirm_calendar_candidate(
        self,
        *,
        actor_id: str,
        candidate_id: str,
        req: CalendarCandidateConfirmRequest,
    ) -> dict[str, Any]:
        behavior = (
            NotificationBehavior(req.notification_behavior)
            if req.notification_behavior is not None
            else None
        )
        try:
            card = self.calendar_sync.confirm_candidate_and_queue_card(
                actor_learner_id=actor_id,
                candidate_id=candidate_id,
                confirmed_at=self.now(),
                rehearsal_at=req.rehearsal_at,
                notification_behavior=behavior,
            )
        except KeyError as error:
            raise ApiProblemError(
                status=404,
                code="calendar_candidate_not_found",
                title="Calendar candidate not found",
                detail="The requested calendar candidate does not exist.",
            ) from error
        except PermissionError as error:
            raise ApiProblemError(
                status=403,
                code="access_denied",
                title="Access denied",
                detail=str(error),
            ) from error
        except ValueError as error:
            code = str(error)
            raise ApiProblemError(
                status=422,
                code=code,
                title="Calendar candidate confirmation failed",
                detail=f"Cannot confirm calendar candidate ({code}).",
            ) from error

        doc = self.store.attempts.get(card.attempt_id)
        if doc is not None and not doc["context"].get("recurrence_id"):
            doc["context"]["recurrence_id"] = card.recurrence_id
            prog_attempts = self._progress_for(actor_id)._attempts  # noqa: SLF001
            if card.attempt_id in prog_attempts:
                prog_attempts[card.attempt_id]["context"]["recurrence_id"] = card.recurrence_id

        self.store.recurrences[card.recurrence_id] = {
            "recurrence_id": card.recurrence_id,
            "learner_id": actor_id,
            "attempt_id": card.attempt_id,
            "situation": card.situation.value,
            "scheduled_for": card.scheduled_for,
            "rehearsal_at": card.rehearsal_at,
            "candidate_rehearsal_at": card.rehearsal_at,
            "time_zone": card.time_zone,
            "source": "explicit_calendar_link",
            "calendar_event_reference": card.calendar_event_reference,
            "quiet_hours": card.quiet_hours,
            "selection_required": False,
            "notification_behavior": card.notification_behavior.value,
            "created_at": _iso_utc(self.now()),
        }
        return self._serialize_rehearsal_card(card)

    def list_rehearsal_cards(self, *, actor_id: str) -> dict[str, Any]:
        return {
            "items": [
                self._serialize_rehearsal_card(card)
                for card in self.calendar_sync.list_queued_cards(actor_learner_id=actor_id)
            ]
        }

    async def transcribe_rehearsal_chunks(
        self,
        *,
        actor_id: str,
        rehearsal_id: str,
        req: TranscribeChunksRequest,
    ) -> dict[str, Any]:
        entry = self._require_owned_rehearsal(actor_id=actor_id, rehearsal_id=rehearsal_id)
        pcm_chunks: list[PcmAudioChunk] = []
        for item in req.chunks:
            try:
                raw_bytes = base64.b64decode(item.pcm_base64, validate=True)
            except (binascii.Error, ValueError) as error:
                raise ApiProblemError(
                    status=422,
                    code="invalid_pcm_base64",
                    title="Invalid base64 PCM chunk",
                    detail="Audio chunk payload is not valid base64.",
                ) from error
            pcm_chunks.append(
                PcmAudioChunk(
                    sequence_id=item.sequence_id,
                    pcm_buffer=bytearray(raw_bytes),
                    sample_rate_hz=item.sample_rate_hz,
                    channels=item.channels,
                    sample_width_bytes=item.sample_width_bytes,
                )
            )

        try:
            result = await self.chunk_transcriber.transcribe_ptt_turn(
                learner_id=actor_id,
                rehearsal_id=entry.rehearsal_id,
                turn_event_id=req.turn_event_id,
                chunks=pcm_chunks,
                streaming_transcription_consented=req.streaming_transcription_consented,
                capture_origin=req.capture_origin,
                continuous_stream=req.continuous_stream,
                save_self_excerpt=req.save_self_excerpt,
                transcribed_at=self.now(),
            )
        except CovertCaptureProhibitedError as error:
            raise ApiProblemError(
                status=403,
                code=str(error),
                title="Prohibited audio capture origin",
                detail="Continuous or non-iPhone push-to-talk audio streaming is prohibited.",
            ) from error
        except ConsentError as error:
            raise ApiProblemError(
                status=403,
                code=str(error),
                title="Streaming transcription consent required",
                detail="Learner must explicitly consent to push-to-talk streaming transcription.",
            ) from error
        except ValueError as error:
            raise ApiProblemError(
                status=422,
                code="invalid_audio_chunk",
                title="Invalid PCM audio chunk",
                detail=str(error),
            ) from error
        except TranscribeTransportError as error:
            raise ApiProblemError(
                status=503,
                code=error.code,
                title="Streaming transcription unavailable",
                detail=error.detail,
            ) from error

        return {
            "turn_event_id": result.turn_event_id,
            "rehearsal_id": result.rehearsal_id,
            "learner_id": result.learner_id,
            "transcript": result.transcript,
            "confidence": result.confidence,
            "below_confidence_threshold": result.below_confidence_threshold,
            "partial_count": result.partial_count,
            "chunk_receipts": [
                {
                    "sequence_id": r.sequence_id,
                    "bytes_zeroed": r.bytes_zeroed,
                    "deleted_immediately": True,
                    "deleted_at": r.deleted_at,
                }
                for r in result.chunk_receipts
            ],
            "retained_pcm_bytes": 0,
            "transcript_delete_after": result.transcript_delete_after,
            "provider": result.provider,
            "provenance": result.provenance,
        }

    @staticmethod
    def _serialize_haptic_event(event: HapticFeedbackEvent) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "rehearsal_id": event.rehearsal_id,
            "turn_id": event.turn_id,
            "trigger": event.trigger.value,
            "pattern": event.pattern.value if event.pattern is not None else None,
            "sequence": (
                {
                    "pattern": event.sequence.pattern.value,
                    "pulse_durations_ms": list(event.sequence.pulse_durations_ms),
                    "gap_durations_ms": list(event.sequence.gap_durations_ms),
                    "total_duration_ms": event.sequence.total_duration_ms,
                }
                if event.sequence is not None
                else None
            ),
            "target_device": event.target_device,
            "emitted": event.emitted,
            "suppression_reason": event.suppression_reason,
            "visual_caption_key": event.visual_caption_key,
            "voiceover_announcement": event.voiceover_announcement,
            "duplicate": event.duplicate,
            "created_at": event.created_at,
        }

    def emit_rehearsal_haptic(
        self,
        *,
        actor_id: str,
        rehearsal_id: str,
        req: HapticEventRequest,
    ) -> dict[str, Any]:
        entry = self._require_owned_rehearsal(actor_id=actor_id, rehearsal_id=rehearsal_id)
        try:
            event = entry.haptics.generate(
                source_event_id=req.source_event_id,
                trigger=RehearsalPacingTrigger(req.trigger),
                occurred_at=self.now(),
                turn_id=req.turn_id,
                target_device=req.target_device,
                preferences=HapticPreferences(
                    haptics_enabled=req.haptics_enabled,
                    reduced_motion=req.reduced_motion,
                ),
            )
        except ValueError as error:
            raise ApiProblemError(
                status=422,
                code=str(error),
                title="Invalid haptic target or trigger",
                detail=(
                    "Haptics are bound to the iPhone rehearsal client; "
                    "Bee wearable haptic commands are prohibited."
                ),
            ) from error
        return self._serialize_haptic_event(event)

    def create_ephemeral_simulator_rehearsal(
        self,
        *,
        actor_id: str,
        situation: Situation,
    ) -> RehearsalSessionEntry:
        """Create an isolated fixture-labeled rehearsal session for WebSocket/CLI simulation."""
        now = self.now()
        token_seed = secrets.token_hex(6)
        attempt_id = deterministic_ulid(
            namespace="sim-attempt",
            seed=f"{actor_id}:{situation.value}:{token_seed}",
            timestamp=now,
        )
        recurrence_id = deterministic_ulid(
            namespace="sim-recurrence",
            seed=f"{actor_id}:{attempt_id}",
            timestamp=now,
        )
        consent_id = deterministic_ulid(
            namespace="sim-consent",
            seed=f"{actor_id}:{attempt_id}",
            timestamp=now,
        )
        breakdown_id = deterministic_ulid(
            namespace="sim-breakdown",
            seed=f"{actor_id}:{attempt_id}",
            timestamp=now,
        )
        rehearsal_id = deterministic_ulid(
            namespace="sim-rehearsal",
            seed=f"{actor_id}:{recurrence_id}",
            timestamp=now,
        )
        confirm_criterion = (
            "next_step_confirmed" if situation is Situation.SHIFT else "appointment_confirmed"
        )
        fixture_doc: dict[str, Any] = {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "learner_id": actor_id,
            "source": {
                "bee_recording_reference": f"fixture-sim-{token_seed}",
                "recorded_at": _iso_utc(now - timedelta(hours=1)),
                "capture_mode": "self_memo",
                "device_type": "bee_device",
                "provenance": "fixture",
            },
            "context": {
                "situation": situation.value,
                "target_language": "nl",
                "support_language": "en",
                "learner_goal": f"Practice {situation.value} confirmation in Dutch.",
                "rubric_version": self.rubric.version,
                "recurrence_id": recurrence_id,
            },
            "consent": {
                "consent_id": consent_id,
                "policy_version": "consent_v1",
                "attested_at": _iso_utc(now - timedelta(hours=2)),
                "all_participants_consented": True,
                "revoked": False,
            },
            "processing": {
                "diarization_method": "self_only",
                "asr_confidence": 0.92,
                "redaction_status": "minimized",
                "other_speaker_transcript_retained": False,
                "model_version": "deterministic-breakdown-v1",
            },
            "outcome": {
                "status": "partial",
                "criteria": [
                    {
                        "criterion_id": "goal_stated",
                        "result": "satisfied",
                        "self_evidence_excerpt": "Ik wil een afspraak maken.",
                        "self_evidence_start_ms": 0,
                        "self_evidence_end_ms": 2500,
                    },
                    {
                        "criterion_id": confirm_criterion,
                        "result": "not_satisfied",
                        "self_evidence_excerpt": "",
                    },
                ],
                "confirmation_required": False,
                "confirmed_by_learner": True,
            },
            "breakdowns": [
                {
                    "breakdown_id": breakdown_id,
                    "code": "confirmation_missing",
                    "description": "Explicit date/time or next-step confirmation was missing.",
                    "self_evidence_excerpt": "Ik wil een afspraak maken.",
                    "confidence": 0.88,
                    "review_status": "confirmed",
                }
            ],
            "next_occurrence": {
                "scheduled_for": _iso_utc(now + timedelta(hours=2)),
                "source": "learner_marked",
                "rehearsal_at": _iso_utc(now + timedelta(minutes=15)),
                "time_zone": "Europe/Amsterdam",
            },
            "status": "confirmed",
            "retention": {
                "transient_source_delete_after": _iso_utc(now + timedelta(hours=23)),
                "derived_delete_after": _iso_utc(now + timedelta(days=30)),
            },
            "version": 1,
            "created_at": _iso_utc(now - timedelta(minutes=50)),
            "updated_at": _iso_utc(now - timedelta(minutes=45)),
        }
        svc = FixtureRehearsalService(
            fixture_doc,
            actor_learner_id=actor_id,
            breakdown_id=breakdown_id,
            initial_skill_states=self.store.skill_states.get(actor_id, {}),
        )
        tz = ZoneInfo("Europe/Amsterdam")
        svc.mark_recurrence(
            actor_learner_id=actor_id,
            occurrence_at=(now + timedelta(hours=2)).astimezone(tz),
            marked_at=now.astimezone(tz),
        )
        svc.schedule(
            actor_learner_id=actor_id,
            learner_confirmed=True,
            selected_at=(now + timedelta(minutes=15)).astimezone(tz),
            notification_behavior=NotificationBehavior.OPEN_TEMPLATE_ONLY,
        )
        entry = RehearsalSessionEntry(
            rehearsal_id=rehearsal_id,
            recurrence_id=recurrence_id,
            attempt_id=attempt_id,
            learner_id=actor_id,
            breakdown_id=breakdown_id,
            service=svc,
            haptics=PacingHapticsEngine(rehearsal_id=rehearsal_id),
            ephemeral_simulator=True,
        )
        self._rehearsals[rehearsal_id] = entry
        self.store.rehearsal_sessions[rehearsal_id] = {
            "rehearsal_id": rehearsal_id,
            "recurrence_id": recurrence_id,
            "attempt_id": attempt_id,
            "learner_id": actor_id,
            "target_function": svc.scenario.target_function,
            "state": svc.state.value,
            "non_speaking_practice": False,
        }
        return entry

    def build_rehearsal_breakdown_summary(
        self,
        *,
        entry: RehearsalSessionEntry,
        dialogue_turns: Sequence[TranscriptTurn],
    ) -> dict[str, Any]:
        """Minimize rehearsal dialogue and compute post-rehearsal breakdown via ModelGateway.

        Drops all Speaker.OTHER counterpart turns, scrubs deterministic PII identifiers,
        validates non-sensitive content, and guarantees authentic_mastery_updated == False.
        """
        now = self.now()
        learner_turns = [t for t in dialogue_turns if t.speaker is Speaker.LEARNER]
        if not learner_turns:
            return {
                "type": "breakdown_summary",
                "rehearsal_id": entry.rehearsal_id,
                "situation": entry.service.scenario.situation.value,
                "target_function": entry.service.scenario.target_function,
                "state": entry.service.state.value,
                "committed_turn_count": len(entry.committed_sequences),
                "minimized_excerpts": [],
                "other_speaker_transcript_retained": False,
                "criteria": [],
                "breakdowns": [],
                "model_version": "none_no_learner_turns",
                "fallback_used": True,
                "raw_pcm_retained_bytes": len(self.store.rehearsal_raw_audio),
                "authentic_mastery_updated": False,
                "provenance": "fixture",
            }

        source = SourceTranscript(
            reference=f"rehearsal-ws:{entry.rehearsal_id}",
            learner_id=entry.learner_id,
            recorded_at=now,
            capture_mode=CaptureMode.CONSENTED_CONVERSATION,
            situation=entry.service.scenario.situation,
            turns=tuple(dialogue_turns),
            consent_attested=True,
            all_participants_consented=True,
            revoked=False,
            retention_policy_present=True,
            provenance="fixture",
        )
        minimized = minimize_transcript(source, minimized_at=now)
        raw_extraction = self.breakdown_extractor.extract(
            minimized,
            self.rubric,
            correlation_id=f"ws-breakdown:{entry.rehearsal_id}",
        )
        return {
            "type": "breakdown_summary",
            "rehearsal_id": entry.rehearsal_id,
            "situation": entry.service.scenario.situation.value,
            "target_function": entry.service.scenario.target_function,
            "state": entry.service.state.value,
            "committed_turn_count": len(entry.committed_sequences),
            "minimized_excerpts": [
                {
                    "text": ex.text,
                    "start_ms": ex.start_ms,
                    "end_ms": ex.end_ms,
                    "confidence": ex.confidence,
                }
                for ex in minimized.self_excerpts
            ],
            "other_speaker_transcript_retained": False,
            "criteria": [
                {
                    "criterion_id": c.criterion_id,
                    "result": c.result.value,
                    "self_evidence_excerpt": c.self_evidence_excerpt,
                    "model_confidence": c.model_confidence,
                }
                for c in raw_extraction.criteria
            ],
            "breakdowns": [
                {
                    "code": b.code.value,
                    "description": b.description,
                    "self_evidence_excerpt": b.self_evidence_excerpt,
                    "model_confidence": b.model_confidence,
                }
                for b in raw_extraction.breakdowns
            ],
            "model_version": raw_extraction.model_version,
            "fallback_used": raw_extraction.fallback_used,
            "raw_pcm_retained_bytes": len(self.store.rehearsal_raw_audio),
            "authentic_mastery_updated": False,
            "provenance": "fixture",
        }
