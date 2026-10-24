from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .problem import ApiProblemError


@dataclass(frozen=True)
class IdempotencyRecord:
    request_hash: str
    status_code: int
    response_body: dict[str, Any]
    expires_at: datetime


class IdempotencyStore:
    """24-hour idempotency ledger keyed by actor, method, path, and Idempotency-Key."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str, str], IdempotencyRecord] = {}

    @staticmethod
    def _hash_payload(payload: object) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def require_key(idempotency_key: str | None) -> str:
        if idempotency_key is None or not idempotency_key.strip() or len(idempotency_key) > 128:
            raise ApiProblemError(
                status=400,
                code="idempotency_key_required",
                title="Idempotency-Key header required",
                detail="Mutations require a non-empty Idempotency-Key header (<= 128 chars).",
            )
        return idempotency_key.strip()

    def check(
        self,
        *,
        actor_id: str,
        method: str,
        path: str,
        idempotency_key: str,
        payload: object,
        now: datetime | None = None,
    ) -> tuple[int, dict[str, Any]] | None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        slot = (actor_id, method.upper(), path, idempotency_key)
        existing = self._records.get(slot)
        if existing is None:
            return None
        if current >= existing.expires_at:
            del self._records[slot]
            return None
        incoming_hash = self._hash_payload(payload)
        if existing.request_hash != incoming_hash:
            raise ApiProblemError(
                status=409,
                code="idempotency_key_reused",
                title="Idempotency key reused with different request",
                detail="The supplied Idempotency-Key is already bound to a different payload.",
            )
        return existing.status_code, deepcopy(existing.response_body)

    def save(
        self,
        *,
        actor_id: str,
        method: str,
        path: str,
        idempotency_key: str,
        payload: object,
        status_code: int,
        response_body: dict[str, Any],
        now: datetime | None = None,
    ) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        slot = (actor_id, method.upper(), path, idempotency_key)
        self._records[slot] = IdempotencyRecord(
            request_hash=self._hash_payload(payload),
            status_code=status_code,
            response_body=deepcopy(response_body),
            expires_at=current + timedelta(hours=24),
        )
