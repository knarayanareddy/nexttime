from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    CalendarAdapterError,
    CalendarProvenance,
    CalendarSyncBatch,
    TransientCalendarEvent,
)

MAX_ICS_BYTES = 1_048_576


def _unfold_ics_lines(raw_ics: str) -> list[str]:
    normalized = raw_ics.replace("\r\n", "\n").replace("\r", "\n")
    unfolded: list[str] = []
    for line in normalized.split("\n"):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return [line for line in unfolded if line]


def _unescape_ics_text(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _parse_ics_property(line: str) -> tuple[str, dict[str, str], str]:
    if ":" not in line:
        raise CalendarAdapterError("invalid_ics_line", "Malformed iCalendar property line")
    left, value = line.split(":", 1)
    parts = left.split(";")
    name = parts[0].strip().upper()
    params: dict[str, str] = {}
    for raw_param in parts[1:]:
        if "=" in raw_param:
            k, v = raw_param.split("=", 1)
            params[k.strip().upper()] = v.strip().strip('"')
    return name, params, value


def _parse_ics_datetime(
    raw_value: str,
    params: dict[str, str],
    default_tz: str,
) -> tuple[datetime | None, str, bool]:
    val = raw_value.strip()
    if params.get("VALUE", "").upper() == "DATE" or (len(val) == 8 and val.isdigit()):
        return None, default_tz, True

    tz_name = params.get("TZID", default_tz)
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as error:
        raise CalendarAdapterError("invalid_ics_timezone", "Unknown iCalendar TZID") from error

    try:
        if val.endswith("Z"):
            dt = datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            return dt, tz_name, False
        naive = datetime.strptime(val, "%Y%m%dT%H%M%S")
        aware = naive.replace(tzinfo=zone).astimezone(UTC)
        return aware, tz_name, False
    except ValueError as error:
        raise CalendarAdapterError(
            "invalid_ics_datetime", "Invalid iCalendar DTSTART/DTEND"
        ) from error


def parse_ics_events(
    raw_ics: str,
    *,
    default_time_zone: str = "Europe/Amsterdam",
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> tuple[TransientCalendarEvent, ...]:
    if len(raw_ics.encode("utf-8")) > MAX_ICS_BYTES:
        raise CalendarAdapterError("ics_payload_too_large", "iCalendar payload exceeds 1 MiB cap")
    lines = _unfold_ics_lines(raw_ics)
    if not lines or lines[0].strip().upper() != "BEGIN:VCALENDAR":
        raise CalendarAdapterError("invalid_ics_header", "Missing BEGIN:VCALENDAR header")

    events: list[TransientCalendarEvent] = []
    in_event = False
    current_props: dict[str, tuple[dict[str, str], str]] = {}
    cal_tz = default_time_zone

    for line in lines:
        upper = line.strip().upper()
        if upper == "BEGIN:VEVENT":
            in_event = True
            current_props = {}
            continue
        if upper == "END:VEVENT":
            if in_event:
                uid_entry = current_props.get("UID")
                dtstart_entry = current_props.get("DTSTART")
                if uid_entry is None or not uid_entry[1].strip():
                    raise CalendarAdapterError("missing_ics_uid", "VEVENT missing UID")
                cancelled = current_props.get("STATUS", ({}, ""))[1].strip().upper() == "CANCELLED"
                starts_at: datetime | None = None
                ends_at: datetime | None = None
                tz_used = cal_tz
                all_day = False
                if dtstart_entry is not None:
                    starts_at, tz_used, all_day = _parse_ics_datetime(
                        dtstart_entry[1], dtstart_entry[0], cal_tz
                    )
                dtend_entry = current_props.get("DTEND")
                if dtend_entry is not None and not all_day:
                    ends_at, _, _ = _parse_ics_datetime(dtend_entry[1], dtend_entry[0], tz_used)

                in_window = True
                if starts_at is not None:
                    if window_start is not None and starts_at < window_start.astimezone(UTC):
                        in_window = False
                    if window_end is not None and starts_at > window_end.astimezone(UTC):
                        in_window = False
                if in_window or cancelled:
                    summary = _unescape_ics_text(current_props.get("SUMMARY", ({}, ""))[1])
                    desc = _unescape_ics_text(current_props.get("DESCRIPTION", ({}, ""))[1])
                    events.append(
                        TransientCalendarEvent(
                            raw_uid=uid_entry[1].strip(),
                            provider="icalendar",
                            summary=summary,
                            description=desc,
                            starts_at=starts_at,
                            ends_at=ends_at,
                            time_zone=tz_used,
                            cancelled=cancelled,
                            all_day=all_day,
                        )
                    )
            in_event = False
            current_props = {}
            continue

        name, params, val = _parse_ics_property(line)
        if not in_event and name == "X-WR-TIMEZONE" and val.strip():
            cal_tz = val.strip()
        elif in_event:
            current_props[name] = (params, val)

    return tuple(events)


class ICalendarFeedAdapter:
    """Local/feed RFC 5545 iCalendar adapter producing transient events for candidate matching."""

    def __init__(
        self,
        raw_ics: str,
        *,
        default_time_zone: str = "Europe/Amsterdam",
        provenance: CalendarProvenance = "fixture",
    ) -> None:
        self._raw_ics = raw_ics
        self._default_tz = default_time_zone
        self._provenance = provenance

    async def fetch_events(
        self,
        *,
        sync_token: str | None,
        window_start: datetime,
        window_end: datetime,
    ) -> CalendarSyncBatch:
        token = "ics_sha256_" + hashlib.sha256(self._raw_ics.encode("utf-8")).hexdigest()[:20]
        if sync_token == token:
            return CalendarSyncBatch(items=(), next_sync_token=token, provenance=self._provenance)
        parsed = parse_ics_events(
            self._raw_ics,
            default_time_zone=self._default_tz,
            window_start=window_start,
            window_end=window_end,
        )
        return CalendarSyncBatch(items=parsed, next_sync_token=token, provenance=self._provenance)
