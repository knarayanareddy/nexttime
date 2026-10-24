from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from nexttime_domain import NotificationBehavior, Situation

CalendarProviderKind = Literal["icalendar", "google_calendar"]
CalendarProvenance = Literal["fixture", "live"]
CandidateConfirmationState = Literal["awaiting_learner_confirmation", "confirmed", "dismissed"]


class CalendarAdapterError(RuntimeError):
    """Typed error raised by iCalendar or Google Calendar adapters without leaking PII/tokens."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class TransientCalendarEvent:
    """Ephemeral in-memory calendar event; summary/description MUST NEVER be persisted."""

    raw_uid: str
    provider: CalendarProviderKind
    summary: str
    description: str
    starts_at: datetime | None
    ends_at: datetime | None
    time_zone: str
    cancelled: bool = False
    all_day: bool = False


@dataclass(frozen=True)
class CalendarSyncBatch:
    items: tuple[TransientCalendarEvent, ...]
    next_sync_token: str | None
    provenance: CalendarProvenance = "fixture"


class CalendarSourcePort(Protocol):
    async def fetch_events(
        self,
        *,
        sync_token: str | None,
        window_start: datetime,
        window_end: datetime,
    ) -> CalendarSyncBatch: ...


@dataclass(frozen=True)
class CalendarRecurrenceCandidate:
    """Minimized user-confirmable recurrence candidate with zero raw event title retention."""

    candidate_id: str
    learner_id: str
    provider: CalendarProviderKind
    calendar_event_reference: str
    situation: Situation
    matched_attempt_id: str
    matched_breakdown_id: str
    scheduled_for: str
    proposed_rehearsal_at: str
    time_zone: str
    quiet_hours: bool
    confirmation_state: CandidateConfirmationState
    provenance: CalendarProvenance
    created_at: str


@dataclass(frozen=True)
class PreMeetingRehearsalCard:
    """Minimized NT-NextMoment / NT-RehearsalReady card queued only after learner confirmation."""

    card_id: str
    learner_id: str
    candidate_id: str
    recurrence_id: str
    attempt_id: str
    breakdown_id: str
    situation: Situation
    target_function: str
    scheduled_for: str
    rehearsal_at: str
    time_zone: str
    estimated_seconds: int
    quiet_hours: bool
    notification_behavior: NotificationBehavior
    event_source: Literal["explicit_calendar_link"]
    calendar_event_reference: str
    status: Literal["queued", "cancelled"]
    provenance: CalendarProvenance


@dataclass(frozen=True)
class CalendarSyncSummary:
    learner_id: str
    provider: CalendarProviderKind
    candidates: tuple[CalendarRecurrenceCandidate, ...]
    excluded_sensitive_count: int
    ignored_unrelated_count: int
    cancelled_count: int
    next_sync_token: str | None
    provenance: CalendarProvenance
