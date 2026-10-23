from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from .minimization import validate_minimized_attempt
from .models import Situation, SkillState


class RehearsalState(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class NotificationBehavior(StrEnum):
    LOCAL_NOTIFICATION = "local_notification"
    OPEN_TEMPLATE_ONLY = "open_template_only"


class PracticeMode(StrEnum):
    SPEECH = "speech"
    TEXT = "text"
    TAP = "tap"


@dataclass(frozen=True)
class Recurrence:
    learner_id: str
    situation: Situation
    occurrence_at: datetime
    marked_at: datetime
    source: Literal["learner_marked"] = "learner_marked"


@dataclass(frozen=True)
class ScenarioTurn:
    turn_id: str
    counterpart_text_nl: str
    counterpart_text_en: str
    expected_functions: tuple[str, ...]
    accepted_phrase_ids: tuple[str, ...]
    repair_if_missing: str


@dataclass(frozen=True)
class SupportPhrase:
    phrase_id: str
    text_nl: str
    text_en: str


@dataclass(frozen=True)
class RehearsalScenario:
    scenario_version: int
    situation: Situation
    target_function: str
    estimated_seconds: int
    counterpart_persona: str
    voice_id: str
    speaking_rate: str
    turns: tuple[ScenarioTurn, ...]
    success_criteria: tuple[str, ...]
    support_phrases: tuple[SupportPhrase, ...]
    safety_exclusions: tuple[str, ...]
    template_version: str
    model_generated: bool = False
    human_content_review_required: bool = True


@dataclass(frozen=True)
class ScheduleDecision:
    selection_required: bool
    quiet_hours: bool
    scheduled_at: datetime | None
    notification_behavior: NotificationBehavior | None


@dataclass(frozen=True)
class NotificationRequest:
    rehearsal_id: str
    event_id: str
    status: Literal["fixture_request_not_sent"]
    duplicate: bool = False


@dataclass(frozen=True)
class RehearsalTurnResult:
    state: RehearsalState
    current_turn_id: str | None
    mapped_function: str | None
    support: Literal["repair", "easier_alternative"] | None
    committed_turn_count: int
    non_speaking_practice: bool
    duplicate: bool = False


_GOAL_PHRASES = {
    Situation.REPAIR: (
        "goal_repair_basic",
        "Ik wil een afspraak voor de reparatie.",
        "I would like to make a repair appointment.",
        "De reparatiewerkplek is vrijdag om tien uur.",
        "appointment_confirmed",
        "property_scheduler",
    ),
    Situation.SCHOOL_ADMIN: (
        "goal_school_basic",
        "Ik wil een afspraak met de schooladministratie.",
        "I would like an appointment with the school office.",
        "De schooladministratie kan maandag om negen uur.",
        "appointment_confirmed",
        "school_scheduler",
    ),
    Situation.SHIFT: (
        "goal_shift_basic",
        "Ik wil mijn dienst en de volgende stap controleren.",
        "I want to check my shift and the next step.",
        "Uw volgende dienst begint dinsdag om acht uur.",
        "next_step_confirmed",
        "shift_scheduler",
    ),
}

_EXPECTED_FUNCTIONS = {
    "goal_stated",
    "appointment_confirmed",
    "next_step_confirmed",
    "repair_requested",
    "independent_completion",
}
_REQUIRED_EXCLUSIONS = {"medical", "legal", "payment"}


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value


def _quiet_hours(value: datetime) -> bool:
    return value.hour >= 21 or value.hour < 8


def validate_rehearsal_scenario(scenario: RehearsalScenario) -> list[str]:
    issues: set[str] = set()
    if scenario.scenario_version != 1:
        issues.add("unsupported_scenario_version")
    if not isinstance(scenario.situation, Situation):
        issues.add("unreviewed_situation")
    if scenario.target_function != "confirmation":
        issues.add("unreviewed_target_function")
    if not 1 <= scenario.estimated_seconds <= 120:
        issues.add("scenario_duration_out_of_bounds")
    if scenario.voice_id != "configured-dutch-voice" or scenario.speaking_rate != "medium":
        issues.add("unregistered_voice")
    if not 1 <= len(scenario.turns) <= 6:
        issues.add("scenario_turn_count_out_of_bounds")
    if len({turn.turn_id for turn in scenario.turns}) != len(scenario.turns):
        issues.add("duplicate_turn_id")
    if len(scenario.support_phrases) > 2:
        issues.add("too_many_support_phrases")
    if not _REQUIRED_EXCLUSIONS.issubset(scenario.safety_exclusions):
        issues.add("missing_safety_exclusion")
    for turn in scenario.turns:
        if not turn.turn_id.strip() or not turn.repair_if_missing.strip():
            issues.add("missing_turn_contract")
        if not turn.expected_functions or not set(turn.expected_functions).issubset(
            _EXPECTED_FUNCTIONS
        ):
            issues.add("unregistered_expected_function")
        if not turn.accepted_phrase_ids:
            issues.add("missing_accepted_phrase")
        for text in (
            turn.counterpart_text_nl,
            turn.counterpart_text_en,
        ):
            if not text.strip() or len(text) > 280:
                issues.add("counterpart_text_out_of_bounds")
    return sorted(issues)


def _scenario(situation: Situation) -> RehearsalScenario:
    (
        goal_id,
        goal_nl,
        goal_en,
        proposal_nl,
        confirmation_criterion,
        persona,
    ) = _GOAL_PHRASES[situation]
    scenario = RehearsalScenario(
        scenario_version=1,
        situation=situation,
        target_function="confirmation",
        estimated_seconds=90,
        counterpart_persona=persona,
        voice_id="configured-dutch-voice",
        speaking_rate="medium",
        turns=(
            ScenarioTurn(
                turn_id="t1",
                counterpart_text_nl="Vertel alstublieft wat u nodig heeft.",
                counterpart_text_en="Please explain what you need.",
                expected_functions=("goal_stated",),
                accepted_phrase_ids=(goal_id,),
                repair_if_missing="goal_statement",
            ),
            ScenarioTurn(
                turn_id="t2",
                counterpart_text_nl=f"{proposal_nl} Kunt u dat bevestigen?",
                counterpart_text_en="Please confirm the proposed date and time.",
                expected_functions=(confirmation_criterion,),
                accepted_phrase_ids=("confirm_time",),
                repair_if_missing="confirmation",
            ),
        ),
        success_criteria=("goal_stated", confirmation_criterion),
        support_phrases=(
            SupportPhrase(
                phrase_id="confirm_time",
                text_nl="Ja, ik bevestig de datum en de tijd.",
                text_en="Yes, I confirm the date and time.",
            ),
        ),
        safety_exclusions=("medical", "legal", "payment"),
        template_version="fixture-confirmation-v1",
    )
    issues = validate_rehearsal_scenario(scenario)
    if issues:
        raise ValueError(f"Invalid fixture rehearsal template: {issues}")
    return scenario


def build_fixture_rehearsal_scenario(
    document: dict[str, Any],
    *,
    actor_learner_id: str,
    breakdown_id: str,
) -> RehearsalScenario:
    issues = validate_minimized_attempt(document)
    if issues:
        raise ValueError(f"Invalid minimized attempt: {issues}")
    if actor_learner_id != document["learner_id"]:
        raise PermissionError("attempt_access_denied")
    if document["status"] != "confirmed" or document["consent"]["revoked"]:
        raise ValueError("A confirmed, consented attempt is required")
    breakdown = next(
        (item for item in document["breakdowns"] if item["breakdown_id"] == breakdown_id),
        None,
    )
    if breakdown is None:
        raise ValueError("breakdown_not_found")
    if breakdown["review_status"] != "confirmed":
        raise ValueError("rejected_or_unreviewed_breakdown_cannot_target_rehearsal")
    if breakdown["code"] != "confirmation_missing":
        raise ValueError("reviewed_fixture_template_unavailable")
    return _scenario(Situation(document["context"]["situation"]))


class FixtureRehearsalService:
    """One synthetic recurrence/rehearsal; no audio, notification adapter, or durable store."""

    def __init__(
        self,
        document: dict[str, Any],
        *,
        actor_learner_id: str,
        breakdown_id: str,
        initial_skill_states: Mapping[str, SkillState] | None = None,
    ) -> None:
        self._learner_id = document["learner_id"]
        self.scenario = build_fixture_rehearsal_scenario(
            document,
            actor_learner_id=actor_learner_id,
            breakdown_id=breakdown_id,
        )
        self._initial_skills = deepcopy(dict(initial_skill_states or {}))
        self._state = RehearsalState.DRAFT
        self._recurrence: Recurrence | None = None
        self._scheduled_at: datetime | None = None
        self._notification_behavior: NotificationBehavior | None = None
        self._notification: NotificationRequest | None = None
        self._turn_index = 0
        self._support_count = 0
        self._committed_turns = 0
        self._non_speaking = False
        self._events: dict[str, tuple[tuple[Any, ...], RehearsalTurnResult]] = {}

    def _actor(self, actor_learner_id: str) -> None:
        if actor_learner_id != self._learner_id:
            raise PermissionError("attempt_access_denied")

    @property
    def state(self) -> RehearsalState:
        return self._state

    @property
    def authentic_skill_states(self) -> dict[str, SkillState]:
        return deepcopy(self._initial_skills)

    def mark_recurrence(
        self,
        *,
        actor_learner_id: str,
        occurrence_at: datetime,
        marked_at: datetime,
    ) -> Recurrence:
        self._actor(actor_learner_id)
        occurrence = _utc(occurrence_at, "Occurrence")
        marked = _utc(marked_at, "Recurrence timestamp")
        if occurrence <= marked:
            raise ValueError("Occurrence must be after it is marked")
        recurrence = Recurrence(
            learner_id=self._learner_id,
            situation=self.scenario.situation,
            occurrence_at=occurrence,
            marked_at=marked,
        )
        if self._recurrence is not None:
            if self._recurrence != recurrence:
                raise ValueError("Recurrence is already committed")
            return self._recurrence
        self._recurrence = recurrence
        return recurrence

    def schedule(
        self,
        *,
        actor_learner_id: str,
        learner_confirmed: bool,
        selected_at: datetime | None = None,
        notification_behavior: NotificationBehavior = NotificationBehavior.LOCAL_NOTIFICATION,
    ) -> ScheduleDecision:
        self._actor(actor_learner_id)
        if self._recurrence is None:
            raise ValueError("A learner-marked recurrence is required")
        default_at = self._recurrence.occurrence_at - timedelta(minutes=60)
        chosen = _utc(selected_at or default_at, "Rehearsal time")
        quiet = _quiet_hours(chosen)
        if self._state is not RehearsalState.DRAFT:
            if (
                chosen == self._scheduled_at
                and notification_behavior == self._notification_behavior
            ):
                return ScheduleDecision(False, quiet, chosen, notification_behavior)
            raise ValueError("Rehearsal is already scheduled")
        if not learner_confirmed or (
            quiet and notification_behavior is NotificationBehavior.LOCAL_NOTIFICATION
        ):
            return ScheduleDecision(True, quiet, None, None)
        if chosen < self._recurrence.marked_at or chosen > self._recurrence.occurrence_at:
            raise ValueError("Rehearsal time must be between marking and occurrence")
        self._scheduled_at = chosen
        self._notification_behavior = notification_behavior
        self._state = RehearsalState.SCHEDULED
        return ScheduleDecision(False, quiet, chosen, notification_behavior)

    def notification_request(
        self,
        *,
        actor_learner_id: str,
        event_id: str,
    ) -> NotificationRequest | None:
        self._actor(actor_learner_id)
        if not event_id.strip():
            raise ValueError("Notification event ID is required")
        if self._notification is not None:
            return NotificationRequest(
                rehearsal_id=self._notification.rehearsal_id,
                event_id=self._notification.event_id,
                status=self._notification.status,
                duplicate=True,
            )
        if self._state is not RehearsalState.SCHEDULED:
            raise ValueError("Rehearsal is not scheduled")
        if self._notification_behavior is not NotificationBehavior.LOCAL_NOTIFICATION:
            return None
        self._notification = NotificationRequest(
            rehearsal_id=f"fixture-rehearsal-{self.scenario.situation.value}",
            event_id=event_id,
            status="fixture_request_not_sent",
        )
        return self._notification

    def begin(self, *, actor_learner_id: str) -> None:
        self._actor(actor_learner_id)
        if self._state is not RehearsalState.SCHEDULED:
            raise ValueError("Rehearsal is not ready to begin")
        self._state = RehearsalState.IN_PROGRESS

    def submit_turn(
        self,
        *,
        actor_learner_id: str,
        event_id: str,
        turn_id: str,
        phrase_id: str | None,
        confidence: float,
        mode: PracticeMode = PracticeMode.SPEECH,
    ) -> RehearsalTurnResult:
        self._actor(actor_learner_id)
        if not event_id.strip() or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("Invalid turn event")
        fingerprint = (turn_id, phrase_id, confidence, mode)
        existing = self._events.get(event_id)
        if existing is not None:
            if existing[0] != fingerprint:
                raise ValueError("turn_event_replay_conflict")
            prior = existing[1]
            return RehearsalTurnResult(
                state=self._state,
                current_turn_id=(
                    self.scenario.turns[self._turn_index].turn_id
                    if self._state is RehearsalState.IN_PROGRESS
                    else None
                ),
                mapped_function=prior.mapped_function,
                support=prior.support,
                committed_turn_count=self._committed_turns,
                non_speaking_practice=self._non_speaking,
                duplicate=True,
            )
        if self._state is not RehearsalState.IN_PROGRESS:
            raise ValueError("Rehearsal is not in progress")
        if self._turn_index >= len(self.scenario.turns):
            raise ValueError("Rehearsal has no remaining turns")
        turn = self.scenario.turns[self._turn_index]
        if turn.turn_id != turn_id:
            raise ValueError("stale_rehearsal_turn")
        accepted = phrase_id in turn.accepted_phrase_ids and confidence >= 0.75
        support: Literal["repair", "easier_alternative"] | None = None
        mapped: str | None = None
        if accepted:
            mapped = turn.expected_functions[0]
            self._committed_turns += 1
            self._turn_index += 1
            self._non_speaking = self._non_speaking or mode is not PracticeMode.SPEECH
            if self._turn_index == len(self.scenario.turns):
                self._state = RehearsalState.COMPLETED
        else:
            self._support_count += 1
            if self._support_count == 1:
                support = "repair"
            elif self._support_count == 2:
                support = "easier_alternative"
            else:
                self._state = RehearsalState.ABANDONED
        result = RehearsalTurnResult(
            state=self._state,
            current_turn_id=(
                self.scenario.turns[self._turn_index].turn_id
                if self._state is RehearsalState.IN_PROGRESS
                else None
            ),
            mapped_function=mapped,
            support=support,
            committed_turn_count=self._committed_turns,
            non_speaking_practice=self._non_speaking,
        )
        self._events[event_id] = (fingerprint, result)
        return result
