from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Protocol

from .breakdowns import deterministic_ulid
from .consent import ConsentError
from .minimization import find_prohibited_transcript_fields
from .models import SkillState
from .transcripts import CaptureMode

_ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_POLICY_VERSION_PATTERN = re.compile(r"^consent_v[0-9]+$")


def _require_ulid(value: str, field: str) -> str:
    if not _ULID_PATTERN.match(value):
        raise ValueError(f"Invalid {field} ULID")
    return value


def _iso_millis(value: datetime, field: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp string must include a timezone")
    return parsed.astimezone(UTC)


class DeletionScope(StrEnum):
    ATTEMPT = "attempt"
    CONSENT_REVOCATION = "consent_revocation"
    EXPIRED_TRANSIENT = "expired_transient"
    ACCOUNT = "account"


class DeletionStageName(StrEnum):
    TRANSIENT_SOURCE = "transient_source"
    AUDIO_AND_REHEARSAL = "audio_and_rehearsal"
    CACHES_AND_PROMPTS = "caches_and_prompts"
    DERIVED_LEARNER_STATE = "derived_learner_state"


class DeletionJobStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED_ALARMED = "failed_alarmed"


@dataclass(frozen=True)
class ConsentGrant:
    consent_id: str
    learner_id: str
    capture_mode: CaptureMode
    policy_version: str
    all_participants_consented: bool
    attested_at: str
    revoked: bool = False
    revoked_at: str | None = None
    version: int = 1

    @classmethod
    def create(
        cls,
        *,
        consent_id: str,
        learner_id: str,
        capture_mode: CaptureMode,
        policy_version: str,
        all_participants_consented: bool,
        attested_at: datetime,
    ) -> ConsentGrant:
        _require_ulid(consent_id, "consent_id")
        _require_ulid(learner_id, "learner_id")
        if not _POLICY_VERSION_PATTERN.match(policy_version):
            raise ValueError("Invalid consent policy_version")
        if capture_mode is CaptureMode.CONSENTED_CONVERSATION and not all_participants_consented:
            raise ConsentError("Every participant must consent to conversation processing")
        return cls(
            consent_id=consent_id,
            learner_id=learner_id,
            capture_mode=capture_mode,
            policy_version=policy_version,
            all_participants_consented=all_participants_consented,
            attested_at=_iso_millis(attested_at, "Consent attestation timestamp"),
            revoked=False,
            revoked_at=None,
            version=1,
        )

    def revoke(
        self,
        *,
        actor_learner_id: str,
        revoked_at: datetime,
    ) -> ConsentGrant:
        if actor_learner_id != self.learner_id:
            raise PermissionError("consent_access_denied")
        revoked_iso = _iso_millis(revoked_at, "Consent revocation timestamp")
        if self.revoked:
            return self
        if _parse_iso(revoked_iso) < _parse_iso(self.attested_at):
            raise ValueError("Revocation timestamp cannot precede attestation")
        return ConsentGrant(
            consent_id=self.consent_id,
            learner_id=self.learner_id,
            capture_mode=self.capture_mode,
            policy_version=self.policy_version,
            all_participants_consented=self.all_participants_consented,
            attested_at=self.attested_at,
            revoked=True,
            revoked_at=revoked_iso,
            version=self.version + 1,
        )


@dataclass(frozen=True)
class TransientTranscriptRecord:
    transient_id: str
    learner_id: str
    source_reference: str
    consent_id: str
    encrypted_uri: str
    created_at: str
    delete_after: str
    attempt_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        learner_id: str,
        source_reference: str,
        consent_id: str,
        encrypted_uri: str,
        created_at: datetime,
        delete_after: datetime | None = None,
        attempt_id: str | None = None,
    ) -> TransientTranscriptRecord:
        _require_ulid(learner_id, "learner_id")
        _require_ulid(consent_id, "consent_id")
        if attempt_id is not None:
            _require_ulid(attempt_id, "attempt_id")
        if not source_reference.strip():
            raise ValueError("Source reference is required")
        uri = encrypted_uri.strip()
        if not uri or uri.startswith(("http://", "https://")):
            raise ValueError("Transient transcript URI must be private encrypted storage")
        created_iso = _iso_millis(created_at, "Transient created_at")
        created_utc = _parse_iso(created_iso)
        resolved_delete = delete_after or (created_utc + timedelta(hours=24))
        delete_iso = _iso_millis(resolved_delete, "Transient delete_after")
        delete_utc = _parse_iso(delete_iso)
        if delete_utc < created_utc or delete_utc > created_utc + timedelta(hours=24):
            raise ValueError("Transient delete_after must be within 24 hours of creation")
        transient_id = deterministic_ulid(
            namespace="transient",
            seed=f"{learner_id}:{source_reference}",
            timestamp=created_utc,
        )
        return cls(
            transient_id=transient_id,
            learner_id=learner_id,
            source_reference=source_reference.strip(),
            consent_id=consent_id,
            encrypted_uri=uri,
            created_at=created_iso,
            delete_after=delete_iso,
            attempt_id=attempt_id,
        )


@dataclass(frozen=True)
class OverdueTransientAlarm:
    transient_id: str
    learner_id: str
    source_reference: str
    delete_after: str
    detected_at: str
    overdue_seconds: int
    alarm_code: Literal["transient_source_deletion_overdue"] = "transient_source_deletion_overdue"


@dataclass(frozen=True)
class StageReceipt:
    stage: DeletionStageName
    deleted_count: int
    verified_empty: bool
    completed_at: str
    receipt_id: str
    error_code: str | None = None


@dataclass(frozen=True)
class DeletionTombstone:
    attempt_id: str
    learner_id: str
    deleted_at: str
    job_id: str
    status: Literal["deleted"] = "deleted"


@dataclass(frozen=True)
class DeletionJob:
    job_id: str
    learner_id: str
    scope: DeletionScope
    target_attempt_ids: tuple[str, ...]
    consent_id: str | None
    status: DeletionJobStatus
    stage_receipts: tuple[StageReceipt, ...]
    alarm_triggered: bool
    alarm_codes: tuple[str, ...]
    created_at: str
    updated_at: str
    completed_at: str | None = None


class DeletionStorePort(Protocol):
    def purge_transient_sources(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        consent_id: str | None,
        source_references: tuple[str, ...],
        full_account: bool,
    ) -> int: ...

    def verify_transient_sources_empty(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        consent_id: str | None,
        source_references: tuple[str, ...],
        full_account: bool,
    ) -> bool: ...

    def purge_audio_and_rehearsals(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        full_account: bool,
    ) -> int: ...

    def verify_audio_and_rehearsals_empty(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        full_account: bool,
    ) -> bool: ...

    def purge_caches_and_prompts(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        full_account: bool,
    ) -> int: ...

    def verify_caches_and_prompts_empty(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        full_account: bool,
    ) -> bool: ...

    def purge_derived_learner_state(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        job_id: str,
        deleted_at: str,
        full_account: bool,
    ) -> int: ...

    def verify_derived_learner_state_empty(
        self,
        *,
        learner_id: str,
        attempt_ids: tuple[str, ...],
        full_account: bool,
    ) -> bool: ...


@dataclass(frozen=True)
class LearnerExportPackage:
    schema_version: Literal[1]
    learner_id: str
    exported_at: str
    consents: tuple[dict[str, Any], ...]
    attempts: tuple[dict[str, Any], ...]
    recurrences: tuple[dict[str, Any], ...]
    rehearsal_history: tuple[dict[str, Any], ...]
    comparisons: tuple[dict[str, Any], ...]
    skill_states: Mapping[str, dict[str, Any]]
    other_speaker_transcript_included: Literal[False] = False


def scan_overdue_transient_sources(
    records: Sequence[TransientTranscriptRecord],
    *,
    now: datetime,
) -> tuple[OverdueTransientAlarm, ...]:
    now_iso = _iso_millis(now, "Alarm scan timestamp")
    now_utc = _parse_iso(now_iso)
    alarms: list[OverdueTransientAlarm] = []
    for record in records:
        deadline = _parse_iso(record.delete_after)
        if now_utc > deadline:
            overdue_seconds = max(1, int((now_utc - deadline).total_seconds()))
            alarms.append(
                OverdueTransientAlarm(
                    transient_id=record.transient_id,
                    learner_id=record.learner_id,
                    source_reference=record.source_reference,
                    delete_after=record.delete_after,
                    detected_at=now_iso,
                    overdue_seconds=overdue_seconds,
                )
            )
    return tuple(alarms)


def stages_for_scope(scope: DeletionScope) -> tuple[DeletionStageName, ...]:
    if scope is DeletionScope.EXPIRED_TRANSIENT:
        return (DeletionStageName.TRANSIENT_SOURCE,)
    return (
        DeletionStageName.TRANSIENT_SOURCE,
        DeletionStageName.AUDIO_AND_REHEARSAL,
        DeletionStageName.CACHES_AND_PROMPTS,
        DeletionStageName.DERIVED_LEARNER_STATE,
    )


def build_learner_export(
    *,
    actor_learner_id: str,
    learner_id: str,
    recent_auth_verified: bool,
    exported_at: datetime,
    consents: Sequence[ConsentGrant],
    attempts: Sequence[Mapping[str, Any]],
    recurrences: Sequence[Mapping[str, Any]],
    rehearsal_history: Sequence[Mapping[str, Any]],
    comparisons: Sequence[Mapping[str, Any]],
    skill_states: Mapping[str, SkillState],
) -> LearnerExportPackage:
    _require_ulid(learner_id, "learner_id")
    if actor_learner_id != learner_id:
        raise PermissionError("export_access_denied")
    if not recent_auth_verified:
        raise PermissionError("recent_auth_required")
    export_iso = _iso_millis(exported_at, "Export timestamp")

    exported_attempts: list[dict[str, Any]] = []
    for attempt in attempts:
        if attempt.get("learner_id") != learner_id:
            continue
        if attempt.get("status") in {"deleted", "deletion_pending"}:
            continue
        copy_doc = deepcopy(dict(attempt))
        prohibited = find_prohibited_transcript_fields(copy_doc)
        if (
            prohibited
            or copy_doc.get("processing", {}).get("other_speaker_transcript_retained") is not False
        ):
            raise ValueError("prohibited_transcript_in_export")
        exported_attempts.append(copy_doc)

    exported_consents = tuple(
        {
            "consent_id": grant.consent_id,
            "capture_mode": grant.capture_mode.value,
            "policy_version": grant.policy_version,
            "all_participants_consented": grant.all_participants_consented,
            "attested_at": grant.attested_at,
            "revoked": grant.revoked,
            "revoked_at": grant.revoked_at,
            "version": grant.version,
        }
        for grant in consents
        if grant.learner_id == learner_id
    )
    exported_recurrences = tuple(
        deepcopy(dict(item)) for item in recurrences if item.get("learner_id") == learner_id
    )
    exported_rehearsals: list[dict[str, Any]] = []
    for rehearsal in rehearsal_history:
        if rehearsal.get("learner_id", learner_id) != learner_id:
            continue
        cleaned_rehearsal = {
            k: deepcopy(v)
            for k, v in rehearsal.items()
            if k not in {"raw_audio_bytes", "raw_audio_uri"}
        }
        exported_rehearsals.append(cleaned_rehearsal)

    exported_comparisons = tuple(
        deepcopy(dict(item))
        for item in comparisons
        if item.get("learner_id", learner_id) == learner_id
    )
    exported_skills = {
        skill: {
            "alpha": state.alpha,
            "beta": state.beta,
            "mastery": round(state.mastery, 4),
            "authentic_attempt_count": state.authentic_attempt_count,
        }
        for skill, state in sorted(skill_states.items())
    }

    return LearnerExportPackage(
        schema_version=1,
        learner_id=learner_id,
        exported_at=export_iso,
        consents=exported_consents,
        attempts=tuple(exported_attempts),
        recurrences=exported_recurrences,
        rehearsal_history=tuple(exported_rehearsals),
        comparisons=exported_comparisons,
        skill_states=exported_skills,
    )
