from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from nexttime_api import ApiRuntimeState, Principal, create_app

LEARNER_A = "01000000000000000000000002"
CONSENT_ID = "01000000000000000000000003"
BRIDGE_ID = "01000000000000000000000088"

LEARNER_HEADERS = {"Authorization": f"Bearer learner:{LEARNER_A}"}
LEARNER_RECENT_HEADERS = {"Authorization": f"Bearer learner-recent:{LEARNER_A}"}
BRIDGE_HEADERS = {"Authorization": f"Bearer bridge:{BRIDGE_ID}"}


def _client(fixed_now: datetime | None = None) -> TestClient:
    clock = fixed_now or datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    state = ApiRuntimeState(clock=clock)
    return TestClient(create_app(state))


def _create_consent(client: TestClient) -> dict[str, Any]:
    res = client.post(
        "/v1/consents",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "consent-create-1"},
        json={
            "consent_id": CONSENT_ID,
            "capture_mode": "consented_conversation",
            "policy_version": "consent_v1",
            "all_participants_consented": True,
            "attested_at": "2026-10-24T09:00:00Z",
        },
    )
    assert res.status_code == 201
    return dict(res.json())


def _ingest_repair_attempt(
    client: TestClient,
    *,
    ref: str,
    recorded_at: str,
    self_text: str,
    recurrence_id: str | None = None,
    idem_key: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "learner_id": LEARNER_A,
        "consent_id": CONSENT_ID,
        "bee_recording_reference": ref,
        "recorded_at": recorded_at,
        "capture_mode": "consented_conversation",
        "device_type": "bee_device",
        "provenance": "fixture",
        "situation": "repair_appointment",
        "learner_goal": "Plan een reparatieafspraak voor vrijdag.",
        "segments": [
            {
                "segment_id": "seg-1",
                "speaker_label": "self",
                "text": self_text,
                "start_ms": 0,
                "end_ms": 3000,
                "asr_confidence": 0.92,
                "speaker_confidence": 0.95,
            },
            {
                "segment_id": "seg-2",
                "speaker_label": "other",
                "text": "De monteur Jan Jansen komt om tien uur.",
                "start_ms": 3200,
                "end_ms": 5000,
                "asr_confidence": 0.95,
                "speaker_confidence": 0.96,
            },
        ],
    }
    if recurrence_id is not None:
        payload["recurrence_id"] = recurrence_id

    res = client.post(
        "/v1/attempts/ingest",
        headers={**BRIDGE_HEADERS, "Idempotency-Key": idem_key},
        json=payload,
    )
    assert res.status_code in {200, 201}
    return dict(res.json())


def test_full_rehearsal_session_and_learner_progress_history_flow() -> None:
    client = _client()
    assert client.get("/healthz").json()["status"] == "ok"

    _create_consent(client)

    # 1. Ingest initial attempt where appointment_confirmed is NOT satisfied
    ingested_1 = _ingest_repair_attempt(
        client,
        ref="bee-rec-101",
        recorded_at="2026-10-24T09:15:00Z",
        self_text="Goedemiddag, ik wil een afspraak voor een reparatie maken.",
        idem_key="ingest-101",
    )
    attempt_1_id = ingested_1["attempt_id"]
    attempt_1 = ingested_1["attempt"]
    assert "Jan Jansen" not in str(attempt_1)
    breakdown_1_id = attempt_1["breakdowns"][0]["breakdown_id"]

    # List & get attempts
    listed = client.get("/v1/attempts?limit=1", headers=LEARNER_HEADERS).json()
    assert len(listed["items"]) == 1
    fetched = client.get(f"/v1/attempts/{attempt_1_id}", headers=LEARNER_HEADERS).json()
    assert fetched["attempt_id"] == attempt_1_id

    # 2. Confirm attempt 1 with explicit criterion states so breakdown is confirmed
    b1_decisions = [
        {
            "breakdown_id": b["breakdown_id"],
            "decision": "confirmed" if idx == 0 else "rejected",
            "redact_excerpt": False,
        }
        for idx, b in enumerate(attempt_1["breakdowns"])
    ]
    review_1 = client.post(
        f"/v1/attempts/{attempt_1_id}/review",
        headers={
            **LEARNER_HEADERS,
            "Idempotency-Key": "rev-101",
            "If-Match": '"1"',
        },
        json={
            "expected_version": 1,
            "breakdown_decisions": b1_decisions,
            "criterion_corrections": [
                {
                    "criterion_id": "goal_stated",
                    "result": "satisfied",
                    "self_evidence_excerpt": "ik wil een afspraak voor een reparatie maken",
                    "self_evidence_start_ms": 0,
                    "self_evidence_end_ms": 3000,
                },
                {
                    "criterion_id": "appointment_confirmed",
                    "result": "not_satisfied",
                    "self_evidence_excerpt": "ik wil een afspraak voor een reparatie maken",
                    "self_evidence_start_ms": 0,
                    "self_evidence_end_ms": 3000,
                },
            ],
            "confirm_attempt": True,
        },
    )
    assert review_1.status_code == 200
    assert review_1.json()["attempt"]["status"] == "confirmed"

    # Snapshot authentic skills before rehearsal
    progress_before = client.get("/v1/progress", headers=LEARNER_HEADERS).json()
    skills_before = progress_before["skill_states"]
    assert len(skills_before) >= 1

    # 3. Create recurrence & link calendar event
    rec_res = client.post(
        "/v1/recurrences",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "rec-101"},
        json={
            "attempt_id": attempt_1_id,
            "scheduled_for": "2026-10-25T13:00:00Z",
            "rehearsal_at": "2026-10-25T12:00:00Z",
            "time_zone": "Europe/Amsterdam",
            "learner_confirmed": True,
            "notification_behavior": "local_notification",
        },
    )
    assert rec_res.status_code == 201
    recurrence_id = rec_res.json()["recurrence_id"]
    assert rec_res.json()["quiet_hours"] is False

    cal_res = client.post(
        f"/v1/recurrences/{recurrence_id}/calendar-link",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "cal-101"},
        json={"calendar_event_reference": "eventkit-opaque-ref-42"},
    )
    assert cal_res.status_code == 200
    assert cal_res.json()["source"] == "explicit_calendar_link"

    # 4. Generate 90-second rehearsal session
    reh_res = client.post(
        f"/v1/recurrences/{recurrence_id}/rehearsal",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "reh-101"},
        json={"breakdown_id": breakdown_1_id},
    )
    assert reh_res.status_code == 201
    reh = reh_res.json()
    rehearsal_id = reh["rehearsal_id"]
    assert reh["estimated_seconds"] <= 90
    assert reh["model_generated"] is False
    assert reh["turns"][0]["counterpart_audio"]["signed_url"].startswith(
        "https://audio.nexttime.local/v1/counterpart-clips/"
    )

    # Fetch rehearsal state via GET
    reh_get = client.get(f"/v1/rehearsals/{rehearsal_id}", headers=LEARNER_HEADERS)
    assert reh_get.status_code == 200
    assert reh_get.json()["state"] == "scheduled"

    # 5. Turn 1: low-confidence miss triggers repair support, then Dutch speech succeeds
    miss_turn = client.post(
        f"/v1/rehearsals/{rehearsal_id}/turns",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "turn-miss-1"},
        json={
            "turn_event_id": "evt-miss-1",
            "turn_id": "t1",
            "sequence": 1,
            "transcript": "uhm hallo",
            "confidence": 0.55,
            "mode": "speech",
        },
    )
    assert miss_turn.status_code == 200
    assert miss_turn.json()["support"] == "repair"
    assert miss_turn.json()["raw_audio_deleted"] is True
    assert miss_turn.json()["authentic_mastery_updated"] is False

    ok_turn_1 = client.post(
        f"/v1/rehearsals/{rehearsal_id}/turns",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "turn-ok-1"},
        json={
            "turn_event_id": "evt-ok-1",
            "turn_id": "t1",
            "sequence": 1,
            "transcript": "Ik wil een afspraak voor de reparatie.",
            "confidence": 0.91,
            "mode": "speech",
        },
    )
    assert ok_turn_1.status_code == 200
    assert ok_turn_1.json()["mapped_function"] == "goal_stated"
    assert ok_turn_1.json()["current_turn_id"] == "t2"

    # Duplicate turn_event_id replay is idempotent
    dup_turn_1 = client.post(
        f"/v1/rehearsals/{rehearsal_id}/turns",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "turn-ok-1-dup-event"},
        json={
            "turn_event_id": "evt-ok-1",
            "turn_id": "t1",
            "sequence": 1,
            "transcript": "Ik wil een afspraak voor de reparatie.",
            "confidence": 0.91,
            "mode": "speech",
        },
    )
    assert dup_turn_1.status_code == 200
    assert dup_turn_1.json()["duplicate"] is True

    # Submit Turn 2 in non-speaking tap mode
    ok_turn_2 = client.post(
        f"/v1/rehearsals/{rehearsal_id}/turns",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "turn-ok-2"},
        json={
            "turn_event_id": "evt-ok-2",
            "turn_id": "t2",
            "sequence": 2,
            "phrase_id": "confirm_time",
            "confidence": 0.95,
            "mode": "tap",
        },
    )
    assert ok_turn_2.status_code == 200
    assert ok_turn_2.json()["state"] == "completed"
    assert ok_turn_2.json()["non_speaking_practice"] is True

    # Finalize rehearsal and verify authentic skill mastery NEVER changed (FR-NT-011 / AC-NT-015)
    completed = client.post(
        f"/v1/rehearsals/{rehearsal_id}/complete",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "reh-complete-1"},
        json={"final_turn_sequence": 2},
    )
    assert completed.status_code == 200
    assert completed.json()["state"] == "completed"

    progress_after_rehearsal = client.get("/v1/progress", headers=LEARNER_HEADERS).json()
    assert progress_after_rehearsal["skill_states"] == skills_before
    assert len(progress_after_rehearsal["rehearsal_history"]) == 1

    # 6. Advance clock to follow-up day, ingest & confirm linked attempt -> OutcomeComparison!
    client.app.state.runtime._fixed_clock = datetime(2026, 10, 25, 13, 30, tzinfo=UTC)  # type: ignore[union-attr]
    ingested_2 = _ingest_repair_attempt(
        client,
        ref="bee-rec-102",
        recorded_at="2026-10-25T13:10:00Z",
        self_text="Goedemiddag, ik bel voor de reparatie en ik bevestig vrijdag om tien uur.",
        recurrence_id=recurrence_id,
        idem_key="ingest-102",
    )
    attempt_2_id = ingested_2["attempt_id"]
    attempt_2 = ingested_2["attempt"]
    b2_decisions = [
        {"breakdown_id": b["breakdown_id"], "decision": "rejected"} for b in attempt_2["breakdowns"]
    ]
    review_2 = client.post(
        f"/v1/attempts/{attempt_2_id}/review",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "rev-102"},
        json={
            "expected_version": 1,
            "breakdown_decisions": b2_decisions,
            "criterion_corrections": [
                {
                    "criterion_id": "goal_stated",
                    "result": "satisfied",
                    "self_evidence_excerpt": "ik bel voor de reparatie",
                    "self_evidence_start_ms": 0,
                    "self_evidence_end_ms": 1500,
                },
                {
                    "criterion_id": "appointment_confirmed",
                    "result": "satisfied",
                    "self_evidence_excerpt": "ik bevestig vrijdag om tien uur",
                    "self_evidence_start_ms": 1500,
                    "self_evidence_end_ms": 3000,
                },
            ],
            "confirm_attempt": True,
        },
    )
    assert review_2.status_code == 200

    progress_final = client.get("/v1/progress", headers=LEARNER_HEADERS).json()
    assert progress_final["causal_claim"] is False
    assert progress_final["gamified_streaks"] is False
    assert len(progress_final["authentic_attempts"]) == 2
    assert len(progress_final["comparisons"]) == 1
    assert progress_final["comparisons"][0]["functional_progress"] == "improved"
    assert len(progress_final["functional_wins"]) >= 1


def test_idempotency_problem_details_quiet_hours_early_exit_and_account_deletion() -> None:
    state = ApiRuntimeState(clock=datetime(2026, 10, 24, 10, 0, tzinfo=UTC))
    state.authenticator.register_token(
        "custom-learner-token",
        Principal(actor_id=LEARNER_A, role="learner", recent_auth=True),
    )
    client = TestClient(create_app(state))

    # Missing Idempotency-Key -> 400 application/problem+json
    missing_idem = client.post(
        "/v1/consents",
        headers={"Authorization": "Bearer custom-learner-token"},
        json={
            "consent_id": CONSENT_ID,
            "capture_mode": "consented_conversation",
            "policy_version": "consent_v1",
            "all_participants_consented": True,
            "attested_at": "2026-10-24T09:00:00Z",
        },
    )
    assert missing_idem.status_code == 400
    assert missing_idem.headers["content-type"].startswith("application/problem+json")
    assert missing_idem.json()["code"] == "idempotency_key_required"

    _create_consent(client)

    # Reused Idempotency-Key with different payload -> 409 idempotency_key_reused
    conflict_idem = client.post(
        "/v1/consents",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "consent-create-1"},
        json={
            "consent_id": CONSENT_ID,
            "capture_mode": "self_memo",
            "policy_version": "consent_v1",
            "all_participants_consented": False,
            "attested_at": "2026-10-24T09:00:00Z",
        },
    )
    assert conflict_idem.status_code == 409
    assert conflict_idem.json()["code"] == "idempotency_key_reused"

    # Replay exact same Idempotency-Key + payload -> returns cached 201
    replay_idem = client.post(
        "/v1/consents",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "consent-create-1"},
        json={
            "consent_id": CONSENT_ID,
            "capture_mode": "consented_conversation",
            "policy_version": "consent_v1",
            "all_participants_consented": True,
            "attested_at": "2026-10-24T09:00:00Z",
        },
    )
    assert replay_idem.status_code == 201

    ingested = _ingest_repair_attempt(
        client,
        ref="bee-rec-201",
        recorded_at="2026-10-24T09:20:00Z",
        self_text="Goedemiddag, ik wil een afspraak voor een reparatie.",
        idem_key="ingest-201",
    )
    aid = ingested["attempt_id"]
    bid = ingested["attempt"]["breakdowns"][0]["breakdown_id"]

    # Stale version conflict -> 409 stale_version
    stale_rev = client.post(
        f"/v1/attempts/{aid}/review",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "rev-stale"},
        json={"expected_version": 99, "confirm_attempt": True},
    )
    assert stale_rev.status_code == 409
    assert stale_rev.json()["code"] == "stale_version"

    # Confirm with excerpt redaction
    b201_decisions = [
        {
            "breakdown_id": b["breakdown_id"],
            "decision": "confirmed" if idx == 0 else "rejected",
            "redact_excerpt": idx == 0,
        }
        for idx, b in enumerate(ingested["attempt"]["breakdowns"])
    ]
    ok_rev = client.post(
        f"/v1/attempts/{aid}/review",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "rev-ok-201"},
        json={
            "expected_version": 1,
            "breakdown_decisions": b201_decisions,
            "criterion_corrections": [
                {
                    "criterion_id": "goal_stated",
                    "result": "satisfied",
                    "self_evidence_excerpt": "ik wil een afspraak",
                },
                {
                    "criterion_id": "appointment_confirmed",
                    "result": "not_satisfied",
                    "self_evidence_excerpt": "ik wil een afspraak",
                },
            ],
            "confirm_attempt": True,
        },
    )
    assert ok_rev.status_code == 200
    assert ok_rev.json()["attempt"]["breakdowns"][0]["self_evidence_excerpt"] == "[redacted]"

    # Quiet hours (22:30 local Amsterdam = 20:30Z in October CEST) requires explicit choice
    quiet_rec = client.post(
        "/v1/recurrences",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "rec-quiet"},
        json={
            "attempt_id": aid,
            "scheduled_for": "2026-10-24T21:30:00Z",
            "rehearsal_at": "2026-10-24T20:30:00Z",
            "time_zone": "Europe/Amsterdam",
            "learner_confirmed": True,
            "notification_behavior": "local_notification",
        },
    )
    assert quiet_rec.status_code == 201
    assert quiet_rec.json()["quiet_hours"] is True
    assert quiet_rec.json()["selection_required"] is True

    # Open template only during quiet hours succeeds and allows early exit without penalty
    open_only_rec = client.post(
        "/v1/recurrences",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "rec-open-only"},
        json={
            "attempt_id": aid,
            "scheduled_for": "2026-10-24T21:45:00Z",
            "rehearsal_at": "2026-10-24T20:45:00Z",
            "time_zone": "Europe/Amsterdam",
            "learner_confirmed": True,
            "notification_behavior": "open_template_only",
        },
    )
    rec_id_2 = open_only_rec.json()["recurrence_id"]
    reh_2 = client.post(
        f"/v1/recurrences/{rec_id_2}/rehearsal",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "reh-202"},
        json={"breakdown_id": bid},
    ).json()
    early_exit = client.post(
        f"/v1/rehearsals/{reh_2['rehearsal_id']}/complete",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "reh-early-exit"},
        json={"final_turn_sequence": 0, "exit_early": True},
    )
    assert early_exit.status_code == 200
    assert early_exit.json()["state"] == "abandoned"

    # Export & delete account with recent_auth
    exported = client.get("/v1/export", headers=LEARNER_RECENT_HEADERS)
    assert exported.status_code == 200
    assert exported.json()["other_speaker_transcript_included"] is False

    deleted = client.delete(
        "/v1/account",
        headers={**LEARNER_RECENT_HEADERS, "Idempotency-Key": "acct-del-1"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "completed"
    assert client.get("/v1/attempts", headers=LEARNER_HEADERS).json()["items"] == []


def test_api_error_envelopes_pagination_exclusion_and_entrypoint() -> None:
    from datetime import timedelta

    from nexttime_api import IdempotencyStore
    from nexttime_api.main import app as default_app

    assert default_app.title == "NextTime API"

    # Idempotency 24h expiry check
    idem = IdempotencyStore()
    t0 = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    idem.save(
        actor_id=LEARNER_A,
        method="POST",
        path="/v1/consents",
        idempotency_key="k1",
        payload={"a": 1},
        status_code=201,
        response_body={"ok": True},
        now=t0,
    )
    assert (
        idem.check(
            actor_id=LEARNER_A,
            method="POST",
            path="/v1/consents",
            idempotency_key="k1",
            payload={"a": 2},
            now=t0 + timedelta(hours=25),
        )
        is None
    )

    client = _client()
    # Auth edge cases + schema validation 422
    assert client.get("/v1/attempts", headers={"Authorization": "Bearer   "}).status_code == 401
    bad_schema = client.post(
        "/v1/consents",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "bad-schema"},
        json={"consent_id": "short"},
    )
    assert bad_schema.status_code == 422
    assert "fields" in bad_schema.json()

    _create_consent(client)
    i1 = _ingest_repair_attempt(
        client,
        ref="bee-rec-301",
        recorded_at="2026-10-24T09:10:00Z",
        self_text="Goedemiddag, ik wil een afspraak voor een reparatie.",
        idem_key="ing-301",
    )
    i2 = _ingest_repair_attempt(
        client,
        ref="bee-rec-302",
        recorded_at="2026-10-24T09:20:00Z",
        self_text="Goedemiddag, ik wil een afspraak voor de monteur.",
        idem_key="ing-302",
    )
    # Pagination + invalid cursor
    p1 = client.get("/v1/attempts?limit=1", headers=LEARNER_HEADERS).json()
    assert p1["next_cursor"] == i1["attempt_id"]
    p2 = client.get(
        f"/v1/attempts?limit=1&cursor={p1['next_cursor']}", headers=LEARNER_HEADERS
    ).json()
    assert p2["items"][0]["attempt_id"] == i2["attempt_id"]
    assert client.get("/v1/attempts?cursor=bad-cursor", headers=LEARNER_HEADERS).status_code == 400

    # Exclude attempt with delete_now=True
    excl = client.post(
        f"/v1/attempts/{i2['attempt_id']}/exclude",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "excl-302"},
        json={"expected_version": 1, "reason": "learner_excluded", "delete_now": True},
    )
    assert excl.status_code == 200
    assert excl.json()["attempt"]["status"] == "excluded"
    assert (
        client.get(f"/v1/attempts/{i2['attempt_id']}", headers=LEARNER_HEADERS).status_code == 404
    )

    # Unconfirmed attempt cannot create recurrence
    unconf_rec = client.post(
        "/v1/recurrences",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "rec-unconf"},
        json={
            "attempt_id": i1["attempt_id"],
            "scheduled_for": "2026-10-25T13:00:00Z",
            "time_zone": "Europe/Amsterdam",
        },
    )
    assert unconf_rec.status_code == 422

    # Confirm i1 and test invalid timezone / past occurrence / out-of-order turn / incomplete finish
    b_decisions = [
        {
            "breakdown_id": b["breakdown_id"],
            "decision": "confirmed" if idx == 0 else "rejected",
        }
        for idx, b in enumerate(i1["attempt"]["breakdowns"])
    ]
    client.post(
        f"/v1/attempts/{i1['attempt_id']}/review",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "rev-301"},
        json={
            "expected_version": 1,
            "breakdown_decisions": b_decisions,
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
    assert (
        client.post(
            "/v1/recurrences",
            headers={**LEARNER_HEADERS, "Idempotency-Key": "rec-badtz"},
            json={
                "attempt_id": i1["attempt_id"],
                "scheduled_for": "2026-10-25T13:00:00Z",
                "time_zone": "Invalid/Zone",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/recurrences",
            headers={**LEARNER_HEADERS, "Idempotency-Key": "rec-past"},
            json={
                "attempt_id": i1["attempt_id"],
                "scheduled_for": "2026-10-23T13:00:00Z",
                "time_zone": "Europe/Amsterdam",
            },
        ).status_code
        == 422
    )

    rec_ok = client.post(
        "/v1/recurrences",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "rec-301"},
        json={
            "attempt_id": i1["attempt_id"],
            "scheduled_for": "2026-10-25T13:00:00Z",
            "rehearsal_at": "2026-10-25T12:00:00Z",
            "time_zone": "Europe/Amsterdam",
        },
    ).json()
    reh_ok = client.post(
        f"/v1/recurrences/{rec_ok['recurrence_id']}/rehearsal",
        headers={**LEARNER_HEADERS, "Idempotency-Key": "reh-301"},
        json={"breakdown_id": i1["attempt"]["breakdowns"][0]["breakdown_id"]},
    ).json()
    rid = reh_ok["rehearsal_id"]

    assert (
        client.post(
            f"/v1/rehearsals/{rid}/turns",
            headers={**LEARNER_HEADERS, "Idempotency-Key": "bad-seq"},
            json={
                "turn_event_id": "evt-seq-9",
                "turn_id": "t1",
                "sequence": 9,
                "transcript": "hallo",
                "confidence": 0.9,
            },
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/v1/rehearsals/{rid}/complete",
            headers={**LEARNER_HEADERS, "Idempotency-Key": "bad-comp-seq"},
            json={"final_turn_sequence": 5},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/v1/rehearsals/{rid}/complete",
            headers={**LEARNER_HEADERS, "Idempotency-Key": "incomp-reh"},
            json={"final_turn_sequence": 0, "exit_early": False},
        ).status_code
        == 422
    )
