from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from nexttime_bee_adapter import BeeAdapter, BeeAdapterError, BeeTranscript, redact_bridge_log
from nexttime_domain import (
    BreakdownProposalResult,
    BreakdownProposalService,
    BreakdownValidationError,
    CaptureMode,
    ConsentError,
    LearnerTurnLabel,
    Rubric,
    SensitiveContentError,
    SeparationMethod,
    Situation,
    SourceTranscript,
    SpeakerSeparationPolicy,
    TranscriptMinimizationService,
    TransientTranscriptRecord,
    separate_speaker_turns,
)
from nexttime_minimization import DeletionPipelineWorker, InMemoryArtifactStore


class BeeBridgeLeaseError(RuntimeError):
    """Raised when another worker holds an active BeeSourceCursor lease for the learner."""


@dataclass(frozen=True)
class BeeSourceCursor:
    learner_id: str
    cursor: str | None
    last_sync_at: str | None
    worker_id: str | None
    lease_expires_at: datetime | None


@dataclass(frozen=True)
class CaptureMetadata:
    learner_id: str
    consent_id: str
    capture_mode: CaptureMode
    situation: Situation
    learner_goal: str
    separation_method: SeparationMethod | None = None
    recurrence_id: str | None = None
    separation_policy: SpeakerSeparationPolicy = SpeakerSeparationPolicy()
    learner_turn_labels: tuple[LearnerTurnLabel, ...] = ()


@dataclass(frozen=True)
class IngestedRecordingOutcome:
    reference: str
    status: Literal[
        "ingested",
        "duplicate",
        "rejected_missing_consent",
        "rejected_invalid_transcript",
        "stopped_consent_revoked",
    ]
    attempt_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class BridgeSyncReport:
    learner_id: str
    status: Literal["completed", "stopped_consent_revoked", "adapter_error"]
    outcomes: tuple[IngestedRecordingOutcome, ...]
    cursor: str | None
    ingested_count: int
    duplicate_count: int
    rejected_count: int
    error_detail: str | None = None


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Bridge timestamp must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class BeeIngestionBridge:
    """User-controlled local CLI bridge feeding Bee transcripts into minimization."""

    def __init__(
        self,
        *,
        adapter: BeeAdapter,
        store: InMemoryArtifactStore,
        deletion_worker: DeletionPipelineWorker,
        rubric: Rubric,
        minimization_service: TranscriptMinimizationService | None = None,
        proposal_service: BreakdownProposalService | None = None,
        lease_duration_seconds: int = 60,
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._deletion_worker = deletion_worker
        self._minimization = minimization_service or TranscriptMinimizationService()
        self._proposal = proposal_service or BreakdownProposalService(rubric)
        self._lease_duration = timedelta(seconds=max(5, lease_duration_seconds))
        self._cursors: dict[str, BeeSourceCursor] = {}
        self._pending_metadata: dict[tuple[str, str], CaptureMetadata] = {}
        self._ref_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def register_capture_metadata(self, reference: str, metadata: CaptureMetadata) -> None:
        if not reference.strip():
            raise ValueError("Recording reference is required")
        self._pending_metadata[(metadata.learner_id, reference.strip())] = metadata

    def get_cursor(self, learner_id: str) -> BeeSourceCursor | None:
        return self._cursors.get(learner_id)

    def _acquire_lease(self, *, learner_id: str, worker_id: str, now: datetime) -> BeeSourceCursor:
        now_utc = now.astimezone(UTC)
        existing = self._cursors.get(learner_id)
        if (
            existing is not None
            and existing.worker_id is not None
            and existing.worker_id != worker_id
            and existing.lease_expires_at is not None
            and existing.lease_expires_at > now_utc
        ):
            raise BeeBridgeLeaseError("sync_lease_held_by_another_worker")

        leased = BeeSourceCursor(
            learner_id=learner_id,
            cursor=existing.cursor if existing is not None else None,
            last_sync_at=existing.last_sync_at if existing is not None else None,
            worker_id=worker_id,
            lease_expires_at=now_utc + self._lease_duration,
        )
        self._cursors[learner_id] = leased
        return leased

    def _release_lease(self, *, learner_id: str, cursor: str | None, synced_at: datetime) -> None:
        self._cursors[learner_id] = BeeSourceCursor(
            learner_id=learner_id,
            cursor=cursor,
            last_sync_at=_iso_utc(synced_at),
            worker_id=None,
            lease_expires_at=None,
        )

    def _enforce_revoked_consents(self, *, learner_id: str, now: datetime) -> bool:
        revoked_grants = [
            grant
            for grant in self._store.consents.values()
            if grant.learner_id == learner_id and grant.revoked
        ]
        if not revoked_grants:
            return False
        for grant in revoked_grants:
            self._deletion_worker.revoke_consent_and_delete(
                actor_learner_id=learner_id,
                consent_id=grant.consent_id,
                revoked_at=now,
            )
        return True

    async def ingest_transcript(
        self,
        transcript: BeeTranscript,
        metadata: CaptureMetadata,
        *,
        ingested_at: datetime | None = None,
    ) -> tuple[IngestedRecordingOutcome, BreakdownProposalResult | None]:
        now = (ingested_at or datetime.now(UTC)).astimezone(UTC)
        _iso_utc(ingested_at or now)
        lock_key = (metadata.learner_id, transcript.reference)
        lock = self._ref_locks.setdefault(lock_key, asyncio.Lock())

        async with lock:
            grant = self._store.consents.get(metadata.consent_id)
            if grant is None or grant.learner_id != metadata.learner_id:
                self._store.delete_transient_by_reference(
                    learner_id=metadata.learner_id,
                    source_reference=transcript.reference,
                )
                return (
                    IngestedRecordingOutcome(
                        reference=transcript.reference,
                        status="rejected_missing_consent",
                        error_code="consent_attestation_missing",
                    ),
                    None,
                )
            if grant.revoked:
                self._deletion_worker.revoke_consent_and_delete(
                    actor_learner_id=metadata.learner_id,
                    consent_id=grant.consent_id,
                    revoked_at=now,
                )
                return (
                    IngestedRecordingOutcome(
                        reference=transcript.reference,
                        status="stopped_consent_revoked",
                        error_code="consent_revoked",
                    ),
                    None,
                )
            if grant.capture_mode != metadata.capture_mode:
                self._store.delete_transient_by_reference(
                    learner_id=metadata.learner_id,
                    source_reference=transcript.reference,
                )
                return (
                    IngestedRecordingOutcome(
                        reference=transcript.reference,
                        status="rejected_missing_consent",
                        error_code="capture_mode_mismatch_with_consent",
                    ),
                    None,
                )

            transient_record = TransientTranscriptRecord.create(
                learner_id=metadata.learner_id,
                source_reference=transcript.reference,
                consent_id=grant.consent_id,
                encrypted_uri=f"s3://nexttime-transient-encrypted/{metadata.learner_id}/{transcript.reference}",
                created_at=now,
            )
            serialized_transient = json.dumps(
                {
                    "reference": transcript.reference,
                    "segments": len(transcript.segments),
                },
                separators=(",", ":"),
            )
            self._store.put_transient_source(transient_record, serialized_transient)

            resolved_method = metadata.separation_method or (
                SeparationMethod.SELF_ONLY
                if metadata.capture_mode is CaptureMode.SELF_MEMO
                else SeparationMethod.BEE_SPEAKER_LABELS
            )
            try:
                separation = separate_speaker_turns(
                    transcript.segments,
                    capture_mode=metadata.capture_mode,
                    method=resolved_method,
                    policy=metadata.separation_policy,
                    learner_turn_labels=metadata.learner_turn_labels,
                )
                source = SourceTranscript(
                    reference=transcript.reference,
                    learner_id=metadata.learner_id,
                    recorded_at=transcript.recorded_at,
                    capture_mode=metadata.capture_mode,
                    situation=metadata.situation,
                    turns=separation.turns,
                    consent_attested=True,
                    all_participants_consented=grant.all_participants_consented,
                    revoked=grant.revoked,
                    retention_policy_present=True,
                    provenance=transcript.provenance,
                )
                minimized = self._minimization.minimize(source, minimized_at=now)
                proposal = self._proposal.propose(
                    minimized,
                    consent_id=grant.consent_id,
                    consent_attested_at=datetime.fromisoformat(
                        grant.attested_at.replace("Z", "+00:00")
                    ),
                    all_participants_consented=grant.all_participants_consented,
                    learner_goal=metadata.learner_goal,
                    consent_revoked=grant.revoked,
                    policy_version=grant.policy_version,
                    device_type=transcript.device_type,
                    diarization_method=separation.diarization_method.value,
                    recurrence_id=metadata.recurrence_id,
                    proposed_at=now,
                )
            except (
                ConsentError,
                SensitiveContentError,
                BreakdownValidationError,
                ValueError,
            ) as error:
                # NFR-NT-004: immediate deletion on failure
                self._store.delete_transient_by_reference(
                    learner_id=metadata.learner_id,
                    source_reference=transcript.reference,
                )
                code = (
                    "sensitive_content_excluded"
                    if isinstance(error, SensitiveContentError)
                    else "consent_rejected"
                    if isinstance(error, ConsentError)
                    else "minimization_failed"
                )
                return (
                    IngestedRecordingOutcome(
                        reference=transcript.reference,
                        status="rejected_invalid_transcript",
                        error_code=code,
                    ),
                    None,
                )

            if proposal.attempt_id not in self._store.attempts:
                self._store.attempts[proposal.attempt_id] = proposal.attempt_document
            # SPEC §7 step 8: delete transient source immediately after minimized draft secured
            self._store.delete_transient_by_reference(
                learner_id=metadata.learner_id,
                source_reference=transcript.reference,
            )
            outcome_status: Literal["ingested", "duplicate"] = (
                "duplicate" if proposal.duplicate else "ingested"
            )
            return (
                IngestedRecordingOutcome(
                    reference=transcript.reference,
                    status=outcome_status,
                    attempt_id=proposal.attempt_id,
                ),
                proposal,
            )

    async def sync_once(
        self,
        *,
        learner_id: str,
        worker_id: str = "local-bridge-worker-1",
        now: datetime | None = None,
    ) -> BridgeSyncReport:
        sync_time = (now or datetime.now(UTC)).astimezone(UTC)
        _iso_utc(now or sync_time)
        lease = self._acquire_lease(learner_id=learner_id, worker_id=worker_id, now=sync_time)
        current_cursor = lease.cursor

        try:
            if self._enforce_revoked_consents(learner_id=learner_id, now=sync_time):
                return BridgeSyncReport(
                    learner_id=learner_id,
                    status="stopped_consent_revoked",
                    outcomes=(),
                    cursor=current_cursor,
                    ingested_count=0,
                    duplicate_count=0,
                    rejected_count=0,
                )

            try:
                page = await self._adapter.list_recordings(current_cursor)
            except BeeAdapterError as error:
                return BridgeSyncReport(
                    learner_id=learner_id,
                    status="adapter_error",
                    outcomes=(),
                    cursor=current_cursor,
                    ingested_count=0,
                    duplicate_count=0,
                    rejected_count=0,
                    error_detail=f"{error.code}:{redact_bridge_log(str(error))}",
                )

            outcomes: list[IngestedRecordingOutcome] = []
            ingested = 0
            duplicates = 0
            rejected = 0

            for summary in page.items:
                if self._enforce_revoked_consents(learner_id=learner_id, now=sync_time):
                    return BridgeSyncReport(
                        learner_id=learner_id,
                        status="stopped_consent_revoked",
                        outcomes=tuple(outcomes),
                        cursor=current_cursor,
                        ingested_count=ingested,
                        duplicate_count=duplicates,
                        rejected_count=rejected,
                    )

                meta = self._pending_metadata.get((learner_id, summary.reference))
                if meta is None:
                    self._store.delete_transient_by_reference(
                        learner_id=learner_id,
                        source_reference=summary.reference,
                    )
                    outcomes.append(
                        IngestedRecordingOutcome(
                            reference=summary.reference,
                            status="rejected_missing_consent",
                            error_code="capture_metadata_not_attested",
                        )
                    )
                    rejected += 1
                    continue

                try:
                    transcript = await self._adapter.get_transcript(summary.reference)
                except BeeAdapterError as error:
                    return BridgeSyncReport(
                        learner_id=learner_id,
                        status="adapter_error",
                        outcomes=tuple(outcomes),
                        cursor=current_cursor,
                        ingested_count=ingested,
                        duplicate_count=duplicates,
                        rejected_count=rejected,
                        error_detail=f"{error.code}:{redact_bridge_log(str(error))}",
                    )

                outcome, _ = await self.ingest_transcript(
                    transcript,
                    meta,
                    ingested_at=sync_time,
                )
                outcomes.append(outcome)
                if outcome.status == "stopped_consent_revoked":
                    return BridgeSyncReport(
                        learner_id=learner_id,
                        status="stopped_consent_revoked",
                        outcomes=tuple(outcomes),
                        cursor=current_cursor,
                        ingested_count=ingested,
                        duplicate_count=duplicates,
                        rejected_count=rejected,
                    )
                if outcome.status in {"ingested", "duplicate"}:
                    ack_cursor = page.next_cursor or summary.reference
                    await self._adapter.acknowledge(summary.reference, ack_cursor)
                    current_cursor = ack_cursor
                    if outcome.status == "ingested":
                        ingested += 1
                    else:
                        duplicates += 1
                else:
                    rejected += 1

            return BridgeSyncReport(
                learner_id=learner_id,
                status="completed",
                outcomes=tuple(outcomes),
                cursor=current_cursor,
                ingested_count=ingested,
                duplicate_count=duplicates,
                rejected_count=rejected,
            )
        finally:
            self._release_lease(
                learner_id=learner_id,
                cursor=current_cursor,
                synced_at=sync_time,
            )
