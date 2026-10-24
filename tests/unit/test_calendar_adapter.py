from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from nexttime_calendar import (
    REQUIRED_GOOGLE_CALENDAR_SCOPE,
    CalendarAdapterError,
    CalendarSyncService,
    GoogleCalendarAdapter,
    GoogleOAuthGrant,
    ICalendarFeedAdapter,
    classify_calendar_event_situation,
    opaque_calendar_reference,
    parse_ics_events,
)
from nexttime_domain import NotificationBehavior, Situation

LEARNER_ID = "01000000000000000000000002"
OTHER_LEARNER = "01000000000000000000000099"


def _confirmed_attempt_for(
    situation: Situation,
    *,
    attempt_id: str = "01000000000000000000000001",
    breakdown_id: str = "01000000000000000000000005",
    learner_id: str = LEARNER_ID,
) -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "learner_id": learner_id,
        "status": "confirmed",
        "consent": {"revoked": False},
        "source": {"recorded_at": "2026-10-22T14:30:00Z"},
        "context": {"situation": situation.value, "recurrence_id": None},
        "breakdowns": [
            {
                "breakdown_id": breakdown_id,
                "code": "confirmation_missing",
                "review_status": "confirmed",
            }
        ],
    }


def test_classifier_matches_only_supported_dutch_situations_and_excludes_sensitive() -> None:
    assert (
        classify_calendar_event_situation("Reparatieafspraak cv-ketel met Jan Jansen")
        is Situation.REPAIR
    )
    assert (
        classify_calendar_event_situation("Oudergesprek op basisschool") is Situation.SCHOOL_ADMIN
    )
    assert (
        classify_calendar_event_situation("Bespreking werkrooster volgende week") is Situation.SHIFT
    )
    assert classify_calendar_event_situation("   ") == "unrelated"
    assert classify_calendar_event_situation("Koffie met buurman") == "unrelated"
    # Ambiguous multi-situation event fails closed to unrelated
    assert (
        classify_calendar_event_situation("Oudergesprek en reparatieafspraak tegelijk")
        == "unrelated"
    )
    # Sensitive domains fail closed to excluded_sensitive (SEC-NT-006)
    for sensitive_title in (
        "Afspraak huisarts in ziekenhuis",
        "Gesprek met advocaat over verblijfsvergunning",
        "Waarschuwingsgesprek en ontslag HR",
        "Schuldsanering en incasso",
    ):
        assert classify_calendar_event_situation(sensitive_title) == "excluded_sensitive"

    with pytest.raises(ValueError, match="UID cannot be empty"):
        opaque_calendar_reference(learner_id=LEARNER_ID, provider="icalendar", raw_uid="  ")


def test_ics_parser_handles_unfolding_escapes_timezones_and_errors() -> None:
    raw_ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "X-WR-TIMEZONE:Europe/Amsterdam\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:evt-repair-001@example.nl\r\n"
        "SUMMARY:Reparatieafspraak cv-\r\n"
        " ketel\\, woningcorporatie\r\n"
        "DESCRIPTION:Monteur komt langs\\nNeem sleutel mee\r\n"
        "DTSTART;TZID=Europe/Amsterdam:20261026T140000\r\n"
        "DTEND;TZID=Europe/Amsterdam:20261026T143000\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:evt-allday-002\r\n"
        "SUMMARY:Koningsdag\r\n"
        "DTSTART;VALUE=DATE:20261027\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:evt-utc-cancelled-003\r\n"
        "STATUS:CANCELLED\r\n"
        "SUMMARY:Werkrooster overleg\r\n"
        "DTSTART:20261028T090000Z\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    events = parse_ics_events(
        raw_ics,
        window_start=datetime(2026, 10, 24, 10, 0, tzinfo=UTC),
        window_end=datetime(2026, 11, 5, 10, 0, tzinfo=UTC),
    )
    assert len(events) == 3
    assert events[0].summary == "Reparatieafspraak cv-ketel, woningcorporatie"
    assert "Neem sleutel mee" in events[0].description
    assert events[0].starts_at == datetime(2026, 10, 26, 13, 0, tzinfo=UTC)
    assert events[1].all_day is True
    assert events[2].cancelled is True

    with pytest.raises(CalendarAdapterError) as exc_hdr:
        parse_ics_events("NOT:A:CALENDAR")
    assert exc_hdr.value.code == "invalid_ics_header"

    with pytest.raises(CalendarAdapterError) as exc_line:
        parse_ics_events("BEGIN:VCALENDAR\nMALFORMED_LINE\nEND:VCALENDAR")
    assert exc_line.value.code == "invalid_ics_line"

    with pytest.raises(CalendarAdapterError) as exc_tz:
        parse_ics_events(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\nDTSTART;TZID=Mars/Phobos:20261026T100000\n"
            "END:VEVENT\nEND:VCALENDAR"
        )
    assert exc_tz.value.code == "invalid_ics_timezone"

    with pytest.raises(CalendarAdapterError) as exc_dt:
        parse_ics_events(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\nDTSTART:NOTADATE\nEND:VEVENT\nEND:VCALENDAR"
        )
    assert exc_dt.value.code == "invalid_ics_datetime"

    with pytest.raises(CalendarAdapterError) as exc_uid:
        parse_ics_events(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Geen UID\nDTSTART:20261026T100000Z\n"
            "END:VEVENT\nEND:VCALENDAR"
        )
    assert exc_uid.value.code == "missing_ics_uid"


def test_sync_service_ics_candidate_confirmation_quiet_hours_and_zero_title_retention() -> None:
    now = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    # 22:30 Europe/Amsterdam (+01:00 on Oct 26, after DST switch) -> proposed 21:30 quiet hours!
    ics_payload = (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "BEGIN:VEVENT\n"
        "UID:repair-priv-991\n"
        "SUMMARY:Reparatie cv-ketel met Jan Jansen Kerkstraat 12\n"
        "DTSTART;TZID=Europe/Amsterdam:20261026T223000\n"
        "END:VEVENT\n"
        "BEGIN:VEVENT\n"
        "UID:med-priv-992\n"
        "SUMMARY:Afspraak huisarts Dr. Peters\n"
        "DTSTART;TZID=Europe/Amsterdam:20261026T150000\n"
        "END:VEVENT\n"
        "BEGIN:VEVENT\n"
        "UID:coffee-993\n"
        "SUMMARY:Lunch met collega\n"
        "DTSTART;TZID=Europe/Amsterdam:20261026T120000\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )
    adapter = ICalendarFeedAdapter(ics_payload)
    sync = CalendarSyncService()
    attempts = [
        _confirmed_attempt_for(Situation.REPAIR),
        _confirmed_attempt_for(
            Situation.SHIFT,
            attempt_id="01000000000000000000000011",
            breakdown_id="01000000000000000000000012",
        ),
    ]

    summary = asyncio.run(
        sync.sync_candidates(
            learner_id=LEARNER_ID,
            provider="icalendar",
            source=adapter,
            confirmed_attempts=attempts,
            now=now,
        )
    )
    assert len(summary.candidates) == 1
    assert summary.excluded_sensitive_count == 1
    assert summary.ignored_unrelated_count == 1
    assert summary.next_sync_token is not None
    assert sync.get_sync_token(learner_id=LEARNER_ID, provider="icalendar") == (
        summary.next_sync_token
    )

    candidate = summary.candidates[0]
    assert candidate.situation is Situation.REPAIR
    assert candidate.quiet_hours is True
    assert candidate.confirmation_state == "awaiting_learner_confirmation"
    assert candidate.calendar_event_reference.startswith("calref_")
    # Raw title and PII MUST NEVER appear anywhere in serialized candidate state
    serialized_repr = repr(candidate)
    assert "Jan Jansen" not in serialized_repr
    assert "Kerkstraat" not in serialized_repr
    assert "repair-priv-991" not in serialized_repr

    # Re-sync with identical feed short-circuits via sync_token
    repeat_summary = asyncio.run(
        sync.sync_candidates(
            learner_id=LEARNER_ID,
            provider="icalendar",
            source=adapter,
            confirmed_attempts=attempts,
            now=now,
        )
    )
    assert repeat_summary.candidates == ()
    assert len(sync.list_candidates(actor_learner_id=LEARNER_ID)) == 1
    assert sync.list_queued_cards(actor_learner_id=LEARNER_ID) == ()

    # Quiet hours require explicit notification_behavior selection (AC-NT-012)
    with pytest.raises(ValueError, match="quiet_hours_notification_choice_required"):
        sync.confirm_candidate_and_queue_card(
            actor_learner_id=LEARNER_ID,
            candidate_id=candidate.candidate_id,
            confirmed_at=now,
        )

    # Cross-learner isolation check
    with pytest.raises(PermissionError, match="calendar_candidate_access_denied"):
        sync.confirm_candidate_and_queue_card(
            actor_learner_id=OTHER_LEARNER,
            candidate_id=candidate.candidate_id,
            confirmed_at=now,
            notification_behavior=NotificationBehavior.OPEN_TEMPLATE_ONLY,
        )

    # Explicit confirmation queues pre-meeting rehearsal card
    card = sync.confirm_candidate_and_queue_card(
        actor_learner_id=LEARNER_ID,
        candidate_id=candidate.candidate_id,
        confirmed_at=now,
        notification_behavior=NotificationBehavior.OPEN_TEMPLATE_ONLY,
    )
    assert card.status == "queued"
    assert card.estimated_seconds == 90
    assert card.event_source == "explicit_calendar_link"
    assert card.notification_behavior is NotificationBehavior.OPEN_TEMPLATE_ONLY
    # Idempotent repeat confirmation returns same queued card
    same_card = sync.confirm_candidate_and_queue_card(
        actor_learner_id=LEARNER_ID,
        candidate_id=candidate.candidate_id,
        confirmed_at=now,
        notification_behavior=NotificationBehavior.OPEN_TEMPLATE_ONLY,
    )
    assert same_card.card_id == card.card_id

    # Cancelled update cancels queued rehearsal card automatically
    cancelled_ics = (
        "BEGIN:VCALENDAR\n"
        "BEGIN:VEVENT\n"
        "UID:repair-priv-991\n"
        "STATUS:CANCELLED\n"
        "SUMMARY:Reparatie geannuleerd\n"
        "DTSTART;TZID=Europe/Amsterdam:20261026T223000\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )
    cancel_summary = asyncio.run(
        sync.sync_candidates(
            learner_id=LEARNER_ID,
            provider="icalendar",
            source=ICalendarFeedAdapter(cancelled_ics),
            confirmed_attempts=attempts,
            now=now,
        )
    )
    assert cancel_summary.cancelled_count == 1
    assert sync.list_queued_cards(actor_learner_id=LEARNER_ID) == ()
    purged = sync.purge_learner(LEARNER_ID)
    assert purged == 2
    assert sync.list_candidates(actor_learner_id=LEARNER_ID) == ()


class _FakeGoogleClient:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = deepcopy(dict(payload))

    async def list_events_json(
        self,
        *,
        sync_token: str | None,
        time_min_iso: str,
        time_max_iso: str,
    ) -> Mapping[str, Any]:
        return self._payload


def test_google_calendar_adapter_oauth_scopes_fail_closed_and_dismissal() -> None:
    now = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    end = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)

    # Revoked grant rejected
    revoked_grant = GoogleOAuthGrant(
        learner_id=LEARNER_ID,
        granted_scopes=frozenset({REQUIRED_GOOGLE_CALENDAR_SCOPE}),
        revoked=True,
    )
    with pytest.raises(CalendarAdapterError) as exc_rev:
        asyncio.run(
            GoogleCalendarAdapter(oauth_grant=revoked_grant).fetch_events(
                sync_token=None, window_start=now, window_end=end
            )
        )
    assert exc_rev.value.code == "google_oauth_revoked"

    # Broad write scope rejected (INT-NT-006 requires incremental read-only scope)
    broad_grant = GoogleOAuthGrant(
        learner_id=LEARNER_ID,
        granted_scopes=frozenset(
            {
                REQUIRED_GOOGLE_CALENDAR_SCOPE,
                "https://www.googleapis.com/auth/calendar.events",
            }
        ),
    )
    with pytest.raises(CalendarAdapterError) as exc_broad:
        asyncio.run(
            GoogleCalendarAdapter(oauth_grant=broad_grant).fetch_events(
                sync_token=None, window_start=now, window_end=end
            )
        )
    assert exc_broad.value.code == "google_oauth_excessive_scope"

    # Missing scope rejected
    empty_grant = GoogleOAuthGrant(learner_id=LEARNER_ID, granted_scopes=frozenset())
    with pytest.raises(CalendarAdapterError) as exc_missing:
        asyncio.run(
            GoogleCalendarAdapter(oauth_grant=empty_grant).fetch_events(
                sync_token=None, window_start=now, window_end=end
            )
        )
    assert exc_missing.value.code == "google_oauth_missing_scope"

    # Unconfigured live client fails closed cleanly
    valid_grant = GoogleOAuthGrant(
        learner_id=LEARNER_ID,
        granted_scopes=frozenset({REQUIRED_GOOGLE_CALENDAR_SCOPE}),
    )
    with pytest.raises(CalendarAdapterError) as exc_unconf:
        asyncio.run(
            GoogleCalendarAdapter(oauth_grant=valid_grant).fetch_events(
                sync_token=None, window_start=now, window_end=end
            )
        )
    assert exc_unconf.value.code == "google_calendar_oauth_not_configured"

    # Valid fixture Google Calendar sync + dismissal flow
    fake_payload = {
        "nextSyncToken": "gtok_001",
        "items": [
            {
                "id": "gcal_shift_1",
                "status": "confirmed",
                "summary": "Bespreking werkrooster vrijdag",
                "description": "Met teamleider",
                "start": {"dateTime": "2026-10-27T14:00:00+01:00", "timeZone": "Europe/Amsterdam"},
                "end": {"dateTime": "2026-10-27T14:30:00+01:00", "timeZone": "Europe/Amsterdam"},
            },
            {
                "id": "gcal_school_unmatched",
                "status": "confirmed",
                "summary": "Oudergesprek schooladministratie",
                "start": {"dateTime": "2026-10-28T11:00:00Z"},
                "end": {"dateTime": "2026-10-28T11:30:00Z"},
            },
        ],
    }
    g_adapter = GoogleCalendarAdapter(
        oauth_grant=valid_grant,
        http_client=_FakeGoogleClient(fake_payload),
        provenance="fixture",
    )
    sync = CalendarSyncService()
    summary = asyncio.run(
        sync.sync_candidates(
            learner_id=LEARNER_ID,
            provider="google_calendar",
            source=g_adapter,
            confirmed_attempts=[_confirmed_attempt_for(Situation.SHIFT)],
            now=now,
        )
    )
    assert len(summary.candidates) == 1
    assert summary.ignored_unrelated_count == 1
    cand = summary.candidates[0]
    assert cand.provider == "google_calendar"
    assert cand.situation is Situation.SHIFT

    dismissed = sync.dismiss_candidate(
        actor_learner_id=LEARNER_ID,
        candidate_id=cand.candidate_id,
    )
    assert dismissed.confirmation_state == "dismissed"
    with pytest.raises(ValueError, match="dismissed_candidate_cannot_be_confirmed"):
        sync.confirm_candidate_and_queue_card(
            actor_learner_id=LEARNER_ID,
            candidate_id=cand.candidate_id,
            confirmed_at=now,
        )

    # Google Calendar malformed payload/date/all-day/naive-tz branches
    for bad_payload, expected_code in (
        ({"items": "not-a-list"}, "invalid_google_payload"),
        ({"items": ["not-a-dict"]}, "invalid_google_event"),
        ({"items": [{"status": "confirmed"}]}, "missing_google_event_id"),
        (
            {"items": [{"id": "e1", "start": {"dateTime": "not-an-iso-dt"}}]},
            "invalid_google_datetime",
        ),
    ):
        bad_adapter = GoogleCalendarAdapter(
            oauth_grant=valid_grant,
            http_client=_FakeGoogleClient(bad_payload),
        )
        with pytest.raises(CalendarAdapterError) as exc_bad:
            asyncio.run(bad_adapter.fetch_events(sync_token=None, window_start=now, window_end=end))
        assert exc_bad.value.code == expected_code

    edge_payload = {
        "items": [
            {"id": "e_allday", "start": {"date": "2026-10-29", "timeZone": "Bad/Zone"}},
            {"id": "e_naive", "start": {"dateTime": "2026-10-29T10:00:00"}},
            {"id": "e_empty_dt", "start": {"dateTime": "   "}},
            {"id": "e_no_start"},
        ]
    }
    edge_batch = asyncio.run(
        GoogleCalendarAdapter(
            oauth_grant=valid_grant,
            http_client=_FakeGoogleClient(edge_payload),
        ).fetch_events(sync_token=None, window_start=now, window_end=end)
    )
    assert edge_batch.items[0].all_day is True
    assert edge_batch.items[1].starts_at is not None
    assert edge_batch.items[2].starts_at is None
    assert edge_batch.items[3].starts_at is None
