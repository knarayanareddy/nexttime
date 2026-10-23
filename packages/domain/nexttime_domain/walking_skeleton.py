import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .minimization import validate_minimized_attempt
from .models import (
    AttemptEvaluationInput,
    CriterionEvidence,
    CriterionResult,
    Situation,
    SkillState,
)
from .progress import AuthenticProgressService
from .rehearsal import FixtureRehearsalService
from .review import AttemptReviewService, BreakdownReviewDecision
from .rubric import evaluate_authentic_attempt, rubric_from_mapping
from .skills import update_skill_from_authentic_attempt
from .transcripts import (
    CaptureMode,
    SourceTranscript,
    Speaker,
    TranscriptMinimizationService,
    TranscriptTurn,
)

ROOT = Path(__file__).resolve().parents[3]


def main() -> None:
    document: dict[str, Any] = json.loads(
        (ROOT / "examples" / "real-world-attempt.example.json").read_text()
    )
    rubric_document = json.loads((ROOT / "packages" / "rubrics" / "rubric_v1.json").read_text())
    issues = validate_minimized_attempt(document)
    if issues:
        raise ValueError(f"Invalid minimized fixture: {issues}")

    rubric = rubric_from_mapping(rubric_document)
    criteria = {
        item["criterion_id"]: CriterionEvidence(
            result=CriterionResult(item["result"]),
            evidence_present=bool(item["self_evidence_excerpt"]),
        )
        for item in document["outcome"]["criteria"]
    }
    outcome = evaluate_authentic_attempt(
        rubric,
        AttemptEvaluationInput(
            situation=Situation(document["context"]["situation"]),
            criteria=criteria,
            asr_confidence=document["processing"]["asr_confidence"],
            minimization_complete=document["processing"]["redaction_status"] == "minimized",
            interaction_ended=True,
        ),
    )
    initial_skill = SkillState()
    unchanged_skill = update_skill_from_authentic_attempt(
        initial_skill,
        CriterionResult.SATISFIED,
        confirmed_authentic_attempt=document["status"] == "confirmed",
    )
    minimization_service = TranscriptMinimizationService()
    source = SourceTranscript(
        reference="fixture-bee-recording-walking-001",
        learner_id=document["learner_id"],
        recorded_at=datetime(2026, 10, 22, 15, 0, tzinfo=UTC),
        capture_mode=CaptureMode.CONSENTED_CONVERSATION,
        situation=Situation.REPAIR,
        turns=(
            TranscriptTurn(
                Speaker.LEARNER,
                "Ik wil een afspraak voor de reparatie.",
                0,
                1200,
                0.91,
            ),
            TranscriptTurn(
                Speaker.OTHER,
                "Fixture other-speaker text that must be excluded.",
                1200,
                2400,
                0.93,
            ),
        ),
        consent_attested=True,
        all_participants_consented=True,
        revoked=False,
        retention_policy_present=True,
    )
    minimized = minimization_service.minimize(
        source, minimized_at=source.recorded_at + timedelta(hours=1)
    )
    duplicate_safe = minimization_service.minimize(source) is minimized
    reviewed_service = AttemptReviewService(document, rubric)
    reviewed_service.review_breakdown(
        actor_learner_id=document["learner_id"],
        expected_version=document["version"],
        breakdown_id=document["breakdowns"][0]["breakdown_id"],
        decision=BreakdownReviewDecision.CONFIRMED,
        reviewed_at=datetime(2026, 10, 22, 16, 0, tzinfo=UTC),
    )
    reviewed = reviewed_service.confirm_attempt(
        actor_learner_id=document["learner_id"],
        expected_version=document["version"] + 1,
        reviewed_at=datetime(2026, 10, 22, 16, 0, tzinfo=UTC),
    )
    rehearsal = FixtureRehearsalService(
        reviewed.document,
        actor_learner_id=document["learner_id"],
        breakdown_id=document["breakdowns"][0]["breakdown_id"],
        initial_skill_states=reviewed.skill_states,
    )
    rehearsal.mark_recurrence(
        actor_learner_id=document["learner_id"],
        occurrence_at=datetime(2026, 10, 24, 15, 0, tzinfo=UTC),
        marked_at=datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
    )
    rehearsal_schedule = rehearsal.schedule(
        actor_learner_id=document["learner_id"],
        learner_confirmed=True,
    )
    notification = rehearsal.notification_request(
        actor_learner_id=document["learner_id"],
        event_id="fixture-notification-1",
    )
    rehearsal.begin(actor_learner_id=document["learner_id"])
    rehearsal.submit_turn(
        actor_learner_id=document["learner_id"],
        event_id="fixture-turn-1",
        turn_id="t1",
        phrase_id="goal_repair_basic",
        confidence=0.91,
    )
    rehearsal_result = rehearsal.submit_turn(
        actor_learner_id=document["learner_id"],
        event_id="fixture-turn-2",
        turn_id="t2",
        phrase_id="confirm_time",
        confidence=0.90,
    )
    duplicate_notification = rehearsal.notification_request(
        actor_learner_id=document["learner_id"],
        event_id="fixture-notification-retry",
    )
    next_draft = json.loads(json.dumps(document))
    next_draft["attempt_id"] = "fixture-authentic-attempt-002"
    next_draft["source"]["bee_recording_reference"] = "fixture-bee-recording-walking-002"
    next_draft["source"]["recorded_at"] = "2026-10-24T15:00:00.000Z"
    next_draft["created_at"] = "2026-10-24T15:01:00.000Z"
    next_draft["updated_at"] = "2026-10-24T15:02:00.000Z"
    next_draft["retention"]["transient_source_delete_after"] = "2026-10-24T17:00:00.000Z"
    next_draft["version"] = 1
    next_draft["breakdowns"] = []
    next_draft["outcome"]["criteria"][1]["result"] = "satisfied"
    next_draft["outcome"]["criteria"][1]["self_evidence_excerpt"] = (
        "Ja, de afspraak is om drie uur."
    )
    next_draft["outcome"]["criteria"][1]["self_evidence_start_ms"] = 5000
    next_draft["outcome"]["criteria"][1]["self_evidence_end_ms"] = 6200
    next_review_service = AttemptReviewService(
        next_draft,
        rubric,
        initial_skill_states=reviewed.skill_states,
    )
    next_reviewed = next_review_service.confirm_attempt(
        actor_learner_id=document["learner_id"],
        expected_version=1,
        reviewed_at=datetime(2026, 10, 24, 16, 0, tzinfo=UTC),
    )
    progress = AuthenticProgressService(rubric)
    progress.record_review(reviewed)
    linked_progress = progress.record_review(next_reviewed)
    duplicate_progress = progress.record_review(next_reviewed)
    progress.record_rehearsal(
        {
            "rehearsal_id": "fixture-rehearsal-history-1",
            "recurrence_id": document["context"]["recurrence_id"],
            "target_function": rehearsal.scenario.target_function,
            "state": rehearsal_result.state.value,
            "completed_at": "2026-10-23T12:00:00Z",
        }
    )
    progress_snapshot = progress.snapshot()
    print(
        json.dumps(
            {
                "mode": "FIXTURE ONLY — no Bee, AWS, or iOS connection",
                "rubric_version": rubric.version,
                "minimization_status": minimized.status,
                "minimized_self_excerpts": len(minimized.self_excerpts),
                "other_speaker_transcript_retained": minimized.other_speaker_transcript_retained,
                "duplicate_source_safe": duplicate_safe,
                "outcome": outcome.status.value,
                "confirmation_required": outcome.confirmation_required,
                "reason_codes": outcome.reason_codes,
                "authentic_skill_update": unchanged_skill != initial_skill,
                "notice": (
                    "No authentic progress is inferred from this unconfirmed fixture attempt."
                ),
                "reviewed_status": reviewed.document["status"],
                "reviewed_outcome": reviewed.document["outcome"]["status"],
                "reviewed_skill_updates": reviewed.skill_updates,
                "reviewed_confirmation_beta": reviewed.skill_states["confirmation"].beta,
                "reviewed_notice": (
                    "A separate fixture-only reviewed copy updates the confirmed "
                    "breakdown once; no real learner or persistent learner store is involved."
                ),
                "next_authentic_attempt_status": next_reviewed.document["status"],
                "comparison_functional_progress": (
                    linked_progress.comparison.functional_progress.value
                    if linked_progress.comparison is not None
                    else None
                ),
                "comparison_confidence": (
                    linked_progress.comparison.comparison_confidence
                    if linked_progress.comparison is not None
                    else None
                ),
                "comparison_uncertainty_codes": (
                    linked_progress.comparison.uncertainty_codes
                    if linked_progress.comparison is not None
                    else ()
                ),
                "comparison_criterion_deltas": [
                    {
                        "criterion_id": delta.criterion_id,
                        "prior_result": delta.prior_result.value,
                        "next_result": delta.next_result.value,
                        "delta": delta.delta,
                    }
                    for delta in (
                        linked_progress.comparison.criterion_deltas
                        if linked_progress.comparison is not None
                        else ()
                    )
                ],
                "comparison_causal_claim": False,
                "progress_duplicate_safe": duplicate_progress.duplicate,
                "progress_authentic_attempt_count": len(progress_snapshot.authentic_attempts),
                "progress_rehearsal_history_count": len(progress_snapshot.rehearsal_history),
                "progress_skill_states_unchanged_by_replay": (
                    duplicate_progress.skill_states == next_reviewed.skill_states
                ),
                "rehearsal_state": rehearsal_result.state.value,
                "rehearsal_target": rehearsal.scenario.target_function,
                "rehearsal_estimated_seconds": rehearsal.scenario.estimated_seconds,
                "rehearsal_scheduled_at": (
                    rehearsal_schedule.scheduled_at.isoformat()
                    if rehearsal_schedule.scheduled_at is not None
                    else None
                ),
                "rehearsal_committed_turns": rehearsal_result.committed_turn_count,
                "rehearsal_notification_status": notification.status if notification else None,
                "rehearsal_notification_duplicate_safe": (
                    duplicate_notification.duplicate if duplicate_notification else False
                ),
                "authentic_skills_unchanged_by_rehearsal": (
                    rehearsal.authentic_skill_states == reviewed.skill_states
                ),
                "rehearsal_audio_generated": False,
                "rehearsal_notice": (
                    "Deterministic text/template, reviewed fixture attempts, and fixture "
                    "notification requests only; no Polly, Bedrock, iOS, actual notification, "
                    "real Bee attempt, causal learning claim, or persistent progress store "
                    "is claimed."
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
