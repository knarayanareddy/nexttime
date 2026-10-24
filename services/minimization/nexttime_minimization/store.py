from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

from nexttime_domain.deletion import (
    ConsentGrant,
    DeletionStageName,
    DeletionTombstone,
    TransientTranscriptRecord,
)
from nexttime_domain.models import SkillState


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Store timestamp must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class InMemoryArtifactStore:
    """In-memory multi-stage artifact store implementing DeletionStorePort."""

    def __init__(self) -> None:
        self.consents: dict[str, ConsentGrant] = {}
        self.transient_sources: dict[str, tuple[TransientTranscriptRecord, str]] = {}
        self.attempts: dict[str, dict[str, Any]] = {}
        self.tombstones: dict[str, DeletionTombstone] = {}
        self.recurrences: dict[str, dict[str, Any]] = {}
        self.rehearsal_sessions: dict[str, dict[str, Any]] = {}
        self.rehearsal_raw_audio: dict[str, dict[str, Any]] = {}
        self.rehearsal_turn_transcripts: dict[str, dict[str, Any]] = {}
        self.caches_and_prompts: dict[str, dict[str, Any]] = {}
        self.comparisons: dict[str, dict[str, Any]] = {}
        self.skill_states: dict[str, dict[str, SkillState]] = {}

        self.fail_purge_stages: set[DeletionStageName] = set()
        self.fail_verify_stages: set[DeletionStageName] = set()

    def save_consent(self, grant: ConsentGrant) -> None:
        self.consents[grant.consent_id] = grant

    def is_consent_active(self, *, learner_id: str, consent_id: str) -> bool:
        grant = self.consents.get(consent_id)
        return grant is not None and grant.learner_id == learner_id and not grant.revoked

    def has_revoked_consent_for_learner(self, learner_id: str) -> bool:
        return any(
            grant.revoked for grant in self.consents.values() if grant.learner_id == learner_id
        )

    def put_transient_source(self, record: TransientTranscriptRecord, raw_payload: str) -> None:
        self.transient_sources[record.transient_id] = (record, raw_payload)

    def delete_transient_by_reference(self, *, learner_id: str, source_reference: str) -> int:
        to_remove = [
            tid
            for tid, (rec, _) in self.transient_sources.items()
            if rec.learner_id == learner_id and rec.source_reference == source_reference
        ]
        for tid in to_remove:
            del self.transient_sources[tid]
        return len(to_remove)

    def record_rehearsal_turn_artifacts(
        self,
        *,
        turn_record_id: str,
        learner_id: str,
        rehearsal_id: str,
        attempt_id: str,
        learner_transcript: str,
        recorded_at: datetime,
        raw_audio_bytes: bytes | None = None,
        save_self_excerpt: bool = False,
    ) -> dict[str, Any]:
        if raw_audio_bytes is not None:
            self.rehearsal_raw_audio[turn_record_id] = {
                "learner_id": learner_id,
                "attempt_id": attempt_id,
                "bytes": raw_audio_bytes,
            }
            # SEC-NT-004: raw rehearsal audio deletes immediately after transcription
            del self.rehearsal_raw_audio[turn_record_id]
            raw_audio_deleted = True
        else:
            raw_audio_deleted = True

        delete_after = None if save_self_excerpt else _iso_utc(recorded_at + timedelta(hours=24))
        entry = {
            "turn_record_id": turn_record_id,
            "learner_id": learner_id,
            "rehearsal_id": rehearsal_id,
            "attempt_id": attempt_id,
            "learner_transcript": learner_transcript,
            "saved_self_excerpt": save_self_excerpt,
            "raw_audio_deleted": raw_audio_deleted,
            "created_at": _iso_utc(recorded_at),
            "delete_after": delete_after,
        }
        self.rehearsal_turn_transcripts[turn_record_id] = entry
        return deepcopy(entry)

    def sweep_expired_rehearsal_transcripts(self, *, now: datetime) -> int:
        now_utc = _parse_iso(_iso_utc(now))
        expired_ids = [
            tid
            for tid, item in self.rehearsal_turn_transcripts.items()
            if not item["saved_self_excerpt"]
            and item["delete_after"] is not None
            and now_utc > _parse_iso(item["delete_after"])
        ]
        for tid in expired_ids:
            del self.rehearsal_turn_transcripts[tid]
        return len(expired_ids)

    def put_cache_entry(
        self,
        *,
        cache_key: str,
        learner_id: str,
        attempt_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        self.caches_and_prompts[cache_key] = {
            "learner_id": learner_id,
            "attempt_id": attempt_id,
            "payload": deepcopy(payload),
        }

    def _matches_transient(
        self,
        rec: TransientTranscriptRecord,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        consent_id: str | None,
        source_references: tuple[str, ...],
        full_account: bool,
    ) -> bool:
        if rec.learner_id != learner_id:
            return False
        if full_account:
            return True
        if consent_id is not None and rec.consent_id == consent_id:
            return True
        if rec.attempt_id is not None and rec.attempt_id in attempt_ids:
            return True
        return rec.source_reference in source_references

    def purge_transient_sources(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        consent_id: str | None,
        source_references: tuple[str, ...],
        full_account: bool,
    ) -> int:
        if DeletionStageName.TRANSIENT_SOURCE in self.fail_purge_stages:
            raise RuntimeError("transient_source_purge_failed")
        matching = [
            tid
            for tid, (rec, _) in self.transient_sources.items()
            if self._matches_transient(
                rec,
                learner_id=learner_id,
                attempt_ids=attempt_ids,
                consent_id=consent_id,
                source_references=source_references,
                full_account=full_account,
            )
        ]
        for tid in matching:
            del self.transient_sources[tid]
        return len(matching)

    def verify_transient_sources_empty(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        consent_id: str | None,
        source_references: tuple[str, ...],
        full_account: bool,
    ) -> bool:
        if DeletionStageName.TRANSIENT_SOURCE in self.fail_verify_stages:
            return False
        return not any(
            self._matches_transient(
                rec,
                learner_id=learner_id,
                attempt_ids=attempt_ids,
                consent_id=consent_id,
                source_references=source_references,
                full_account=full_account,
            )
            for rec, _ in self.transient_sources.values()
        )

    def _matches_attempt_scoped(
        self,
        item: dict[str, Any],
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        full_account: bool,
    ) -> bool:
        if item.get("learner_id") != learner_id:
            return False
        if full_account:
            return True
        return item.get("attempt_id") in attempt_ids

    def purge_audio_and_rehearsals(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        full_account: bool,
    ) -> int:
        if DeletionStageName.AUDIO_AND_REHEARSAL in self.fail_purge_stages:
            raise RuntimeError("audio_and_rehearsal_purge_failed")
        removed = 0
        for bucket in (
            self.rehearsal_raw_audio,
            self.rehearsal_turn_transcripts,
            self.rehearsal_sessions,
        ):
            keys = [
                k
                for k, v in bucket.items()
                if self._matches_attempt_scoped(
                    v,
                    learner_id=learner_id,
                    attempt_ids=attempt_ids,
                    full_account=full_account,
                )
            ]
            for k in keys:
                del bucket[k]
                removed += 1
        return removed

    def verify_audio_and_rehearsals_empty(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        full_account: bool,
    ) -> bool:
        if DeletionStageName.AUDIO_AND_REHEARSAL in self.fail_verify_stages:
            return False
        return not any(
            self._matches_attempt_scoped(
                v,
                learner_id=learner_id,
                attempt_ids=attempt_ids,
                full_account=full_account,
            )
            for bucket in (
                self.rehearsal_raw_audio,
                self.rehearsal_turn_transcripts,
                self.rehearsal_sessions,
            )
            for v in bucket.values()
        )

    def purge_caches_and_prompts(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        full_account: bool,
    ) -> int:
        if DeletionStageName.CACHES_AND_PROMPTS in self.fail_purge_stages:
            raise RuntimeError("caches_and_prompts_purge_failed")
        keys = [
            k
            for k, v in self.caches_and_prompts.items()
            if self._matches_attempt_scoped(
                v,
                learner_id=learner_id,
                attempt_ids=attempt_ids,
                full_account=full_account,
            )
        ]
        for k in keys:
            del self.caches_and_prompts[k]
        return len(keys)

    def verify_caches_and_prompts_empty(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        full_account: bool,
    ) -> bool:
        if DeletionStageName.CACHES_AND_PROMPTS in self.fail_verify_stages:
            return False
        return not any(
            self._matches_attempt_scoped(
                v,
                learner_id=learner_id,
                attempt_ids=attempt_ids,
                full_account=full_account,
            )
            for v in self.caches_and_prompts.values()
        )

    def purge_derived_learner_state(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        job_id: str,
        deleted_at: str,
        full_account: bool,
    ) -> int:
        if DeletionStageName.DERIVED_LEARNER_STATE in self.fail_purge_stages:
            raise RuntimeError("derived_learner_state_purge_failed")
        removed = 0
        target_ids = (
            [aid for aid, doc in self.attempts.items() if doc.get("learner_id") == learner_id]
            if full_account
            else [
                aid
                for aid in attempt_ids
                if aid in self.attempts and self.attempts[aid].get("learner_id") == learner_id
            ]
        )
        for aid in target_ids:
            del self.attempts[aid]
            self.tombstones[aid] = DeletionTombstone(
                attempt_id=aid,
                learner_id=learner_id,
                deleted_at=deleted_at,
                job_id=job_id,
            )
            removed += 1

        comp_keys = [
            cid
            for cid, comp in self.comparisons.items()
            if comp.get("learner_id") == learner_id
            and (
                full_account
                or comp.get("prior_attempt_id") in attempt_ids
                or comp.get("next_attempt_id") in attempt_ids
            )
        ]
        for cid in comp_keys:
            del self.comparisons[cid]
            removed += 1

        rec_keys = [
            rid
            for rid, rec in self.recurrences.items()
            if rec.get("learner_id") == learner_id
            and (full_account or rec.get("attempt_id") in attempt_ids)
        ]
        for rid in rec_keys:
            del self.recurrences[rid]
            removed += 1

        if full_account and learner_id in self.skill_states:
            del self.skill_states[learner_id]
            removed += 1

        if full_account:
            consent_keys = [
                cid for cid, grant in self.consents.items() if grant.learner_id == learner_id
            ]
            for cid in consent_keys:
                del self.consents[cid]
                removed += 1

        return removed

    def verify_derived_learner_state_empty(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        full_account: bool,
    ) -> bool:
        if DeletionStageName.DERIVED_LEARNER_STATE in self.fail_verify_stages:
            return False
        if full_account:
            return (
                not any(d.get("learner_id") == learner_id for d in self.attempts.values())
                and not any(c.get("learner_id") == learner_id for c in self.comparisons.values())
                and not any(r.get("learner_id") == learner_id for r in self.recurrences.values())
                and learner_id not in self.skill_states
                and not any(g.learner_id == learner_id for g in self.consents.values())
            )
        return not any(aid in self.attempts for aid in attempt_ids)
