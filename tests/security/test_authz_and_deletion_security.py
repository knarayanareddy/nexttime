from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from nexttime_api import ApiRuntimeState, create_app

LEARNER_A = "01000000000000000000000002"
LEARNER_B = "01999999999999999999999999"
CONSENT_A = "01000000000000000000000003"
BRIDGE_ID = "01000000000000000000000088"

HEADERS_A = {"Authorization": f"Bearer learner:{LEARNER_A}"}
HEADERS_B = {"Authorization": f"Bearer learner:{LEARNER_B}"}
HEADERS_BRIDGE = {"Authorization": f"Bearer bridge:{BRIDGE_ID}"}


def test_cross_learner_object_authorization_and_token_scope_isolation() -> None:
    client = TestClient(
        create_app(ApiRuntimeState(clock=datetime(2026, 10, 24, 10, 0, tzinfo=UTC)))
    )

    # Unauthenticated and malformed tokens -> 401 problem+json
    unauth = client.get("/v1/progress")
    assert unauth.status_code == 401
    assert unauth.headers["content-type"].startswith("application/problem+json")

    bad_tok = client.get("/v1/progress", headers={"Authorization": "Bearer not-a-valid-token"})
    assert bad_tok.status_code == 401

    # Bridge token cannot access learner routes; learner token cannot ingest
    bridge_on_learner = client.get("/v1/progress", headers=HEADERS_BRIDGE)
    assert bridge_on_learner.status_code == 403
    assert bridge_on_learner.json()["code"] == "insufficient_scope"

    learner_on_bridge = client.post(
        "/v1/attempts/ingest",
        headers={**HEADERS_A, "Idempotency-Key": "sec-1"},
        json={},
    )
    assert learner_on_bridge.status_code == 403

    # Seed Learner A consent, attempt, recurrence, and rehearsal
    client.post(
        "/v1/consents",
        headers={**HEADERS_A, "Idempotency-Key": "c-a"},
        json={
            "consent_id": CONSENT_A,
            "capture_mode": "consented_conversation",
            "policy_version": "consent_v1",
            "all_participants_consented": True,
            "attested_at": "2026-10-24T09:00:00Z",
        },
    )
    ingest = client.post(
        "/v1/attempts/ingest",
        headers={**HEADERS_BRIDGE, "Idempotency-Key": "ing-a"},
        json={
            "learner_id": LEARNER_A,
            "consent_id": CONSENT_A,
            "bee_recording_reference": "sec-bee-001",
            "recorded_at": "2026-10-24T09:15:00Z",
            "capture_mode": "consented_conversation",
            "situation": "repair_appointment",
            "learner_goal": "Plan reparatie.",
            "segments": [
                {
                    "segment_id": "s1",
                    "speaker_label": "self",
                    "text": "Goedemiddag, ik wil een afspraak voor een reparatie.",
                    "start_ms": 0,
                    "end_ms": 2500,
                    "asr_confidence": 0.92,
                },
                {
                    "segment_id": "s2",
                    "speaker_label": "other",
                    "text": "Geheime naam van andere spreker op Dorpsstraat 99.",
                    "start_ms": 2700,
                    "end_ms": 4500,
                    "asr_confidence": 0.95,
                },
            ],
        },
    ).json()
    attempt_id = ingest["attempt_id"]
    breakdown_id = ingest["attempt"]["breakdowns"][0]["breakdown_id"]

    # Confirm attempt for Learner A and create recurrence + rehearsal
    sec_decisions = [
        {
            "breakdown_id": b["breakdown_id"],
            "decision": "confirmed" if idx == 0 else "rejected",
        }
        for idx, b in enumerate(ingest["attempt"]["breakdowns"])
    ]
    rev_res = client.post(
        f"/v1/attempts/{attempt_id}/review",
        headers={**HEADERS_A, "Idempotency-Key": "rev-a"},
        json={
            "expected_version": 1,
            "breakdown_decisions": sec_decisions,
            "criterion_corrections": [
                {
                    "criterion_id": "goal_stated",
                    "result": "satisfied",
                    "self_evidence_excerpt": "ik wil een afspraak voor een reparatie",
                },
                {
                    "criterion_id": "appointment_confirmed",
                    "result": "not_satisfied",
                    "self_evidence_excerpt": "ik wil een afspraak voor een reparatie",
                },
            ],
            "confirm_attempt": True,
        },
    )
    assert rev_res.status_code == 200
    rec_id = client.post(
        "/v1/recurrences",
        headers={**HEADERS_A, "Idempotency-Key": "rec-a"},
        json={
            "attempt_id": attempt_id,
            "scheduled_for": "2026-10-25T14:00:00Z",
            "rehearsal_at": "2026-10-25T13:00:00Z",
            "time_zone": "Europe/Amsterdam",
        },
    ).json()["recurrence_id"]
    reh_id = client.post(
        f"/v1/recurrences/{rec_id}/rehearsal",
        headers={**HEADERS_A, "Idempotency-Key": "reh-a"},
        json={"breakdown_id": breakdown_id},
    ).json()["rehearsal_id"]

    # Learner B is strictly denied on every Learner A resource (AC-NT-019)
    assert client.get(f"/v1/attempts/{attempt_id}", headers=HEADERS_B).status_code == 403
    assert (
        client.post(
            f"/v1/attempts/{attempt_id}/exclude",
            headers={**HEADERS_B, "Idempotency-Key": "b-ex"},
            json={"expected_version": 3},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/recurrences/{rec_id}/calendar-link",
            headers={**HEADERS_B, "Idempotency-Key": "b-cal"},
            json={"calendar_event_reference": "hijack"},
        ).status_code
        == 403
    )
    assert client.get(f"/v1/rehearsals/{reh_id}", headers=HEADERS_B).status_code == 403
    assert (
        client.post(
            f"/v1/rehearsals/{reh_id}/turns",
            headers={**HEADERS_B, "Idempotency-Key": "b-turn"},
            json={
                "turn_event_id": "b-evt",
                "turn_id": "turn_1",
                "sequence": 1,
                "transcript": "hallo",
                "confidence": 0.9,
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/consents/{CONSENT_A}/revoke",
            headers={**HEADERS_B, "Idempotency-Key": "b-rev"},
        ).status_code
        == 403
    )

    # Extra unknown fields such as other_speaker_transcript are rejected with 422
    extra_field = client.post(
        f"/v1/rehearsals/{reh_id}/turns",
        headers={**HEADERS_A, "Idempotency-Key": "extra-1"},
        json={
            "turn_event_id": "evt-extra",
            "turn_id": "turn_1",
            "sequence": 1,
            "transcript": "hallo",
            "confidence": 0.9,
            "other_speaker_transcript": "forbidden",
        },
    )
    assert extra_field.status_code == 422
    assert extra_field.json()["code"] == "validation_failed"

    # Without recent_auth, export and account deletion are forbidden
    assert client.get("/v1/export", headers=HEADERS_A).status_code == 403
    assert (
        client.delete(
            "/v1/account",
            headers={**HEADERS_A, "Idempotency-Key": "del-no-recent"},
        ).status_code
        == 403
    )

    # Learner A revokes consent -> deletes attempt + rehearsal and blocks new ingestion (AC-NT-017)
    revoked = client.post(
        f"/v1/consents/{CONSENT_A}/revoke",
        headers={**HEADERS_A, "Idempotency-Key": "a-rev"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["deletion_job"]["status"] == "completed"
    assert client.get(f"/v1/attempts/{attempt_id}", headers=HEADERS_A).status_code == 404
    assert client.get(f"/v1/rehearsals/{reh_id}", headers=HEADERS_A).status_code == 404

    blocked_ingest = client.post(
        "/v1/attempts/ingest",
        headers={**HEADERS_BRIDGE, "Idempotency-Key": "ing-after-rev"},
        json={
            "learner_id": LEARNER_A,
            "consent_id": CONSENT_A,
            "bee_recording_reference": "sec-bee-002",
            "recorded_at": "2026-10-24T09:30:00Z",
            "capture_mode": "consented_conversation",
            "situation": "repair_appointment",
            "learner_goal": "Plan reparatie.",
            "segments": [
                {
                    "segment_id": "s1",
                    "speaker_label": "self",
                    "text": "Goedemiddag, ik wil een afspraak.",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "asr_confidence": 0.92,
                }
            ],
        },
    )
    assert blocked_ingest.status_code == 403
    assert blocked_ingest.json()["code"] == "consent_revoked"
