from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from .auth import Principal, require_bridge_principal, require_learner_principal
from .problem import register_problem_handlers
from .schemas import (
    AttemptExcludeRequest,
    AttemptIngestRequest,
    AttemptIngestResponse,
    AttemptListResponse,
    AttemptReviewRequest,
    AttemptReviewResponse,
    CalendarLinkRequest,
    ConsentCreateRequest,
    ConsentResponse,
    ConsentRevokeResponse,
    DeletionJobResponse,
    ExportResponse,
    HealthResponse,
    ProgressHistoryResponse,
    RecurrenceCreateRequest,
    RecurrenceResponse,
    RehearsalCompleteRequest,
    RehearsalCreateRequest,
    RehearsalSessionResponse,
    RehearsalTurnSubmitRequest,
    RehearsalTurnSubmitResponse,
)
from .state import ApiRuntimeState


def create_app(state: ApiRuntimeState | None = None) -> FastAPI:
    runtime = state or ApiRuntimeState()
    app = FastAPI(
        title="NextTime API",
        version="0.1.0",
        description=(
            "Consented real-world language-attempt review, bounded 90-second Dutch rehearsal "
            "sessions, and non-gamified authentic learner progress history."
        ),
    )
    app.state.runtime = runtime
    register_problem_handlers(app)

    def _learner(authorization: str | None = Header(default=None)) -> Principal:
        return require_learner_principal(runtime.authenticator, authorization)

    def _bridge(authorization: str | None = Header(default=None)) -> Principal:
        return require_bridge_principal(runtime.authenticator, authorization)

    def _idempotent_mutation(
        request: Request,
        *,
        actor_id: str,
        idempotency_key: str | None,
        payload: object,
        status_code: int,
        action: Callable[[], dict[str, Any]],
    ) -> JSONResponse:
        key = runtime.idempotency.require_key(idempotency_key)
        now = runtime.now()
        cached = runtime.idempotency.check(
            actor_id=actor_id,
            method=request.method,
            path=request.url.path,
            idempotency_key=key,
            payload=payload,
            now=now,
        )
        if cached is not None:
            return JSONResponse(status_code=cached[0], content=cached[1])
        body = action()
        runtime.idempotency.save(
            actor_id=actor_id,
            method=request.method,
            path=request.url.path,
            idempotency_key=key,
            payload=payload,
            status_code=status_code,
            response_body=body,
            now=now,
        )
        return JSONResponse(status_code=status_code, content=body)

    @app.get("/healthz", response_model=HealthResponse, tags=["system"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "nexttime-api", "version": "0.1.0"}

    @app.post(
        "/v1/consents",
        response_model=ConsentResponse,
        status_code=201,
        tags=["privacy"],
    )
    def create_consent(
        request: Request,
        body: ConsentCreateRequest,
        principal: Principal = Depends(_learner),  # noqa: B008
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        return _idempotent_mutation(
            request,
            actor_id=principal.actor_id,
            idempotency_key=idempotency_key,
            payload=body.model_dump(mode="json"),
            status_code=201,
            action=lambda: runtime.create_consent(actor_id=principal.actor_id, req=body),
        )

    @app.post(
        "/v1/consents/{consent_id}/revoke",
        response_model=ConsentRevokeResponse,
        tags=["privacy"],
    )
    def revoke_consent(
        consent_id: str,
        request: Request,
        principal: Principal = Depends(_learner),  # noqa: B008
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        return _idempotent_mutation(
            request,
            actor_id=principal.actor_id,
            idempotency_key=idempotency_key,
            payload={"consent_id": consent_id},
            status_code=200,
            action=lambda: runtime.revoke_consent(
                actor_id=principal.actor_id, consent_id=consent_id
            ),
        )

    @app.post(
        "/v1/attempts/ingest",
        response_model=AttemptIngestResponse,
        status_code=201,
        tags=["attempts"],
    )
    async def ingest_attempt(
        request: Request,
        body: AttemptIngestRequest,
        principal: Principal = Depends(_bridge),  # noqa: B008
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        key = runtime.idempotency.require_key(idempotency_key)
        payload = body.model_dump(mode="json")
        now = runtime.now()
        cached = runtime.idempotency.check(
            actor_id=principal.actor_id,
            method=request.method,
            path=request.url.path,
            idempotency_key=key,
            payload=payload,
            now=now,
        )
        if cached is not None:
            return JSONResponse(status_code=cached[0], content=cached[1])
        result = await runtime.ingest_attempt(body)
        status_code = 200 if result["status"] == "duplicate" else 201
        runtime.idempotency.save(
            actor_id=principal.actor_id,
            method=request.method,
            path=request.url.path,
            idempotency_key=key,
            payload=payload,
            status_code=status_code,
            response_body=result,
            now=now,
        )
        return JSONResponse(status_code=status_code, content=result)

    @app.get(
        "/v1/attempts",
        response_model=AttemptListResponse,
        tags=["attempts"],
    )
    def list_attempts(
        cursor: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=20, ge=1, le=50),
        principal: Principal = Depends(_learner),  # noqa: B008
    ) -> dict[str, Any]:
        return runtime.list_attempts(actor_id=principal.actor_id, cursor=cursor, limit=limit)

    @app.get(
        "/v1/attempts/{attempt_id}",
        response_model=dict[str, Any],
        tags=["attempts"],
    )
    def get_attempt(
        attempt_id: str,
        principal: Principal = Depends(_learner),  # noqa: B008
    ) -> dict[str, Any]:
        return runtime.get_attempt(actor_id=principal.actor_id, attempt_id=attempt_id)

    @app.post(
        "/v1/attempts/{attempt_id}/review",
        response_model=AttemptReviewResponse,
        tags=["attempts"],
    )
    def review_attempt(
        attempt_id: str,
        request: Request,
        body: AttemptReviewRequest,
        principal: Principal = Depends(_learner),  # noqa: B008
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> JSONResponse:
        return _idempotent_mutation(
            request,
            actor_id=principal.actor_id,
            idempotency_key=idempotency_key,
            payload={"attempt_id": attempt_id, "body": body.model_dump(mode="json")},
            status_code=200,
            action=lambda: runtime.review_attempt(
                actor_id=principal.actor_id,
                attempt_id=attempt_id,
                req=body,
                if_match=if_match,
            ),
        )

    @app.post(
        "/v1/attempts/{attempt_id}/exclude",
        response_model=AttemptReviewResponse,
        tags=["attempts"],
    )
    def exclude_attempt(
        attempt_id: str,
        request: Request,
        body: AttemptExcludeRequest,
        principal: Principal = Depends(_learner),  # noqa: B008
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        return _idempotent_mutation(
            request,
            actor_id=principal.actor_id,
            idempotency_key=idempotency_key,
            payload={"attempt_id": attempt_id, "body": body.model_dump(mode="json")},
            status_code=200,
            action=lambda: runtime.exclude_attempt(
                actor_id=principal.actor_id, attempt_id=attempt_id, req=body
            ),
        )

    @app.post(
        "/v1/recurrences",
        response_model=RecurrenceResponse,
        status_code=201,
        tags=["rehearsals"],
    )
    def create_recurrence(
        request: Request,
        body: RecurrenceCreateRequest,
        principal: Principal = Depends(_learner),  # noqa: B008
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        return _idempotent_mutation(
            request,
            actor_id=principal.actor_id,
            idempotency_key=idempotency_key,
            payload=body.model_dump(mode="json"),
            status_code=201,
            action=lambda: runtime.create_recurrence(actor_id=principal.actor_id, req=body),
        )

    @app.post(
        "/v1/recurrences/{recurrence_id}/calendar-link",
        response_model=RecurrenceResponse,
        tags=["rehearsals"],
    )
    def link_calendar_event(
        recurrence_id: str,
        request: Request,
        body: CalendarLinkRequest,
        principal: Principal = Depends(_learner),  # noqa: B008
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        return _idempotent_mutation(
            request,
            actor_id=principal.actor_id,
            idempotency_key=idempotency_key,
            payload={"recurrence_id": recurrence_id, "body": body.model_dump(mode="json")},
            status_code=200,
            action=lambda: runtime.link_calendar_event(
                actor_id=principal.actor_id,
                recurrence_id=recurrence_id,
                req=body,
            ),
        )

    @app.post(
        "/v1/recurrences/{recurrence_id}/rehearsal",
        response_model=RehearsalSessionResponse,
        status_code=201,
        tags=["rehearsals"],
    )
    def create_rehearsal(
        recurrence_id: str,
        request: Request,
        body: RehearsalCreateRequest,
        principal: Principal = Depends(_learner),  # noqa: B008
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        return _idempotent_mutation(
            request,
            actor_id=principal.actor_id,
            idempotency_key=idempotency_key,
            payload={"recurrence_id": recurrence_id, "body": body.model_dump(mode="json")},
            status_code=201,
            action=lambda: runtime.create_rehearsal(
                actor_id=principal.actor_id,
                recurrence_id=recurrence_id,
                req=body,
            ),
        )

    @app.get(
        "/v1/rehearsals/{rehearsal_id}",
        response_model=RehearsalSessionResponse,
        tags=["rehearsals"],
    )
    def get_rehearsal(
        rehearsal_id: str,
        principal: Principal = Depends(_learner),  # noqa: B008
    ) -> dict[str, Any]:
        return runtime.get_rehearsal(actor_id=principal.actor_id, rehearsal_id=rehearsal_id)

    @app.post(
        "/v1/rehearsals/{rehearsal_id}/turns",
        response_model=RehearsalTurnSubmitResponse,
        tags=["rehearsals"],
    )
    def submit_rehearsal_turn(
        rehearsal_id: str,
        request: Request,
        body: RehearsalTurnSubmitRequest,
        principal: Principal = Depends(_learner),  # noqa: B008
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        return _idempotent_mutation(
            request,
            actor_id=principal.actor_id,
            idempotency_key=idempotency_key,
            payload={"rehearsal_id": rehearsal_id, "body": body.model_dump(mode="json")},
            status_code=200,
            action=lambda: runtime.submit_rehearsal_turn(
                actor_id=principal.actor_id,
                rehearsal_id=rehearsal_id,
                req=body,
            ),
        )

    @app.post(
        "/v1/rehearsals/{rehearsal_id}/complete",
        response_model=RehearsalSessionResponse,
        tags=["rehearsals"],
    )
    def complete_rehearsal(
        rehearsal_id: str,
        request: Request,
        body: RehearsalCompleteRequest,
        principal: Principal = Depends(_learner),  # noqa: B008
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        return _idempotent_mutation(
            request,
            actor_id=principal.actor_id,
            idempotency_key=idempotency_key,
            payload={"rehearsal_id": rehearsal_id, "body": body.model_dump(mode="json")},
            status_code=200,
            action=lambda: runtime.complete_rehearsal(
                actor_id=principal.actor_id,
                rehearsal_id=rehearsal_id,
                req=body,
            ),
        )

    @app.get(
        "/v1/progress",
        response_model=ProgressHistoryResponse,
        tags=["progress"],
    )
    def get_progress(
        principal: Principal = Depends(_learner),  # noqa: B008
    ) -> dict[str, Any]:
        return runtime.get_progress(actor_id=principal.actor_id)

    @app.get(
        "/v1/export",
        response_model=ExportResponse,
        tags=["privacy"],
    )
    def export_data(
        principal: Principal = Depends(_learner),  # noqa: B008
    ) -> dict[str, Any]:
        return runtime.export_learner_data(
            actor_id=principal.actor_id, recent_auth=principal.recent_auth
        )

    @app.delete(
        "/v1/account",
        response_model=DeletionJobResponse,
        tags=["privacy"],
    )
    def delete_account(
        request: Request,
        principal: Principal = Depends(_learner),  # noqa: B008
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        return _idempotent_mutation(
            request,
            actor_id=principal.actor_id,
            idempotency_key=idempotency_key,
            payload={"action": "delete_account", "learner_id": principal.actor_id},
            status_code=200,
            action=lambda: runtime.delete_learner_account(
                actor_id=principal.actor_id,
                recent_auth=principal.recent_auth,
            ),
        )

    return app
