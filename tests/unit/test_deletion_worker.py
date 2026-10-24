from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from nexttime_domain import (
    CaptureMode,
    ConsentError,
    ConsentGrant,
    DeletionJobStatus,
    DeletionScope,
    DeletionStageName,
    SkillState,
    TransientTranscriptRecord,
    build_learner_export,
    scan_overdue_transient_sources,
)
from nexttime_minimization import DeletionPipelineWorker, InMemoryArtifactStore

LEARNER_A = "01000000000000000000000002"
LEARNER_B = "01999999999999999999999999"
CONSENT_ID = "01000000000000000000000004"


def _seed_store(attempt_document: dict[str, Any]) -> tuple[InMemoryArtifactStore, datetime]:
    now = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    store = InMemoryArtifactStore()
    grant = ConsentGrant.create(
        consent_id=CONSENT_ID,
        learner_id=LEARNER_A,
        capture_mode=CaptureMode.CONSENTED_CONVERSATION,
        policy_version="consent_v1",
        all_participants_consented=True,
        attested_at=now - timedelta(hours=2),
    )
    store.save_consent(grant)

    doc = deepcopy(attempt_document)
    attempt_id = doc["attempt_id"]
    store.attempts[attempt_id] = doc
    store.put_transient_source(
        TransientTranscriptRecord.create(
            learner_id=LEARNER_A,
            source_reference=doc["source"]["bee_recording_reference"],
            consent_id=CONSENT_ID,
            encrypted_uri="s3://transient/bee-001",
            created_at=now - timedelta(hours=1),
            attempt_id=attempt_id,
        ),
        "encrypted-raw-payload",
    )
    store.put_cache_entry(
        cache_key=f"scenario:{attempt_id}",
        learner_id=LEARNER_A,
        attempt_id=attempt_id,
        payload={"template_version": "rehearsal_repair_v1"},
    )
    store.recurrences["01000000000000000000000004"] = {
        "recurrence_id": "01000000000000000000000004",
        "learner_id": LEARNER_A,
        "attempt_id": attempt_id,
    }
    store.rehearsal_sessions["reh-001"] = {
        "rehearsal_id": "reh-001",
        "learner_id": LEARNER_A,
        "attempt_id": attempt_id,
    }
    store.comparisons[f"prior:{attempt_id}"] = {
        "prior_attempt_id": "01000000000000000000000099",
        "next_attempt_id": attempt_id,
        "learner_id": LEARNER_A,
    }
    store.skill_states[LEARNER_A] = {
        "repair_goal_statement": SkillState(alpha=2.0, beta=1.0, authentic_attempt_count=1)
    }
    return store, now


def test_consent_grant_validation_and_revocation() -> None:
    now = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    with pytest.raises(ConsentError):
        ConsentGrant.create(
            consent_id=CONSENT_ID,
            learner_id=LEARNER_A,
            capture_mode=CaptureMode.CONSENTED_CONVERSATION,
            policy_version="consent_v1",
            all_participants_consented=False,
            attested_at=now,
        )
    grant = ConsentGrant.create(
        consent_id=CONSENT_ID,
        learner_id=LEARNER_A,
        capture_mode=CaptureMode.SELF_MEMO,
        policy_version="consent_v1",
        all_participants_consented=False,
        attested_at=now,
    )
    with pytest.raises(PermissionError, match="consent_access_denied"):
        grant.revoke(actor_learner_id=LEARNER_B, revoked_at=now + timedelta(minutes=1))

    revoked = grant.revoke(actor_learner_id=LEARNER_A, revoked_at=now + timedelta(minutes=5))
    assert revoked.revoked
    assert revoked.version == 2
    assert (
        revoked.revoke(actor_learner_id=LEARNER_A, revoked_at=now + timedelta(minutes=10))
        == revoked
    )


def test_consent_revocation_cascades_across_all_four_stages_and_supports_retry(
    attempt_document: dict[str, Any],
) -> None:
    store, now = _seed_store(attempt_document)
    worker = DeletionPipelineWorker(store)
    attempt_id = attempt_document["attempt_id"]

    with pytest.raises(PermissionError, match="consent_access_denied"):
        worker.revoke_consent_and_delete(
            actor_learner_id=LEARNER_B,
            consent_id=CONSENT_ID,
            revoked_at=now,
        )

    # Inject transient stage failure to verify alarm + idempotent retry recovery
    store.fail_purge_stages.add(DeletionStageName.CACHES_AND_PROMPTS)
    grant, failed_job = worker.revoke_consent_and_delete(
        actor_learner_id=LEARNER_A,
        consent_id=CONSENT_ID,
        revoked_at=now,
    )
    assert grant.revoked
    assert failed_job.status is DeletionJobStatus.FAILED_ALARMED
    assert failed_job.alarm_triggered

    # Clear fault and retry job -> completes remaining stage!
    store.fail_purge_stages.clear()
    recovered_job = worker.execute_job(failed_job.job_id, executed_at=now + timedelta(minutes=1))
    assert recovered_job.status is DeletionJobStatus.COMPLETED
    assert [r.stage for r in recovered_job.stage_receipts] == list(DeletionStageName)
    assert all(r.verified_empty for r in recovered_job.stage_receipts)
    assert attempt_id not in store.attempts
    assert attempt_id in store.tombstones
    assert not store.transient_sources
    assert not store.caches_and_prompts
    assert not store.rehearsal_sessions
    assert not store.comparisons

    # Verify get_job authorization
    assert worker.get_job(actor_learner_id=LEARNER_A, job_id=recovered_job.job_id) == recovered_job
    with pytest.raises(PermissionError, match="deletion_job_access_denied"):
        worker.get_job(actor_learner_id=LEARNER_B, job_id=recovered_job.job_id)


def test_overdue_transient_sweep_raises_alarm_when_past_24h(
    attempt_document: dict[str, Any],
) -> None:
    store, now = _seed_store(attempt_document)
    overdue_record = TransientTranscriptRecord.create(
        learner_id=LEARNER_A,
        source_reference="bee-overdue-ref",
        consent_id=CONSENT_ID,
        encrypted_uri="s3://transient/overdue",
        created_at=now - timedelta(hours=26),
    )
    store.put_transient_source(overdue_record, "stale-payload")

    records = [rec for rec, _ in store.transient_sources.values()]
    found = scan_overdue_transient_sources(records, now=now)
    assert len(found) == 1
    assert found[0].source_reference == "bee-overdue-ref"

    worker = DeletionPipelineWorker(store)
    alarms, jobs = worker.sweep_expired_transient_sources(now=now)
    assert len(alarms) == 1
    assert len(jobs) == 1
    assert jobs[0].status is DeletionJobStatus.COMPLETED
    assert overdue_record.transient_id not in store.transient_sources
    assert len(worker.alarms) == 1


def test_rehearsal_raw_audio_deleted_immediately_and_unsaved_transcript_purged_at_24h(
    attempt_document: dict[str, Any],
) -> None:
    store, now = _seed_store(attempt_document)

    receipt = store.record_rehearsal_turn_artifacts(
        turn_record_id="turn-unsaved",
        learner_id=LEARNER_A,
        rehearsal_id="reh-001",
        attempt_id=attempt_document["attempt_id"],
        learner_transcript="Ik wil graag een afspraak.",
        recorded_at=now,
        raw_audio_bytes=b"\x00\x01pcm",
        save_self_excerpt=False,
    )
    assert receipt["raw_audio_deleted"] is True
    assert receipt["delete_after"] is not None
    assert not store.rehearsal_raw_audio

    saved_receipt = store.record_rehearsal_turn_artifacts(
        turn_record_id="turn-saved",
        learner_id=LEARNER_A,
        rehearsal_id="reh-001",
        attempt_id=attempt_document["attempt_id"],
        learner_transcript="Kan het om tien uur?",
        recorded_at=now,
        raw_audio_bytes=None,
        save_self_excerpt=True,
    )
    assert saved_receipt["delete_after"] is None

    purged = store.sweep_expired_rehearsal_transcripts(now=now + timedelta(hours=25))
    assert purged == 1
    assert "turn-unsaved" not in store.rehearsal_turn_transcripts
    assert "turn-saved" in store.rehearsal_turn_transcripts


def test_delete_attempt_account_deletion_and_minimized_export(
    attempt_document: dict[str, Any],
) -> None:
    store, now = _seed_store(attempt_document)
    worker = DeletionPipelineWorker(store)
    aid = attempt_document["attempt_id"]

    with pytest.raises(PermissionError, match="recent_auth_required"):
        build_learner_export(
            actor_learner_id=LEARNER_A,
            learner_id=LEARNER_A,
            recent_auth_verified=False,
            exported_at=now,
            consents=list(store.consents.values()),
            attempts=list(store.attempts.values()),
            recurrences=list(store.recurrences.values()),
            rehearsal_history=[],
            comparisons=list(store.comparisons.values()),
            skill_states=store.skill_states[LEARNER_A],
        )

    export = build_learner_export(
        actor_learner_id=LEARNER_A,
        learner_id=LEARNER_A,
        recent_auth_verified=True,
        exported_at=now,
        consents=list(store.consents.values()),
        attempts=list(store.attempts.values()),
        recurrences=list(store.recurrences.values()),
        rehearsal_history=[],
        comparisons=list(store.comparisons.values()),
        skill_states=store.skill_states[LEARNER_A],
    )
    assert export.other_speaker_transcript_included is False
    assert len(export.attempts) == 1

    # Single attempt deletion + idempotent tombstone replay
    del_job = worker.delete_attempt(
        actor_learner_id=LEARNER_A,
        attempt_id=aid,
        requested_at=now,
    )
    assert del_job.status is DeletionJobStatus.COMPLETED
    assert worker.delete_attempt(actor_learner_id=LEARNER_A, attempt_id=aid, requested_at=now) == (
        del_job
    )

    with pytest.raises(PermissionError, match="recent_auth_required"):
        worker.delete_account(
            actor_learner_id=LEARNER_A,
            learner_id=LEARNER_A,
            recent_auth_verified=False,
            requested_at=now,
        )

    job = worker.delete_account(
        actor_learner_id=LEARNER_A,
        learner_id=LEARNER_A,
        recent_auth_verified=True,
        requested_at=now,
    )
    assert job.scope is DeletionScope.ACCOUNT
    assert job.status is DeletionJobStatus.COMPLETED
    assert not store.attempts
    assert LEARNER_A not in store.skill_states
    assert aid in store.tombstones
