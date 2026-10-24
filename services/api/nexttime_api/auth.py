from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from fastapi import Header

from .problem import ApiProblemError

_ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
PrincipalRole = Literal["learner", "bridge"]


@dataclass(frozen=True)
class Principal:
    actor_id: str
    role: PrincipalRole
    recent_auth: bool = False


class TokenAuthenticator:
    """Validates learner-scoped and bridge-scoped bearer tokens (SEC-NT-003)."""

    def __init__(self, *, allow_structured_local_tokens: bool = True) -> None:
        self._tokens: dict[str, Principal] = {}
        self._allow_local = allow_structured_local_tokens

    def register_token(self, token: str, principal: Principal) -> None:
        if not token.strip():
            raise ValueError("Token cannot be empty")
        self._tokens[token.strip()] = principal

    def resolve(self, authorization: str | None) -> Principal:
        if authorization is None or not authorization.lower().startswith("bearer "):
            raise ApiProblemError(
                status=401,
                code="unauthenticated",
                title="Authentication required",
                detail="A valid Bearer token is required.",
            )
        raw_token = authorization[7:].strip()
        if not raw_token:
            raise ApiProblemError(
                status=401,
                code="unauthenticated",
                title="Authentication required",
                detail="Bearer token cannot be empty.",
            )
        registered = self._tokens.get(raw_token)
        if registered is not None:
            return registered

        if self._allow_local:
            for prefix, role, recent in (
                ("learner-recent:", "learner", True),
                ("learner:", "learner", False),
                ("bridge:", "bridge", False),
            ):
                if raw_token.startswith(prefix):
                    actor = raw_token[len(prefix) :].strip()
                    if _ULID_PATTERN.match(actor):
                        return Principal(
                            actor_id=actor,
                            role=role,  # type: ignore[arg-type]
                            recent_auth=recent,
                        )
        raise ApiProblemError(
            status=401,
            code="invalid_token",
            title="Invalid credentials",
            detail="The supplied Bearer token was not recognized.",
        )


def require_learner_principal(
    authenticator: TokenAuthenticator,
    authorization: str | None = Header(default=None),
) -> Principal:
    principal = authenticator.resolve(authorization)
    if principal.role != "learner":
        raise ApiProblemError(
            status=403,
            code="insufficient_scope",
            title="Learner token required",
            detail="Bridge credentials cannot access learner-scoped endpoints.",
        )
    return principal


def require_bridge_principal(
    authenticator: TokenAuthenticator,
    authorization: str | None = Header(default=None),
) -> Principal:
    principal = authenticator.resolve(authorization)
    if principal.role != "bridge":
        raise ApiProblemError(
            status=403,
            code="insufficient_scope",
            title="Bridge token required",
            detail="Only trusted Bee bridge credentials may upload source packages.",
        )
    return principal
