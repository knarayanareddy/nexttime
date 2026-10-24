from __future__ import annotations

import ast
import json
from pathlib import Path

from nexttime_api import create_app

ROOT = Path(__file__).resolve().parents[2]


def test_openapi_v1_contract_has_no_drift_and_covers_rehearsal_and_progress_routes() -> None:
    committed_path = ROOT / "packages" / "contracts" / "openapi.v1.json"
    assert committed_path.is_file(), "packages/contracts/openapi.v1.json must be committed"

    committed = json.loads(committed_path.read_text(encoding="utf-8"))
    generated = json.loads(json.dumps(create_app().openapi(), sort_keys=True))

    assert committed["openapi"].startswith("3.1.")
    assert committed == generated, "Committed OpenAPI spec drifted from create_app().openapi()"

    expected_routes = {
        "/healthz": {"get"},
        "/v1/health": {"get"},
        "/v1/consents": {"post"},
        "/v1/consents/{consent_id}/revoke": {"post"},
        "/v1/attempts/ingest": {"post"},
        "/v1/attempts": {"get"},
        "/v1/attempts/{attempt_id}": {"get"},
        "/v1/attempts/{attempt_id}/review": {"post"},
        "/v1/attempts/{attempt_id}/exclude": {"post"},
        "/v1/recurrences": {"post"},
        "/v1/recurrences/{recurrence_id}/calendar-link": {"post"},
        "/v1/recurrences/{recurrence_id}/rehearsal": {"post"},
        "/v1/calendar/ics-sync": {"post"},
        "/v1/calendar/candidates": {"get"},
        "/v1/calendar/candidates/{candidate_id}/confirm": {"post"},
        "/v1/rehearsal-cards": {"get"},
        "/v1/rehearsals/{rehearsal_id}": {"get"},
        "/v1/rehearsals/{rehearsal_id}/turns": {"post"},
        "/v1/rehearsals/{rehearsal_id}/transcribe-chunks": {"post"},
        "/v1/rehearsals/{rehearsal_id}/haptics": {"post"},
        "/v1/rehearsals/{rehearsal_id}/complete": {"post"},
        "/v1/progress": {"get"},
        "/v1/export": {"get"},
        "/v1/account": {"delete"},
    }
    for route, methods in expected_routes.items():
        assert route in committed["paths"], f"Missing route {route}"
        assert methods.issubset(set(committed["paths"][route].keys()))

    ws_paths = {getattr(r, "path", None) for r in create_app().routes}
    assert "/v1/rehearsal/ws" in ws_paths, "Missing WebSocket route /v1/rehearsal/ws"


def test_domain_package_remains_pure_stdlib_without_framework_or_cloud_imports() -> None:
    forbidden = {
        "fastapi",
        "pydantic",
        "starlette",
        "uvicorn",
        "httpx",
        "boto3",
        "sqlalchemy",
    }
    domain_dir = ROOT / "packages" / "domain" / "nexttime_domain"
    for py_file in domain_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    assert top not in forbidden, f"Forbidden import {top} in {py_file.name}"
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                top = node.module.split(".")[0]
                assert top not in forbidden, f"Forbidden import {top} in {py_file.name}"
