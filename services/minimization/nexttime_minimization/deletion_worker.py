from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from nexttime_domain.breakdowns import deterministic_ulid
from nexttime_domain.deletion import (
    ConsentGrant,
    DeletionJob,
    DeletionJobStatus,
    DeletionScope,
    DeletionStageName,
    DeletionStorePort,
    OverdueTransientAlarm,
    StageReceipt,
    scan_overdue_transient_sources,
    stages_for_scope,
)

from .store import InMemoryArtifactStore


def _iso_millis(value: datetime, field: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DeletionPipelineWorker:
    """Executes verified multi-stage deletion, revocation, and retention jobs."""

    def __init__(self, store: InMemoryArtifactStore) -> None:
        self._store: InMemoryArtifactStore = store
        self._port: DeletionStorePort = store
        self._jobs: dict[str, DeletionJob] = {}
        self._job_sources: dict[str, tuple[str, ...]] = {}
        self._alarms: list[OverdueTransientAlarm] = []

    @property
    def jobs(self) -> tuple[DeletionJob, ...]:
        return tuple(self._jobs.values())

    @property
    def alarms(self) -> tuple[OverdueTransientAlarm, ...]:
        return tuple(self._alarms)

    def get_job(self, *, actor_learner_id: str, job_id: str) -> DeletionJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError("deletion_job_not_found")
        if actor_learner_id != job.learner_id:
            raise PermissionError("deletion_job_access_denied")
        return job

    def schedule_job(
        self,
        *,
        learner_id: str,
        scope: DeletionScope,
        created_at: datetime,
        target_attempt_ids: tuple[str, ...] = (),
        consent_id: str | None = None,
        source_references: tuple[str, ...] = (),
    ) -> DeletionJob:
        created_iso = _iso_millis(created_at, "Deletion job created_at")
        seed = (
            f"{learner_id}:{scope.value}:{consent_id or ''}:"
            f"{','.join(sorted(target_attempt_ids))}:{','.join(sorted(source_references))}"
        )
        job_id = deterministic_ulid(
            namespace="deletion-job",
            seed=seed,
            timestamp=created_at,
        )
        existing = self._jobs.get(job_id)
        if existing is not None:
            return existing

        job = DeletionJob(
            job_id=job_id,
            learner_id=learner_id,
            scope=scope,
            target_attempt_ids=tuple(sorted(set(target_attempt_ids))),
            consent_id=consent_id,
            status=DeletionJobStatus.PENDING,
            stage_receipts=(),
            alarm_triggered=False,
            alarm_codes=(),
            created_at=created_iso,
            updated_at=created_iso,
            completed_at=None,
        )
        self._jobs[job_id] = job
        self._job_sources[job_id] = tuple(sorted(set(source_references)))
        return job

    def _run_stage(
        self,
        job: DeletionJob,
        stage: DeletionStageName,
        *,
        source_references: tuple[str, ...],
        executed_iso: str,
        executed_at: datetime,
    ) -> StageReceipt:
        full_account = job.scope is DeletionScope.ACCOUNT
        receipt_id = deterministic_ulid(
            namespace="stage-receipt",
            seed=f"{job.job_id}:{stage.value}:{executed_iso}",
            timestamp=executed_at,
        )
        try:
            if stage is DeletionStageName.TRANSIENT_SOURCE:
                deleted = self._port.purge_transient_sources(
                    learner_id=job.learner_id,
                    attempt_ids=job.target_attempt_ids,
                    consent_id=job.consent_id,
                    source_references=source_references,
                    full_account=full_account,
                )
                verified = self._port.verify_transient_sources_empty(
                    learner_id=job.learner_id,
                    attempt_ids=job.target_attempt_ids,
                    consent_id=job.consent_id,
                    source_references=source_references,
                    full_account=full_account,
                )
            elif stage is DeletionStageName.AUDIO_AND_REHEARSAL:
                deleted = self._port.purge_audio_and_rehearsals(
                    learner_id=job.learner_id,
                    attempt_ids=job.target_attempt_ids,
                    full_account=full_account,
                )
                verified = self._port.verify_audio_and_rehearsals_empty(
                    learner_id=job.learner_id,
                    attempt_ids=job.target_attempt_ids,
                    full_account=full_account,
                )
            elif stage is DeletionStageName.CACHES_AND_PROMPTS:
                deleted = self._port.purge_caches_and_prompts(
                    learner_id=job.learner_id,
                    attempt_ids=job.target_attempt_ids,
                    full_account=full_account,
                )
                verified = self._port.verify_caches_and_prompts_empty(
                    learner_id=job.learner_id,
                    attempt_ids=job.target_attempt_ids,
                    full_account=full_account,
                )
            else:
                deleted = self._port.purge_derived_learner_state(
                    learner_id=job.learner_id,
                    attempt_ids=job.target_attempt_ids,
                    job_id=job.job_id,
                    deleted_at=executed_iso,
                    full_account=full_account,
                )
                verified = self._port.verify_derived_learner_state_empty(
                    learner_id=job.learner_id,
                    attempt_ids=job.target_attempt_ids,
                    full_account=full_account,
                )
        except RuntimeError as error:
            return StageReceipt(
                stage=stage,
                deleted_count=0,
                verified_empty=False,
                completed_at=executed_iso,
                receipt_id=receipt_id,
                error_code=str(error),
            )

        error_code = None if verified else f"{stage.value}_verification_failed"
        return StageReceipt(
            stage=stage,
            deleted_count=deleted,
            verified_empty=verified,
            completed_at=executed_iso,
            receipt_id=receipt_id,
            error_code=error_code,
        )

    def execute_job(self, job_id: str, *, executed_at: datetime) -> DeletionJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError("deletion_job_not_found")
        executed_iso = _iso_millis(executed_at, "Deletion execution timestamp")
        if job.status is DeletionJobStatus.COMPLETED:
            return job

        completed_by_stage = {
            receipt.stage: receipt
            for receipt in job.stage_receipts
            if receipt.verified_empty and receipt.error_code is None
        }
        source_refs = self._job_sources.get(job_id, ())
        receipts: list[StageReceipt] = []
        alarm_codes: list[str] = []

        for stage in stages_for_scope(job.scope):
            if stage in completed_by_stage:
                receipts.append(completed_by_stage[stage])
                continue
            receipt = self._run_stage(
                job,
                stage,
                source_references=source_refs,
                executed_iso=executed_iso,
                executed_at=executed_at,
            )
            receipts.append(receipt)
            if not receipt.verified_empty or receipt.error_code is not None:
                alarm_codes.append(receipt.error_code or f"{stage.value}_unverified")

        if alarm_codes:
            updated = replace(
                job,
                status=DeletionJobStatus.FAILED_ALARMED,
                stage_receipts=tuple(receipts),
                alarm_triggered=True,
                alarm_codes=tuple(sorted(set(alarm_codes))),
                updated_at=executed_iso,
                completed_at=None,
            )
        else:
            updated = replace(
                job,
                status=DeletionJobStatus.COMPLETED,
                stage_receipts=tuple(receipts),
                alarm_triggered=False,
                alarm_codes=(),
                updated_at=executed_iso,
                completed_at=executed_iso,
            )
        self._jobs[job_id] = updated
        return updated

    def revoke_consent_and_delete(
        self,
        *,
        actor_learner_id: str,
        consent_id: str,
        revoked_at: datetime,
    ) -> tuple[ConsentGrant, DeletionJob]:
        grant = self._store.consents.get(consent_id)
        if grant is None:
            raise KeyError("consent_not_found")
        revoked_grant = grant.revoke(actor_learner_id=actor_learner_id, revoked_at=revoked_at)
        self._store.save_consent(revoked_grant)
        revoked_iso = _iso_millis(revoked_at, "Revocation timestamp")

        matching_attempt_ids: list[str] = []
        matching_refs: list[str] = []
        for aid, doc in self._store.attempts.items():
            if (
                doc.get("learner_id") == revoked_grant.learner_id
                and doc.get("consent", {}).get("consent_id") == consent_id
            ):
                doc["consent"]["revoked"] = True
                doc["consent"]["revoked_at"] = revoked_iso
                doc["status"] = "deletion_pending"
                doc["outcome"]["confirmed_by_learner"] = False
                doc["updated_at"] = revoked_iso
                matching_attempt_ids.append(aid)
                ref = doc.get("source", {}).get("bee_recording_reference")
                if isinstance(ref, str):
                    matching_refs.append(ref)

        for aid, tomb in self._store.tombstones.items():
            if tomb.learner_id == revoked_grant.learner_id and aid not in matching_attempt_ids:
                matching_attempt_ids.append(aid)

        job = self.schedule_job(
            learner_id=revoked_grant.learner_id,
            scope=DeletionScope.CONSENT_REVOCATION,
            created_at=revoked_at,
            target_attempt_ids=tuple(matching_attempt_ids),
            consent_id=consent_id,
            source_references=tuple(matching_refs),
        )
        executed = self.execute_job(job.job_id, executed_at=revoked_at)
        return revoked_grant, executed

    def delete_attempt(
        self,
        *,
        actor_learner_id: str,
        attempt_id: str,
        requested_at: datetime,
    ) -> DeletionJob:
        doc = self._store.attempts.get(attempt_id)
        if doc is None:
            tombstone = self._store.tombstones.get(attempt_id)
            if tombstone is not None:
                if tombstone.learner_id != actor_learner_id:
                    raise PermissionError("attempt_access_denied")
                return self._jobs[tombstone.job_id]
            raise KeyError("attempt_not_found")
        if doc.get("learner_id") != actor_learner_id:
            raise PermissionError("attempt_access_denied")

        requested_iso = _iso_millis(requested_at, "Attempt deletion timestamp")
        doc["status"] = "deletion_pending"
        doc["outcome"]["confirmed_by_learner"] = False
        doc["updated_at"] = requested_iso
        ref = doc.get("source", {}).get("bee_recording_reference", "")
        refs = (ref,) if isinstance(ref, str) and ref else ()

        job = self.schedule_job(
            learner_id=actor_learner_id,
            scope=DeletionScope.ATTEMPT,
            created_at=requested_at,
            target_attempt_ids=(attempt_id,),
            source_references=refs,
        )
        return self.execute_job(job.job_id, executed_at=requested_at)

    def sweep_expired_transient_sources(
        self,
        *,
        now: datetime,
    ) -> tuple[tuple[OverdueTransientAlarm, ...], tuple[DeletionJob, ...]]:
        records = [rec for rec, _ in self._store.transient_sources.values()]
        detected_alarms = scan_overdue_transient_sources(records, now=now)
        self._alarms.extend(detected_alarms)

        by_learner: dict[str, list[str]] = {}
        for alarm in detected_alarms:
            by_learner.setdefault(alarm.learner_id, []).append(alarm.source_reference)

        executed_jobs: list[DeletionJob] = []
        for learner_id, refs in sorted(by_learner.items()):
            job = self.schedule_job(
                learner_id=learner_id,
                scope=DeletionScope.EXPIRED_TRANSIENT,
                created_at=now,
                source_references=tuple(refs),
            )
            executed_jobs.append(self.execute_job(job.job_id, executed_at=now))
        return detected_alarms, tuple(executed_jobs)

    def delete_account(
        self,
        *,
        actor_learner_id: str,
        learner_id: str,
        recent_auth_verified: bool,
        requested_at: datetime,
    ) -> DeletionJob:
        if actor_learner_id != learner_id:
            raise PermissionError("account_access_denied")
        if not recent_auth_verified:
            raise PermissionError("recent_auth_required")

        requested_iso = _iso_millis(requested_at, "Account deletion timestamp")
        attempt_ids = tuple(
            sorted(
                aid
                for aid, doc in self._store.attempts.items()
                if doc.get("learner_id") == learner_id
            )
        )
        for aid in attempt_ids:
            self._store.attempts[aid]["status"] = "deletion_pending"
            self._store.attempts[aid]["updated_at"] = requested_iso

        job = self.schedule_job(
            learner_id=learner_id,
            scope=DeletionScope.ACCOUNT,
            created_at=requested_at,
            target_attempt_ids=attempt_ids,
        )
        return self.execute_job(job.job_id, executed_at=requested_at)
