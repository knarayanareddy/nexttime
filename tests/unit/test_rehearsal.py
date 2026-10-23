import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from nexttime_domain import (
    AttemptReviewService,
    BreakdownReviewDecision,
    FixtureRehearsalService,
    NotificationBehavior,
    PracticeMode,
    RehearsalState,
    SkillState,
    rubric_from_mapping,
    validate_rehearsal_scenario,
)

ROOT = Path(__file__).resolve().parents[2]
LEARNER = "01000000000000000000000002"
BREAKDOWN = "01000000000000000000000005"
REVIEWED_AT = datetime(2026, 10, 22, 16, 0, tzinfo=UTC)
MARKED_AT = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
OCCURRENCE = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)


@pytest.fixture
def attempt_document() -> dict[str, Any]:
    return json.loads((ROOT / "examples" / "real-world-attempt.example.json").read_text())


@pytest.fixture
def confirmed_document(attempt_document: dict[str, Any]) -> dict[str, Any]:
    rubric = rubric_from_mapping(
        json.loads((ROOT / "packages" / "rubrics" / "rubric_v1.json").read_text())
    )
    service = AttemptReviewService(attempt_document, rubric)
    service.review_breakdown(
        actor_learner_id=LEARNER,
        expected_version=1,
        breakdown_id=BREAKDOWN,
        decision=BreakdownReviewDecision.CONFIRMED,
        reviewed_at=REVIEWED_AT,
    )
    return service.confirm_attempt(
        actor_learner_id=LEARNER,
        expected_version=2,
        reviewed_at=REVIEWED_AT,
    ).document


def rehearsal(
    document: dict[str, Any],
    *,
    skills: dict[str, SkillState] | None = None,
) -> FixtureRehearsalService:
    return FixtureRehearsalService(
        document,
        actor_learner_id=LEARNER,
        breakdown_id=BREAKDOWN,
        initial_skill_states=skills,
    )


def scheduled(document: dict[str, Any]) -> FixtureRehearsalService:
    service = rehearsal(document)
    service.mark_recurrence(
        actor_learner_id=LEARNER,
        occurrence_at=OCCURRENCE,
        marked_at=MARKED_AT,
    )
    decision = service.schedule(actor_learner_id=LEARNER, learner_confirmed=True)
    assert decision.scheduled_at == OCCURRENCE - timedelta(minutes=60)
    return service


def test_only_a_confirmed_breakdown_can_target_rehearsal(
    attempt_document: dict[str, Any], confirmed_document: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="confirmed, consented"):
        rehearsal(attempt_document)
    with pytest.raises(PermissionError, match="access_denied"):
        FixtureRehearsalService(
            confirmed_document,
            actor_learner_id="other-learner",
            breakdown_id=BREAKDOWN,
        )
    with pytest.raises(ValueError, match="breakdown_not_found"):
        FixtureRehearsalService(
            confirmed_document,
            actor_learner_id=LEARNER,
            breakdown_id="missing",
        )
    rejected = deepcopy(confirmed_document)
    rejected["breakdowns"][0]["review_status"] = "rejected"
    with pytest.raises(ValueError, match="cannot_target_rehearsal"):
        rehearsal(rejected)
    unsupported = deepcopy(confirmed_document)
    unsupported["breakdowns"][0]["code"] = "closure"
    with pytest.raises(ValueError, match="template_unavailable"):
        rehearsal(unsupported)


def test_deterministic_template_is_bounded_and_contains_no_attempt_excerpts(
    confirmed_document: dict[str, Any],
) -> None:
    first = rehearsal(confirmed_document).scenario
    second = rehearsal(confirmed_document).scenario
    assert first == second
    assert validate_rehearsal_scenario(first) == []
    assert first.target_function == "confirmation"
    assert first.estimated_seconds == 90
    assert len(first.turns) == 2
    assert first.model_generated is False
    assert first.human_content_review_required is True
    serialized = json.dumps(asdict(first))
    assert "Ja, dank u" not in serialized
    assert "self_evidence" not in serialized
    assert "bystander" not in serialized
    assert "scenario_duration_out_of_bounds" in validate_rehearsal_scenario(
        replace(first, estimated_seconds=121)
    )
    assert "unregistered_voice" in validate_rehearsal_scenario(replace(first, voice_id="unknown"))
    assert "scenario_turn_count_out_of_bounds" in validate_rehearsal_scenario(
        replace(first, turns=())
    )
    assert "unreviewed_target_function" in validate_rehearsal_scenario(
        replace(first, target_function="other")
    )


def test_recurrence_and_quiet_hours_require_explicit_selection(
    confirmed_document: dict[str, Any],
) -> None:
    service = rehearsal(confirmed_document)
    with pytest.raises(ValueError, match="recurrence is required"):
        service.schedule(actor_learner_id=LEARNER, learner_confirmed=True)
    quiet_occurrence = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)
    recurrence = service.mark_recurrence(
        actor_learner_id=LEARNER,
        occurrence_at=quiet_occurrence,
        marked_at=MARKED_AT,
    )
    assert recurrence.source == "learner_marked"
    assert (
        service.mark_recurrence(
            actor_learner_id=LEARNER,
            occurrence_at=quiet_occurrence,
            marked_at=MARKED_AT,
        )
        == recurrence
    )
    unconfirmed = service.schedule(actor_learner_id=LEARNER, learner_confirmed=False)
    assert unconfirmed.selection_required is True
    quiet_default = service.schedule(actor_learner_id=LEARNER, learner_confirmed=True)
    assert quiet_default.selection_required is True
    assert quiet_default.quiet_hours is True
    assert service.state is RehearsalState.DRAFT
    selected = service.schedule(
        actor_learner_id=LEARNER,
        learner_confirmed=True,
        selected_at=datetime(2026, 10, 24, 20, 0, tzinfo=UTC),
    )
    assert selected.selection_required is False
    assert (
        service.schedule(
            actor_learner_id=LEARNER,
            learner_confirmed=True,
            selected_at=datetime(2026, 10, 24, 20, 0, tzinfo=UTC),
        )
        == selected
    )
    with pytest.raises(ValueError, match="already scheduled"):
        service.schedule(
            actor_learner_id=LEARNER,
            learner_confirmed=True,
            selected_at=datetime(2026, 10, 24, 19, 0, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="after it is marked"):
        rehearsal(confirmed_document).mark_recurrence(
            actor_learner_id=LEARNER,
            occurrence_at=MARKED_AT,
            marked_at=MARKED_AT,
        )


def test_quiet_hours_can_use_explicit_template_only_choice(
    confirmed_document: dict[str, Any],
) -> None:
    service = rehearsal(confirmed_document)
    occurrence = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)
    service.mark_recurrence(
        actor_learner_id=LEARNER,
        occurrence_at=occurrence,
        marked_at=MARKED_AT,
    )
    decision = service.schedule(
        actor_learner_id=LEARNER,
        learner_confirmed=True,
        notification_behavior=NotificationBehavior.OPEN_TEMPLATE_ONLY,
    )
    assert decision.quiet_hours is True
    assert decision.selection_required is False
    assert service.notification_request(actor_learner_id=LEARNER, event_id="n1") is None


def test_notification_and_turn_events_are_idempotent_even_after_completion(
    confirmed_document: dict[str, Any],
) -> None:
    service = scheduled(confirmed_document)
    first_notification = service.notification_request(actor_learner_id=LEARNER, event_id="n1")
    assert first_notification is not None
    assert first_notification.status == "fixture_request_not_sent"
    service.begin(actor_learner_id=LEARNER)
    duplicate_notification = service.notification_request(actor_learner_id=LEARNER, event_id="n2")
    assert duplicate_notification is not None
    assert duplicate_notification.duplicate is True
    assert duplicate_notification.rehearsal_id == first_notification.rehearsal_id

    first = service.submit_turn(
        actor_learner_id=LEARNER,
        event_id="turn-event-1",
        turn_id="t1",
        phrase_id="goal_repair_basic",
        confidence=0.91,
    )
    assert first.committed_turn_count == 1
    assert first.current_turn_id == "t2"
    duplicate = service.submit_turn(
        actor_learner_id=LEARNER,
        event_id="turn-event-1",
        turn_id="t1",
        phrase_id="goal_repair_basic",
        confidence=0.91,
    )
    assert duplicate.duplicate is True
    assert duplicate.committed_turn_count == 1
    with pytest.raises(ValueError, match="replay_conflict"):
        service.submit_turn(
            actor_learner_id=LEARNER,
            event_id="turn-event-1",
            turn_id="t1",
            phrase_id="different",
            confidence=0.91,
        )
    with pytest.raises(ValueError, match="stale_rehearsal_turn"):
        service.submit_turn(
            actor_learner_id=LEARNER,
            event_id="wrong-turn",
            turn_id="t1",
            phrase_id="goal_repair_basic",
            confidence=0.91,
        )
    completed = service.submit_turn(
        actor_learner_id=LEARNER,
        event_id="turn-event-2",
        turn_id="t2",
        phrase_id="confirm_time",
        confidence=0.90,
    )
    assert completed.state is RehearsalState.COMPLETED
    assert completed.mapped_function == "appointment_confirmed"
    final_duplicate = service.submit_turn(
        actor_learner_id=LEARNER,
        event_id="turn-event-2",
        turn_id="t2",
        phrase_id="confirm_time",
        confidence=0.90,
    )
    assert final_duplicate.duplicate is True
    assert final_duplicate.committed_turn_count == 2


def test_ambiguous_input_offers_bounded_support_without_authentic_update(
    confirmed_document: dict[str, Any],
) -> None:
    skills = {"confirmation": SkillState(alpha=1.0, beta=2.0, authentic_attempt_count=1)}
    service = rehearsal(confirmed_document, skills=skills)
    service.mark_recurrence(
        actor_learner_id=LEARNER,
        occurrence_at=OCCURRENCE,
        marked_at=MARKED_AT,
    )
    service.schedule(actor_learner_id=LEARNER, learner_confirmed=True)
    service.begin(actor_learner_id=LEARNER)
    repair = service.submit_turn(
        actor_learner_id=LEARNER,
        event_id="a1",
        turn_id="t1",
        phrase_id="goal_repair_basic",
        confidence=0.74,
    )
    assert repair.support == "repair"
    assert repair.committed_turn_count == 0
    easier = service.submit_turn(
        actor_learner_id=LEARNER,
        event_id="a2",
        turn_id="t1",
        phrase_id="unknown",
        confidence=0.90,
    )
    assert easier.support == "easier_alternative"
    abandoned = service.submit_turn(
        actor_learner_id=LEARNER,
        event_id="a3",
        turn_id="t1",
        phrase_id=None,
        confidence=0.90,
    )
    assert abandoned.state is RehearsalState.ABANDONED
    duplicate = service.submit_turn(
        actor_learner_id=LEARNER,
        event_id="a3",
        turn_id="t1",
        phrase_id=None,
        confidence=0.90,
    )
    assert duplicate.duplicate is True
    assert service.authentic_skill_states == skills


def test_text_and_tap_completion_is_labeled_and_does_not_change_mastery(
    confirmed_document: dict[str, Any],
) -> None:
    skills = {"confirmation": SkillState(alpha=1.0, beta=2.0, authentic_attempt_count=1)}
    service = rehearsal(confirmed_document, skills=skills)
    service.mark_recurrence(
        actor_learner_id=LEARNER,
        occurrence_at=OCCURRENCE,
        marked_at=MARKED_AT,
    )
    service.schedule(actor_learner_id=LEARNER, learner_confirmed=True)
    with pytest.raises(ValueError, match="event ID"):
        service.notification_request(actor_learner_id=LEARNER, event_id="")
    service.begin(actor_learner_id=LEARNER)
    service.submit_turn(
        actor_learner_id=LEARNER,
        event_id="tap-1",
        turn_id="t1",
        phrase_id="goal_repair_basic",
        confidence=1.0,
        mode=PracticeMode.TAP,
    )
    result = service.submit_turn(
        actor_learner_id=LEARNER,
        event_id="text-2",
        turn_id="t2",
        phrase_id="confirm_time",
        confidence=1.0,
        mode=PracticeMode.TEXT,
    )
    assert result.state is RehearsalState.COMPLETED
    assert result.non_speaking_practice is True
    assert service.authentic_skill_states == skills
    with pytest.raises(ValueError, match="not in progress"):
        service.submit_turn(
            actor_learner_id=LEARNER,
            event_id="new-after-complete",
            turn_id="t2",
            phrase_id="confirm_time",
            confidence=1.0,
        )
