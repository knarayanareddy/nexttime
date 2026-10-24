from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    CalendarAdapterError,
    CalendarProvenance,
    CalendarSyncBatch,
    TransientCalendarEvent,
)

REQUIRED_GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
FORBIDDEN_WRITE_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
    }
)


@dataclass(frozen=True)
class GoogleOAuthGrant:
    learner_id: str
    granted_scopes: frozenset[str]
    revoked: bool = False

    def validate_incremental_readonly(self) -> None:
        if self.revoked:
            raise CalendarAdapterError(
                "google_oauth_revoked",
                "Google Calendar incremental OAuth grant has been revoked.",
            )
        if self.granted_scopes & FORBIDDEN_WRITE_SCOPES:
            raise CalendarAdapterError(
                "google_oauth_excessive_scope",
                "Only incremental calendar.events.readonly scope is permitted.",
            )
        if REQUIRED_GOOGLE_CALENDAR_SCOPE not in self.granted_scopes:
            raise CalendarAdapterError(
                "google_oauth_missing_scope",
                "Incremental calendar.events.readonly OAuth scope is required.",
            )


class GoogleCalendarHttpPort(Protocol):
    async def list_events_json(
        self,
        *,
        sync_token: str | None,
        time_min_iso: str,
        time_max_iso: str,
    ) -> Mapping[str, Any]: ...


class UnconfiguredGoogleCalendarClient:
    """Fail-closed default when live Google Calendar OAuth credentials are not configured."""

    async def list_events_json(
        self,
        *,
        sync_token: str | None,
        time_min_iso: str,
        time_max_iso: str,
    ) -> Mapping[str, Any]:
        raise CalendarAdapterError(
            "google_calendar_oauth_not_configured",
            "Live Google Calendar OAuth client is not configured in this environment.",
        )


def _parse_google_dt(
    block: Mapping[str, Any] | None,
    fallback_tz: str,
) -> tuple[datetime | None, str, bool]:
    if not block:
        return None, fallback_tz, False
    tz_name = str(block.get("timeZone") or fallback_tz)
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz_name = fallback_tz

    if isinstance(block.get("date"), str) and not block.get("dateTime"):
        return None, tz_name, True

    raw_dt = block.get("dateTime")
    if not isinstance(raw_dt, str) or not raw_dt.strip():
        return None, tz_name, False
    try:
        parsed = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
    except ValueError as error:
        raise CalendarAdapterError(
            "invalid_google_datetime",
            "Malformed Google Calendar event dateTime.",
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
    return parsed.astimezone(UTC), tz_name, False


class GoogleCalendarAdapter:
    """Incremental-OAuth Google Calendar read-only adapter producing transient events."""

    def __init__(
        self,
        *,
        oauth_grant: GoogleOAuthGrant,
        http_client: GoogleCalendarHttpPort | None = None,
        default_time_zone: str = "Europe/Amsterdam",
        provenance: CalendarProvenance = "fixture",
    ) -> None:
        self._grant = oauth_grant
        self._client: GoogleCalendarHttpPort = http_client or UnconfiguredGoogleCalendarClient()
        self._default_tz = default_time_zone
        self._provenance = provenance

    async def fetch_events(
        self,
        *,
        sync_token: str | None,
        window_start: datetime,
        window_end: datetime,
    ) -> CalendarSyncBatch:
        self._grant.validate_incremental_readonly()
        t_min = window_start.astimezone(UTC).isoformat().replace("+00:00", "Z")
        t_max = window_end.astimezone(UTC).isoformat().replace("+00:00", "Z")
        payload = await self._client.list_events_json(
            sync_token=sync_token,
            time_min_iso=t_min,
            time_max_iso=t_max,
        )
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise CalendarAdapterError(
                "invalid_google_payload",
                "Google Calendar response items must be a list.",
            )

        events: list[TransientCalendarEvent] = []
        for item in raw_items:
            if not isinstance(item, Mapping):
                raise CalendarAdapterError(
                    "invalid_google_event",
                    "Google Calendar item must be an object.",
                )
            event_id = item.get("id")
            if not isinstance(event_id, str) or not event_id.strip():
                raise CalendarAdapterError(
                    "missing_google_event_id",
                    "Google Calendar event missing id.",
                )
            cancelled = str(item.get("status", "")).lower() == "cancelled"
            start_block = item.get("start")
            end_block = item.get("end")
            starts_at, tz_used, all_day = _parse_google_dt(
                start_block if isinstance(start_block, Mapping) else None,
                self._default_tz,
            )
            ends_at, _, _ = _parse_google_dt(
                end_block if isinstance(end_block, Mapping) else None,
                tz_used,
            )
            if starts_at is not None and not cancelled:
                if starts_at < window_start.astimezone(UTC) or starts_at > window_end.astimezone(
                    UTC
                ):
                    continue
            events.append(
                TransientCalendarEvent(
                    raw_uid=event_id.strip(),
                    provider="google_calendar",
                    summary=str(item.get("summary") or ""),
                    description=str(item.get("description") or ""),
                    starts_at=starts_at,
                    ends_at=ends_at,
                    time_zone=tz_used,
                    cancelled=cancelled,
                    all_day=all_day,
                )
            )

        next_token = payload.get("nextSyncToken")
        if next_token is not None and not isinstance(next_token, str):
            raise CalendarAdapterError(
                "invalid_google_sync_token",
                "Google Calendar nextSyncToken must be a string or null.",
            )
        return CalendarSyncBatch(
            items=tuple(events),
            next_sync_token=next_token,
            provenance=self._provenance,
        )
