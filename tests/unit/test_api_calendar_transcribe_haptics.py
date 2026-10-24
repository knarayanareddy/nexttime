from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from nexttime_api import ApiRuntimeState, create_app

LEARNER_A = "01000000000000000000000002"
LEARNER_B = "01000000000000000000000077"
CONSENT_ID = "01000000000000000000000003"
BRIDGE_ID = "01000000000000000000000088"

HEADERS_A = {"Authorization": f"Bearer learner:{LEARNER_A}"}
HEADERS_A_RECENT = {"Authorization": f"Bearer learner-recent:{LEARNER_A}"}
HEADERS_B = {"Authorization": f"Bearer learner:{LEARNER_B}"}
BRIDGE_HEADERS = {"Authorization": f"Bearer bridge:{BRIDGE_ID}"}


def _setup_confirmed_repair_attempt(client: TestClient) -> tuple[str, str]:
    res_consent = client.post(
        "/v1/consents",
        headers={**HEADERS_A, "Idempotency-Key": "cal-consent-1"},
        json={
            "consent_id": CONSENT_ID,
            "capture_mode": "consented_conversation",
            "policy_version": "consent_v1",
            "all_participants_consented": True,
            "attested_at": "2026-10-24T09:00:00Z",
        },
    )
    assert res_consent.status_code == 201

    res_ingest = client.post(
        "/v1/attempts/ingest",
        headers={**BRIDGE_HEADERS, "Idempotency-Key": "cal-ingest-1"},
        json={
            "learner_id": LEARNER_A,
            "consent_id": CONSENT_ID,
            "bee_recording_reference": "bee-cal-001",
            "recorded_at": "2026-10-24T09:15:00Z",
            "capture_mode": "consented_conversation",
            "device_type": "bee_device",
            "provenance": "fixture",
            "situation": "repair_appointment",
            "learner_goal": "Plan een reparatieafspraak voor maandag.",
            "segments": [
                {
                    "segment_id": "seg-1",
                    "speaker_label": "self",
                    "text": "Goedemiddag, ik wil een afspraak voor een reparatie maken.",
                    "start_ms": 0,
                    "end_ms": 3000,
                    "asr_confidence": 0.94,
                    "speaker_confidence": 0.96,
                },
                {
                    "segment_id": "seg-2",
                    "speaker_label": "other",
                    "text": "Maandag om tien uur komt de monteur.",
                    "start_ms": 4200,
                    "end_ms": 6200,
                    "asr_confidence": 0.95,
                    "speaker_confidence": 0.97,
                },
            ],
        },
    )
    assert res_ingest.status_code == 201
    attempt: dict[str, Any] = res_ingest.json()["attempt"]
    attempt_id = str(attempt["attempt_id"])
    conf_breakdowns = [b for b in attempt["breakdowns"] if b["code"] == "confirmation_missing"]
    assert conf_breakdowns, f"Expected confirmation_missing in {attempt['breakdowns']}"
    target_breakdown_id = str(conf_breakdowns[0]["breakdown_id"])
    decisions = [
        {
            "breakdown_id": b["breakdown_id"],
            "decision": ("confirmed" if b["breakdown_id"] == target_breakdown_id else "rejected"),
            "redact_excerpt": False,
        }
        for b in attempt["breakdowns"]
    ]

    res_review = client.post(
        f"/v1/attempts/{attempt_id}/review",
        headers={**HEADERS_A, "Idempotency-Key": "cal-review-1", "If-Match": '"1"'},
        json={
            "expected_version": 1,
            "confirm_attempt": True,
            "breakdown_decisions": decisions,
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
        },
    )
    assert res_review.status_code == 200
    return attempt_id, target_breakdown_id


def test_end_to_end_calendar_sync_card_rehearsal_haptics_and_chunk_transcription() -> None:
    clock = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    client = TestClient(create_app(ApiRuntimeState(clock=clock)))
    attempt_id, breakdown_id = _setup_confirmed_repair_attempt(client)

    # 1. Invalid time zone or malformed ICS rejected with 422 Problem JSON
    bad_tz = client.post(
        "/v1/calendar/ics-sync",
        headers={**HEADERS_A, "Idempotency-Key": "ics-bad-tz"},
        json={"ics_payload": "BEGIN:VCALENDAR\nEND:VCALENDAR", "default_time_zone": "Bad/Zone"},
    )
    assert bad_tz.status_code == 422
    assert bad_tz.json()["code"] == "invalid_time_zone"

    bad_ics = client.post(
        "/v1/calendar/ics-sync",
        headers={**HEADERS_A, "Idempotency-Key": "ics-bad-body"},
        json={"ics_payload": "NOT_A_VALID_CALENDAR_PAYLOAD"},
    )
    assert bad_ics.status_code == 422
    assert bad_ics.json()["code"] == "invalid_ics_header"

    # 2. Sync ICS feed containing a repair visit (quiet hours) + a sensitive doctor appointment
    ics_feed = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:repair-apt-777\r\n"
        "SUMMARY:Reparatieafspraak cv-ketel met Jan Jansen\r\n"
        "DTSTART;TZID=Europe/Amsterdam:20261026T223000\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:doctor-apt-888\r\n"
        "SUMMARY:Afspraak huisarts in ziekenhuis\r\n"
        "DTSTART;TZID=Europe/Amsterdam:20261026T140000\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    sync_res = client.post(
        "/v1/calendar/ics-sync",
        headers={**HEADERS_A, "Idempotency-Key": "ics-sync-ok"},
        json={"ics_payload": ics_feed, "default_time_zone": "Europe/Amsterdam"},
    )
    assert sync_res.status_code == 200
    sync_body = sync_res.json()
    assert len(sync_body["candidates"]) == 1
    assert sync_body["excluded_sensitive_count"] == 1
    assert "Jan Jansen" not in str(sync_body)
    candidate_id = sync_body["candidates"][0]["candidate_id"]
    assert sync_body["candidates"][0]["matched_attempt_id"] == attempt_id

    # Idempotent replay returns cached 200
    replay_sync = client.post(
        "/v1/calendar/ics-sync",
        headers={**HEADERS_A, "Idempotency-Key": "ics-sync-ok"},
        json={"ics_payload": ics_feed, "default_time_zone": "Europe/Amsterdam"},
    )
    assert replay_sync.status_code == 200
    assert replay_sync.json() == sync_body

    # Candidate listing is learner-scoped
    assert len(client.get("/v1/calendar/candidates", headers=HEADERS_A).json()["items"]) == 1
    assert client.get("/v1/calendar/candidates", headers=HEADERS_B).json()["items"] == []

    # 3. Quiet-hours candidate confirmation without notification_behavior fails 422
    missing_quiet = client.post(
        f"/v1/calendar/candidates/{candidate_id}/confirm",
        headers={**HEADERS_A, "Idempotency-Key": "confirm-no-quiet"},
        json={},
    )
    assert missing_quiet.status_code == 422
    assert missing_quiet.json()["code"] == "quiet_hours_notification_choice_required"

    # Cross-learner confirmation fails 403; unknown candidate fails 404
    forbidden_confirm = client.post(
        f"/v1/calendar/candidates/{candidate_id}/confirm",
        headers={**HEADERS_B, "Idempotency-Key": "confirm-other"},
        json={"notification_behavior": "open_template_only"},
    )
    assert forbidden_confirm.status_code == 403

    not_found_confirm = client.post(
        "/v1/calendar/candidates/nonexistent/confirm",
        headers={**HEADERS_A, "Idempotency-Key": "confirm-404"},
        json={"notification_behavior": "open_template_only"},
    )
    assert not_found_confirm.status_code == 404

    # Explicit confirmation queues PreMeetingRehearsalCard AND creates linked recurrence
    confirm_res = client.post(
        f"/v1/calendar/candidates/{candidate_id}/confirm",
        headers={**HEADERS_A, "Idempotency-Key": "confirm-ok"},
        json={"notification_behavior": "open_template_only"},
    )
    assert confirm_res.status_code == 201
    card = confirm_res.json()
    assert card["event_source"] == "explicit_calendar_link"
    assert card["estimated_seconds"] == 90
    recurrence_id = card["recurrence_id"]

    cards_list = client.get("/v1/rehearsal-cards", headers=HEADERS_A).json()["items"]
    assert len(cards_list) == 1
    assert cards_list[0]["card_id"] == card["card_id"]

    # 4. Launch bounded 90-second rehearsal directly from the confirmed pre-meeting card!
    reh_res = client.post(
        f"/v1/recurrences/{recurrence_id}/rehearsal",
        headers={**HEADERS_A, "Idempotency-Key": "reh-from-card"},
        json={"breakdown_id": breakdown_id},
    )
    assert reh_res.status_code == 201
    rehearsal_id = reh_res.json()["rehearsal_id"]

    # 5. Emit rehearsal haptics: Bee wearable target rejected (422), iPhone PTT start/release work
    bee_haptic = client.post(
        f"/v1/rehearsals/{rehearsal_id}/haptics",
        headers={**HEADERS_A, "Idempotency-Key": "hap-bee"},
        json={
            "source_event_id": "hap-evt-bee",
            "trigger": "ptt_recording_started",
            "target_device": "bee_wearable",
        },
    )
    assert bee_haptic.status_code == 422
    assert bee_haptic.json()["code"] == "unverified_bee_haptic_command_prohibited"

    ptt_start = client.post(
        f"/v1/rehearsals/{rehearsal_id}/haptics",
        headers={**HEADERS_A, "Idempotency-Key": "hap-start"},
        json={
            "source_event_id": "hap-evt-1",
            "trigger": "ptt_recording_started",
            "turn_id": "turn_1",
        },
    )
    assert ptt_start.status_code == 200
    assert ptt_start.json()["pattern"] == "STEADY_BUZZ"
    assert ptt_start.json()["emitted"] is True

    ptt_release = client.post(
        f"/v1/rehearsals/{rehearsal_id}/haptics",
        headers={**HEADERS_A, "Idempotency-Key": "hap-release"},
        json={
            "source_event_id": "hap-evt-2",
            "trigger": "ptt_recording_released",
            "turn_id": "turn_1",
        },
    )
    assert ptt_release.status_code == 200
    assert ptt_release.json()["pattern"] == "SHORT_DOUBLE"

    # 6. Consented PTT audio chunk transcription: continuous stream blocked (403), unconsented (403)
    pcm_b64 = base64.b64encode(b"\x10\x00" * 600).decode("ascii")

    covert_tx = client.post(
        f"/v1/rehearsals/{rehearsal_id}/transcribe-chunks",
        headers={**HEADERS_A, "Idempotency-Key": "tx-covert"},
        json={
            "turn_event_id": "turn-evt-covert",
            "streaming_transcription_consented": True,
            "capture_origin": "bee_wearable",
            "continuous_stream": True,
            "chunks": [{"sequence_id": 1, "pcm_base64": pcm_b64}],
        },
    )
    assert covert_tx.status_code == 403
    assert covert_tx.json()["code"] == "continuous_microphone_streaming_prohibited"

    no_consent_tx = client.post(
        f"/v1/rehearsals/{rehearsal_id}/transcribe-chunks",
        headers={**HEADERS_A, "Idempotency-Key": "tx-noconsent"},
        json={
            "turn_event_id": "turn-evt-noconsent",
            "streaming_transcription_consented": False,
            "chunks": [{"sequence_id": 1, "pcm_base64": pcm_b64}],
        },
    )
    assert no_consent_tx.status_code == 403

    bad_b64_tx = client.post(
        f"/v1/rehearsals/{rehearsal_id}/transcribe-chunks",
        headers={**HEADERS_A, "Idempotency-Key": "tx-badb64"},
        json={
            "turn_event_id": "turn-evt-badb64",
            "streaming_transcription_consented": True,
            "chunks": [{"sequence_id": 1, "pcm_base64": "***not_base64***"}],
        },
    )
    assert bad_b64_tx.status_code == 422
    assert bad_b64_tx.json()["code"] == "invalid_pcm_base64"

    ok_tx = client.post(
        f"/v1/rehearsals/{rehearsal_id}/transcribe-chunks",
        headers={**HEADERS_A, "Idempotency-Key": "tx-ok"},
        json={
            "turn_event_id": "turn-evt-1",
            "streaming_transcription_consented": True,
            "chunks": [
                {"sequence_id": 1, "pcm_base64": pcm_b64},
                {"sequence_id": 2, "pcm_base64": pcm_b64},
            ],
        },
    )
    assert ok_tx.status_code == 200
    tx_body = ok_tx.json()
    assert tx_body["retained_pcm_bytes"] == 0
    assert len(tx_body["chunk_receipts"]) == 2
    assert tx_body["provider"] == "aws_transcribe_streaming"
    assert tx_body["provenance"] == "fixture"

    # Idempotent replay of transcribe-chunks returns cached 200
    replay_tx = client.post(
        f"/v1/rehearsals/{rehearsal_id}/transcribe-chunks",
        headers={**HEADERS_A, "Idempotency-Key": "tx-ok"},
        json={
            "turn_event_id": "turn-evt-1",
            "streaming_transcription_consented": True,
            "chunks": [
                {"sequence_id": 1, "pcm_base64": pcm_b64},
                {"sequence_id": 2, "pcm_base64": pcm_b64},
            ],
        },
    )
    assert replay_tx.status_code == 200
    assert replay_tx.json() == tx_body

    # 7. Account deletion purges calendar candidates and queued rehearsal cards (AC-NT-018)
    del_res = client.delete(
        "/v1/account",
        headers={**HEADERS_A_RECENT, "Idempotency-Key": "del-acct-cal"},
    )
    assert del_res.status_code == 200
    assert client.get("/v1/calendar/candidates", headers=HEADERS_A).json()["items"] == []
    assert client.get("/v1/rehearsal-cards", headers=HEADERS_A).json()["items"] == []
