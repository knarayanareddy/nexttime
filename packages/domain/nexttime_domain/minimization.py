from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def validate_minimized_attempt(document: dict[str, Any]) -> list[str]:
    issues: set[str] = set()
    processing = document["processing"]
    consent = document["consent"]
    outcome = document["outcome"]
    status = document["status"]

    if processing["other_speaker_transcript_retained"] is not False:
        issues.add("other_speaker_transcript_retained")
    if consent["revoked"] and status != "deletion_pending":
        issues.add("revoked_consent_requires_deletion")
    if status == "confirmed":
        if not outcome["confirmed_by_learner"]:
            issues.add("confirmed_attempt_requires_learner_confirmation")
        if processing["redaction_status"] != "minimized":
            issues.add("confirmed_attempt_requires_minimization")

    recorded_at = datetime.fromisoformat(document["source"]["recorded_at"].replace("Z", "+00:00"))
    delete_after = datetime.fromisoformat(
        document["retention"]["transient_source_delete_after"].replace("Z", "+00:00")
    )
    if delete_after > recorded_at + timedelta(hours=24):
        issues.add("transient_retention_exceeds_24_hours")
    if delete_after < recorded_at:
        issues.add("transient_deletion_precedes_recording")
    return sorted(issues)


def find_prohibited_transcript_fields(value: object, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if "other_speaker_transcript" in key and key != "other_speaker_transcript_retained":
                findings.append(child_path)
            findings.extend(find_prohibited_transcript_fields(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(find_prohibited_transcript_fields(child, f"{path}[{index}]"))
    return findings
