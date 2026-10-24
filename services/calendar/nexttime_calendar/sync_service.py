from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nexttime_domain import NotificationBehavior, Situation, deterministic_ulid

from .classifier import classify_calendar_event_situation, opaque_calendar_reference
from .models import (
    CalendarProviderKind,
    CalendarRecurrenceCandidate,
    CalendarSourcePort,
    CalendarSyncSummary,
    PreMeetingRehearsalCard,
)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Calendar timestamp must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _find_confirmed_target_for_situation(
    confirmed_attempts: Sequence[dict[str, Any]],
    *,
    learner_id: str,
    situation: Situation,
) -> tuple[str, str] | None:
    matching = [
        doc
        for doc in confirmed_attempts
        if doc.get("learner_id") == learner_id
        and doc.get("status") == "confirmed"
        and not doc.get("consent", {}).get("revoked", False)
        and doc.get("context", {}).get("situation") == situation.value
    ]
    matching.sort(key=lambda d: str(d.get("source", {}).get("recorded_at", "")), reverse=True)
    for doc in matching:
        for b in doc.get("breakdowns", []):
            if b.get("review_status") == "confirmed" and b.get("code") == "confirmation_missing":
                return str(doc["attempt_id"]), str(b["breakdown_id"])
    return None


class CalendarSyncService:
    """Discovers privacy-minimized recurrence candidates and queues confirmed rehearsal cards.

    Enforces SPEC.md FR-NT-008, INT-NT-006, SEC-NT-006, AC-NT-012, and §7 step 7:
    - Never persists raw event titles, descriptions, attendees, or locations.
    - Excludes sensitive medical/legal/immigration/disciplinary/financial/crisis events.
    - Matches only the 3 supported Dutch situations where a confirmed breakdown already exists.
    - Requires explicit learner confirmation (and quiet-hours selection) before queuing cards.
    """

    def __init__(self) -> None:
        self._candidates: dict[str, CalendarRecurrenceCandidate] = {}
        self._candidate_by_ref: dict[tuple[str, str], str] = {}
        self._cards: dict[str, PreMeetingRehearsalCard] = {}
        self._card_by_ref: dict[tuple[str, str], str] = {}
        self._sync_tokens: dict[tuple[str, CalendarProviderKind], str] = {}

    def get_sync_token(self, *, learner_id: str, provider: CalendarProviderKind) -> str | None:
        return self._sync_tokens.get((learner_id, provider))

    def list_candidates(self, *, actor_learner_id: str) -> tuple[CalendarRecurrenceCandidate, ...]:
        return tuple(
            c
            for c in sorted(self._candidates.values(), key=lambda x: x.scheduled_for)
            if c.learner_id == actor_learner_id
        )

    def list_queued_cards(self, *, actor_learner_id: str) -> tuple[PreMeetingRehearsalCard, ...]:
        return tuple(
            card
            for card in sorted(self._cards.values(), key=lambda x: x.rehearsal_at)
            if card.learner_id == actor_learner_id and card.status == "queued"
        )

    async def sync_candidates(
        self,
        *,
        learner_id: str,
        provider: CalendarProviderKind,
        source: CalendarSourcePort,
        confirmed_attempts: Sequence[dict[str, Any]],
        now: datetime,
        horizon_days: int = 14,
    ) -> CalendarSyncSummary:
        now_utc = _iso_utc(now)
        start_dt = _parse_iso(now_utc)
        end_dt = start_dt + timedelta(days=max(1, horizon_days))
        prior_token = self._sync_tokens.get((learner_id, provider))

        batch = await source.fetch_events(
            sync_token=prior_token,
            window_start=start_dt,
            window_end=end_dt,
        )
        if batch.next_sync_token is not None:
            self._sync_tokens[(learner_id, provider)] = batch.next_sync_token

        excluded_sensitive = 0
        ignored_unrelated = 0
        cancelled_count = 0
        surfaced: list[CalendarRecurrenceCandidate] = []

        for ev in batch.items:
            opaque_ref = opaque_calendar_reference(
                learner_id=learner_id,
                provider=provider,
                raw_uid=ev.raw_uid,
            )
            ref_key = (learner_id, opaque_ref)

            if ev.cancelled:
                cancelled_count += 1
                existing_cid = self._candidate_by_ref.get(ref_key)
                if existing_cid is not None and existing_cid in self._candidates:
                    self._candidates[existing_cid] = replace(
                        self._candidates[existing_cid],
                        confirmation_state="dismissed",
                    )
                existing_card_id = self._card_by_ref.get(ref_key)
                if existing_card_id is not None and existing_card_id in self._cards:
                    self._cards[existing_card_id] = replace(
                        self._cards[existing_card_id],
                        status="cancelled",
                    )
                continue

            if ev.all_day or ev.starts_at is None or ev.starts_at <= start_dt:
                ignored_unrelated += 1
                continue

            classification = classify_calendar_event_situation(ev.summary, ev.description)
            if classification == "excluded_sensitive":
                excluded_sensitive += 1
                continue
            if not isinstance(classification, Situation):
                ignored_unrelated += 1
                continue

            target_pair = _find_confirmed_target_for_situation(
                confirmed_attempts,
                learner_id=learner_id,
                situation=classification,
            )
            if target_pair is None:
                ignored_unrelated += 1
                continue

            existing_cid = self._candidate_by_ref.get(ref_key)
            if existing_cid is not None and existing_cid in self._candidates:
                surfaced.append(self._candidates[existing_cid])
                continue

            attempt_id, breakdown_id = target_pair
            proposed_rehearsal = max(start_dt, ev.starts_at - timedelta(minutes=60))
            local_reh = proposed_rehearsal.astimezone(ZoneInfo(ev.time_zone))
            quiet = local_reh.hour >= 21 or local_reh.hour < 8

            candidate_id = deterministic_ulid(
                namespace="cal-candidate",
                seed=f"{learner_id}:{opaque_ref}",
                timestamp=start_dt,
            )
            candidate = CalendarRecurrenceCandidate(
                candidate_id=candidate_id,
                learner_id=learner_id,
                provider=provider,
                calendar_event_reference=opaque_ref,
                situation=classification,
                matched_attempt_id=attempt_id,
                matched_breakdown_id=breakdown_id,
                scheduled_for=_iso_utc(ev.starts_at),
                proposed_rehearsal_at=_iso_utc(proposed_rehearsal),
                time_zone=ev.time_zone,
                quiet_hours=quiet,
                confirmation_state="awaiting_learner_confirmation",
                provenance=batch.provenance,
                created_at=now_utc,
            )
            self._candidates[candidate_id] = candidate
            self._candidate_by_ref[ref_key] = candidate_id
            surfaced.append(candidate)

        return CalendarSyncSummary(
            learner_id=learner_id,
            provider=provider,
            candidates=tuple(surfaced),
            excluded_sensitive_count=excluded_sensitive,
            ignored_unrelated_count=ignored_unrelated,
            cancelled_count=cancelled_count,
            next_sync_token=batch.next_sync_token,
            provenance=batch.provenance,
        )

    def dismiss_candidate(
        self,
        *,
        actor_learner_id: str,
        candidate_id: str,
    ) -> CalendarRecurrenceCandidate:
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise KeyError("calendar_candidate_not_found")
        if candidate.learner_id != actor_learner_id:
            raise PermissionError("calendar_candidate_access_denied")
        updated = replace(candidate, confirmation_state="dismissed")
        self._candidates[candidate_id] = updated
        return updated

    def confirm_candidate_and_queue_card(
        self,
        *,
        actor_learner_id: str,
        candidate_id: str,
        confirmed_at: datetime,
        rehearsal_at: datetime | None = None,
        notification_behavior: NotificationBehavior | None = None,
    ) -> PreMeetingRehearsalCard:
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise KeyError("calendar_candidate_not_found")
        if candidate.learner_id != actor_learner_id:
            raise PermissionError("calendar_candidate_access_denied")
        if candidate.confirmation_state == "dismissed":
            raise ValueError("dismissed_candidate_cannot_be_confirmed")

        ref_key = (actor_learner_id, candidate.calendar_event_reference)
        existing_card_id = self._card_by_ref.get(ref_key)
        if existing_card_id is not None and existing_card_id in self._cards:
            existing_card = self._cards[existing_card_id]
            if existing_card.status == "queued":
                return existing_card

        now_utc = _parse_iso(_iso_utc(confirmed_at))
        sched_utc = _parse_iso(candidate.scheduled_for)
        chosen_utc = (
            _parse_iso(_iso_utc(rehearsal_at))
            if rehearsal_at is not None
            else _parse_iso(candidate.proposed_rehearsal_at)
        )
        if chosen_utc < now_utc or chosen_utc > sched_utc:
            raise ValueError("invalid_rehearsal_window")

        local_reh = chosen_utc.astimezone(ZoneInfo(candidate.time_zone))
        quiet = local_reh.hour >= 21 or local_reh.hour < 8
        if quiet and notification_behavior is None:
            raise ValueError("quiet_hours_notification_choice_required")
        resolved_behavior = notification_behavior or NotificationBehavior.LOCAL_NOTIFICATION

        recurrence_id = deterministic_ulid(
            namespace="cal-recurrence",
            seed=f"{actor_learner_id}:{candidate.calendar_event_reference}",
            timestamp=now_utc,
        )
        card_id = deterministic_ulid(
            namespace="rehearsal-card",
            seed=f"{actor_learner_id}:{recurrence_id}",
            timestamp=now_utc,
        )
        card = PreMeetingRehearsalCard(
            card_id=card_id,
            learner_id=actor_learner_id,
            candidate_id=candidate_id,
            recurrence_id=recurrence_id,
            attempt_id=candidate.matched_attempt_id,
            breakdown_id=candidate.matched_breakdown_id,
            situation=candidate.situation,
            target_function="confirmation",
            scheduled_for=candidate.scheduled_for,
            rehearsal_at=_iso_utc(chosen_utc),
            time_zone=candidate.time_zone,
            estimated_seconds=90,
            quiet_hours=quiet,
            notification_behavior=resolved_behavior,
            event_source="explicit_calendar_link",
            calendar_event_reference=candidate.calendar_event_reference,
            status="queued",
            provenance=candidate.provenance,
        )
        self._candidates[candidate_id] = replace(candidate, confirmation_state="confirmed")
        self._cards[card_id] = card
        self._card_by_ref[ref_key] = card_id
        return card

    def purge_learner(self, learner_id: str) -> int:
        cids = [cid for cid, c in self._candidates.items() if c.learner_id == learner_id]
        for cid in cids:
            del self._candidates[cid]
        card_ids = [kid for kid, k in self._cards.items() if k.learner_id == learner_id]
        for kid in card_ids:
            del self._cards[kid]
        for key in [k for k in self._candidate_by_ref if k[0] == learner_id]:
            del self._candidate_by_ref[key]
        for key in [k for k in self._card_by_ref if k[0] == learner_id]:
            del self._card_by_ref[key]
        for tkey in [k for k in self._sync_tokens if k[0] == learner_id]:
            del self._sync_tokens[tkey]
        return len(cids) + len(card_ids)
