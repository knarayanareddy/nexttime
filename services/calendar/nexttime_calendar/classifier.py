from __future__ import annotations

import hashlib
import re
from typing import Literal

from nexttime_domain import (
    SelfExcerpt,
    SensitiveContentError,
    Situation,
    ensure_non_sensitive_excerpts,
)

from .models import CalendarProviderKind

_SENSITIVE_CALENDAR_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Medical / crisis / mental health
    re.compile(
        r"\b(?:dokter|huisarts|ziekenhuis|tandarts|chirurg|psycholoog|therapie|therapist|"
        r"medical|doctor|hospital|clinic|crisis|spoedopname|suicide|psychiatr)\b",
        re.IGNORECASE,
    ),
    # Legal / immigration / law enforcement
    re.compile(
        r"\b(?:advocaat|rechtbank|rechter|juridisch|politie|immigratie|verblijfsvergunning|"
        r"asiel|visum|lawyer|attorney|court|tribunal|immigration|visa|deportation)\b",
        re.IGNORECASE,
    ),
    # Disciplinary / HR / financial distress
    re.compile(
        r"\b(?:ontslag|schorsing|disciplinair|waarschuwingsgesprek|schuldsanering|incasso|"
        r"deurwaarder|hypotheek|belastingdienst|disciplinary|termination|pip\s+review|"
        r"debt\s+collection|bankruptcy|foreclosure)\b",
        re.IGNORECASE,
    ),
)

_SITUATION_PATTERNS: tuple[tuple[Situation, re.Pattern[str]], ...] = (
    (
        Situation.REPAIR,
        re.compile(
            r"\b(?:reparatie|reparatieafspraak|monteur|loodgieter|cv-ketel|lekkage|"
            r"onderhoudsafspraak|woningcorporatie|repair\s+appointment|plumber|"
            r"maintenance\s+visit|apartment\s+repair)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Situation.SCHOOL_ADMIN,
        re.compile(
            r"\b(?:oudergesprek|schooladministratie|schoolafspraak|mentor\s+gesprek|"
            r"leerkracht|schoolinschrijving|school\s+admin|school\s+appointment|"
            r"parent-teacher)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Situation.SHIFT,
        re.compile(
            r"\b(?:werkrooster|dienstoverleg|dienst\s+wisselen|roosterbespreking|"
            r"ploegendienst|shift\s+clarification|shift\s+handover|shift\s+schedule)\b",
            re.IGNORECASE,
        ),
    ),
)


def opaque_calendar_reference(
    *,
    learner_id: str,
    provider: CalendarProviderKind,
    raw_uid: str,
) -> str:
    cleaned_uid = raw_uid.strip()
    if not cleaned_uid:
        raise ValueError("Calendar event UID cannot be empty")
    digest = hashlib.sha256(f"{learner_id}:{provider}:{cleaned_uid}".encode()).hexdigest()[:24]
    return f"calref_{digest}"


def classify_calendar_event_situation(
    summary: str,
    description: str = "",
) -> Situation | Literal["excluded_sensitive", "unrelated"]:
    combined = f"{summary.strip()} {description.strip()}".strip()
    if not combined:
        return "unrelated"

    # Fail-closed sensitive domain check (SEC-NT-006)
    try:
        ensure_non_sensitive_excerpts((SelfExcerpt(combined[:500], 0, 1000, 1.0),))
    except SensitiveContentError:
        return "excluded_sensitive"

    for pattern in _SENSITIVE_CALENDAR_PATTERNS:
        if pattern.search(combined):
            return "excluded_sensitive"

    matches: list[Situation] = [
        situation for situation, pattern in _SITUATION_PATTERNS if pattern.search(combined)
    ]
    if len(matches) != 1:
        return "unrelated"
    return matches[0]
