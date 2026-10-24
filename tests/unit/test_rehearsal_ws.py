from __future__ import annotations

import asyncio
import base64
import io
import json
import struct
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from fastapi.testclient import TestClient
from nexttime_api import ApiRuntimeState, create_app
from nexttime_model_gateway import (
    ModelGateway,
    ProviderInvocationRequest,
    ProviderInvocationResponse,
    TokenUsage,
)
from starlette.websockets import WebSocketDisconnect

from scripts.simulate_rehearsal import (
    load_pcm_chunks_from_file,
    run_simulator_session,
)
from scripts.simulate_rehearsal import (
    main as simulator_main,
)

LEARNER_A = "01000000000000000000000002"
LEARNER_B = "01000000000000000000000077"
BRIDGE_ID = "01000000000000000000000088"
CONSENT_ID = "01000000000000000000000003"

HEADERS_A = {"Authorization": f"Bearer learner:{LEARNER_A}"}
HEADERS_B = {"Authorization": f"Bearer learner:{LEARNER_B}"}
BRIDGE_HEADERS = {"Authorization": f"Bearer bridge:{BRIDGE_ID}"}

WS_SERVER_EVENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "session_ready": {
        "type": "object",
        "required": [
            "type",
            "rehearsal_id",
            "learner_id",
            "situation",
            "target_function",
            "state",
            "current_turn_id",
            "total_turns",
            "estimated_seconds",
            "supported_client_messages",
            "supported_server_messages",
            "provenance",
        ],
        "properties": {
            "type": {"const": "session_ready"},
            "estimated_seconds": {"type": "integer", "maximum": 120},
            "supported_client_messages": {
                "type": "array",
                "contains": {"enum": ["audio_chunk", "phrase_attempt", "request_counterpart"]},
            },
        },
    },
    "counterpart_audio_ready": {
        "type": "object",
        "required": [
            "type",
            "rehearsal_id",
            "turn_id",
            "counterpart_persona",
            "counterpart_text_nl",
            "counterpart_text_en",
            "expected_functions",
            "counterpart_audio",
            "state",
        ],
        "properties": {
            "type": {"const": "counterpart_audio_ready"},
            "counterpart_audio": {
                "type": "object",
                "required": ["turn_id", "signed_url", "expires_at"],
            },
        },
    },
    "turn_haptic": {
        "type": "object",
        "required": [
            "type",
            "event_id",
            "rehearsal_id",
            "trigger",
            "pattern",
            "sequence",
            "target_device",
            "emitted",
            "visual_caption_key",
            "voiceover_announcement",
        ],
        "properties": {
            "type": {"const": "turn_haptic"},
            "pattern": {"enum": ["SHORT_DOUBLE", "STEADY_BUZZ", None]},
            "target_device": {"enum": ["ios_rehearsal_client", "cli_rehearsal_simulator"]},
        },
    },
    "transcription_update": {
        "type": "object",
        "required": [
            "type",
            "rehearsal_id",
            "turn_event_id",
            "turn_id",
            "sequence_id",
            "is_partial",
            "transcript",
            "redacted",
            "confidence",
            "state",
            "retained_pcm_bytes",
            "raw_audio_deleted",
        ],
        "properties": {"type": {"const": "transcription_update"}},
    },
    "breakdown_summary": {
        "type": "object",
        "required": [
            "type",
            "rehearsal_id",
            "situation",
            "target_function",
            "state",
            "committed_turn_count",
            "minimized_excerpts",
            "other_speaker_transcript_retained",
            "criteria",
            "breakdowns",
            "model_version",
            "fallback_used",
            "raw_pcm_retained_bytes",
            "authentic_mastery_updated",
        ],
        "properties": {
            "type": {"const": "breakdown_summary"},
            "other_speaker_transcript_retained": {"const": False},
            "raw_pcm_retained_bytes": {"const": 0},
            "authentic_mastery_updated": {"const": False},
        },
    },
}


class _StubGatewayProvider:
    """Deterministic test ModelProviderPort verifying ModelGateway wiring on /v1/rehearsal/ws."""

    def invoke(self, request: ProviderInvocationRequest) -> ProviderInvocationResponse:
        excerpts = request.payload["self_excerpts"]
        first_text = str(excerpts[0]["text"])
        last_text = str(excerpts[-1]["text"])
        out_payload = {
            "schema_version": 1,
            "criteria": [
                {
                    "criterion_id": "goal_stated",
                    "result": "satisfied",
                    "excerpt_index": 0,
                    "self_evidence_excerpt": first_text,
                    "model_confidence": 0.93,
                },
                {
                    "criterion_id": "appointment_confirmed",
                    "result": "satisfied",
                    "excerpt_index": len(excerpts) - 1,
                    "self_evidence_excerpt": last_text,
                    "model_confidence": 0.91,
                },
            ],
            "breakdowns": [],
        }
        return ProviderInvocationResponse(
            payload=out_payload,
            provider_request_id="req-stub-1",
            model_id="stub-gateway-model-v1",
            latency_ms=12,
            token_usage=TokenUsage(input_tokens=60, output_tokens=45),
        )


def _create_confirmed_http_rehearsal(client: TestClient) -> str:
    assert (
        client.post(
            "/v1/consents",
            headers={**HEADERS_A, "Idempotency-Key": "ws-consent-1"},
            json={
                "consent_id": CONSENT_ID,
                "capture_mode": "consented_conversation",
                "policy_version": "consent_v1",
                "all_participants_consented": True,
                "attested_at": "2026-10-24T09:00:00Z",
            },
        ).status_code
        == 201
    )
    ingest = client.post(
        "/v1/attempts/ingest",
        headers={**BRIDGE_HEADERS, "Idempotency-Key": "ws-ingest-1"},
        json={
            "learner_id": LEARNER_A,
            "consent_id": CONSENT_ID,
            "bee_recording_reference": "bee-ws-ref-01",
            "recorded_at": "2026-10-24T09:15:00Z",
            "capture_mode": "consented_conversation",
            "device_type": "bee_device",
            "provenance": "fixture",
            "situation": "repair_appointment",
            "learner_goal": "Plan een reparatieafspraak voor vrijdag.",
            "segments": [
                {
                    "segment_id": "seg-1",
                    "speaker_label": "self",
                    "text": "Goedemiddag, ik wil een afspraak voor een reparatie maken.",
                    "start_ms": 0,
                    "end_ms": 3000,
                    "asr_confidence": 0.94,
                    "speaker_confidence": 0.96,
                }
            ],
        },
    ).json()["attempt"]
    attempt_id = str(ingest["attempt_id"])
    conf_b = next(b for b in ingest["breakdowns"] if b["code"] == "confirmation_missing")
    breakdown_id = str(conf_b["breakdown_id"])
    decisions = [
        {
            "breakdown_id": b["breakdown_id"],
            "decision": "confirmed" if b["breakdown_id"] == breakdown_id else "rejected",
            "redact_excerpt": False,
        }
        for b in ingest["breakdowns"]
    ]
    assert (
        client.post(
            f"/v1/attempts/{attempt_id}/review",
            headers={**HEADERS_A, "Idempotency-Key": "ws-rev-1", "If-Match": '"1"'},
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
        ).status_code
        == 200
    )
    rec = client.post(
        "/v1/recurrences",
        headers={**HEADERS_A, "Idempotency-Key": "ws-rec-1"},
        json={
            "attempt_id": attempt_id,
            "scheduled_for": "2026-10-25T13:00:00Z",
            "rehearsal_at": "2026-10-25T12:00:00Z",
            "time_zone": "Europe/Amsterdam",
            "learner_confirmed": True,
            "notification_behavior": "local_notification",
        },
    ).json()
    reh = client.post(
        f"/v1/recurrences/{rec['recurrence_id']}/rehearsal",
        headers={**HEADERS_A, "Idempotency-Key": "ws-reh-1"},
        json={"breakdown_id": breakdown_id},
    )
    assert reh.status_code == 201
    return str(reh.json()["rehearsal_id"])


def test_ws_connection_handshakes_auth_ownership_and_bee_haptic_rejection() -> None:
    clock = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    client = TestClient(create_app(ApiRuntimeState(clock=clock)))
    rehearsal_id = _create_confirmed_http_rehearsal(client)

    # 1. Missing token rejected on handshake
    with pytest.raises(WebSocketDisconnect) as exc_unauth:
        with client.websocket_connect("/v1/rehearsal/ws"):
            pass
    assert exc_unauth.value.code == 1008

    # 2. Bridge token rejected on learner-scoped WebSocket (SEC-NT-003)
    with pytest.raises(WebSocketDisconnect) as exc_bridge:
        with client.websocket_connect("/v1/rehearsal/ws", headers=BRIDGE_HEADERS):
            pass
    assert exc_bridge.value.code == 1008

    # 3. Prohibited Bee wearable haptic target rejected on handshake (SPEC.md §4.1)
    with pytest.raises(WebSocketDisconnect) as exc_bee:
        with client.websocket_connect(
            "/v1/rehearsal/ws?target_device=bee_wearable",
            headers=HEADERS_A,
        ):
            pass
    assert exc_bee.value.code == 1008

    # 4. Invalid situation rejected on handshake
    with pytest.raises(WebSocketDisconnect) as exc_sit:
        with client.websocket_connect(
            "/v1/rehearsal/ws?situation=medical_emergency",
            headers=HEADERS_A,
        ):
            pass
    assert exc_sit.value.code == 1008

    # 5. Cross-learner IDOR and unknown rehearsal_id rejected (AC-NT-019)
    with pytest.raises(WebSocketDisconnect) as exc_idor:
        with client.websocket_connect(
            f"/v1/rehearsal/ws?rehearsal_id={rehearsal_id}",
            headers=HEADERS_B,
        ):
            pass
    assert exc_idor.value.code == 1008

    with pytest.raises(WebSocketDisconnect) as exc_404:
        with client.websocket_connect(
            "/v1/rehearsal/ws?rehearsal_id=nonexistent-reh",
            headers=HEADERS_A,
        ):
            pass
    assert exc_404.value.code == 1008

    # 6. Valid query-token handshake attaches cleanly and emits contract-valid session_ready
    with client.websocket_connect(
        f"/v1/rehearsal/ws?rehearsal_id={rehearsal_id}&token=learner:{LEARNER_A}&haptics_enabled=false"
    ) as ws:
        ready = ws.receive_json()
        jsonschema.validate(ready, WS_SERVER_EVENT_SCHEMAS["session_ready"])
        assert ready["rehearsal_id"] == rehearsal_id
        assert ready["state"] == "in_progress"
        assert ready["current_turn_id"] == "t1"


def test_ws_message_ordering_minimized_transcripts_pcm_deletion_and_model_gateway() -> None:
    clock = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    gateway = ModelGateway(provider=_StubGatewayProvider())
    runtime = ApiRuntimeState(clock=clock, model_gateway=gateway)
    client = TestClient(create_app(runtime))
    rehearsal_id = _create_confirmed_http_rehearsal(client)

    skills_before = client.get("/v1/progress", headers=HEADERS_A).json()["skill_states"]
    pcm_chunk_b64 = base64.b64encode(b"\x12\x34" * 800).decode("ascii")

    with client.websocket_connect(
        f"/v1/rehearsal/ws?rehearsal_id={rehearsal_id}",
        headers=HEADERS_A,
    ) as ws:
        ready = ws.receive_json()
        jsonschema.validate(ready, WS_SERVER_EVENT_SCHEMAS["session_ready"])

        # Step 1: request_counterpart -> [counterpart_audio_ready, turn_haptic (SHORT_DOUBLE)]
        ws.send_json({"type": "request_counterpart", "request_id": "cp-1"})
        cp_1 = ws.receive_json()
        hap_cp_1 = ws.receive_json()
        jsonschema.validate(cp_1, WS_SERVER_EVENT_SCHEMAS["counterpart_audio_ready"])
        jsonschema.validate(hap_cp_1, WS_SERVER_EVENT_SCHEMAS["turn_haptic"])
        assert [cp_1["type"], hap_cp_1["type"]] == ["counterpart_audio_ready", "turn_haptic"]
        assert cp_1["turn_id"] == "t1"
        assert hap_cp_1["pattern"] == "SHORT_DOUBLE"
        assert hap_cp_1["trigger"] == "counterpart_turn_ready"

        # Step 2: Stream 3 PTT audio_chunks (with PII in hint) -> verify ordering & PCM deletion
        ws.send_json(
            {
                "type": "audio_chunk",
                "turn_event_id": "ws-ptt-1",
                "sequence_id": 1,
                "pcm_base64": pcm_chunk_b64,
                "is_final": False,
                "streaming_transcription_consented": True,
            }
        )
        hap_ptt_start = ws.receive_json()
        tx_partial_1 = ws.receive_json()
        jsonschema.validate(hap_ptt_start, WS_SERVER_EVENT_SCHEMAS["turn_haptic"])
        jsonschema.validate(tx_partial_1, WS_SERVER_EVENT_SCHEMAS["transcription_update"])
        assert [hap_ptt_start["type"], tx_partial_1["type"]] == [
            "turn_haptic",
            "transcription_update",
        ]
        assert hap_ptt_start["pattern"] == "STEADY_BUZZ"
        assert tx_partial_1["is_partial"] is True
        assert tx_partial_1["raw_audio_deleted"] is False

        # Second intermediate chunk emits ONLY transcription_update (no repeated haptic!)
        ws.send_json(
            {
                "type": "audio_chunk",
                "turn_event_id": "ws-ptt-1",
                "sequence_id": 2,
                "pcm_base64": pcm_chunk_b64,
                "is_final": False,
                "streaming_transcription_consented": True,
            }
        )
        tx_partial_2 = ws.receive_json()
        assert tx_partial_2["type"] == "transcription_update"
        assert tx_partial_2["is_partial"] is True

        # Final chunk (turn boundary!) -> [transcription_update (final, 0 PCM), turn_haptic]
        ws.send_json(
            {
                "type": "audio_chunk",
                "turn_event_id": "ws-ptt-1",
                "sequence_id": 3,
                "pcm_base64": pcm_chunk_b64,
                "is_final": True,
                "streaming_transcription_consented": True,
                "transcript_hint": (
                    "Ik wil een afspraak voor de reparatie, mail jan@example.nl "
                    "of bel +31612345678."
                ),
            }
        )
        tx_final_1 = ws.receive_json()
        hap_ptt_rel = ws.receive_json()
        jsonschema.validate(tx_final_1, WS_SERVER_EVENT_SCHEMAS["transcription_update"])
        jsonschema.validate(hap_ptt_rel, WS_SERVER_EVENT_SCHEMAS["turn_haptic"])
        assert [tx_final_1["type"], hap_ptt_rel["type"]] == [
            "transcription_update",
            "turn_haptic",
        ]
        assert tx_final_1["is_partial"] is False
        assert tx_final_1["retained_pcm_bytes"] == 0
        assert tx_final_1["raw_audio_deleted"] is True
        assert len(runtime.store.rehearsal_raw_audio) == 0
        assert tx_final_1["redacted"] is True
        assert "jan@example.nl" not in tx_final_1["transcript"]
        assert "31612345678" not in tx_final_1["transcript"]
        assert "[redacted]" in tx_final_1["transcript"]
        assert tx_final_1["mapped_function"] == "goal_stated"
        assert hap_ptt_rel["pattern"] == "SHORT_DOUBLE"
        assert hap_ptt_rel["trigger"] == "ptt_recording_released"

        # Step 3: Request Turn 2 counterpart, test repair support, then complete via phrase_attempt
        ws.send_json({"type": "request_counterpart", "request_id": "cp-2"})
        cp_2 = ws.receive_json()
        hap_cp_2 = ws.receive_json()
        assert cp_2["turn_id"] == "t2"
        assert hap_cp_2["pattern"] == "SHORT_DOUBLE"

        # Ambiguous response triggers repair support + SHORT_DOUBLE
        ws.send_json(
            {
                "type": "phrase_attempt",
                "turn_event_id": "ws-phrase-miss",
                "transcript": "Dank u wel.",
                "mode": "text",
                "confidence": 0.9,
            }
        )
        tx_miss = ws.receive_json()
        hap_miss = ws.receive_json()
        assert tx_miss["support"] == "repair"
        assert hap_miss["trigger"] == "support_repair_offered"
        assert hap_miss["pattern"] == "SHORT_DOUBLE"

        # Valid confirmation -> [transcription_update, turn_haptic, breakdown_summary]
        ws.send_json(
            {
                "type": "phrase_attempt",
                "turn_event_id": "ws-phrase-ok",
                "transcript": "Ja, ik bevestig vrijdag om tien uur, dossier 12345678.",
                "mode": "text",
                "confidence": 0.96,
            }
        )
        tx_ok = ws.receive_json()
        hap_done = ws.receive_json()
        summary = ws.receive_json()

        jsonschema.validate(tx_ok, WS_SERVER_EVENT_SCHEMAS["transcription_update"])
        jsonschema.validate(hap_done, WS_SERVER_EVENT_SCHEMAS["turn_haptic"])
        jsonschema.validate(summary, WS_SERVER_EVENT_SCHEMAS["breakdown_summary"])

        assert [tx_ok["type"], hap_done["type"], summary["type"]] == [
            "transcription_update",
            "turn_haptic",
            "breakdown_summary",
        ]
        assert hap_done["trigger"] == "rehearsal_completed"
        assert summary["fallback_used"] is False
        assert summary["other_speaker_transcript_retained"] is False
        assert summary["raw_pcm_retained_bytes"] == 0
        assert summary["authentic_mastery_updated"] is False

        # Verify counterpart text and raw PII are absent from breakdown_summary & stored transcripts
        summary_str = json.dumps(summary)
        assert "Vertel alstublieft wat u nodig heeft" not in summary_str
        assert "De reparatiewerkplek is vrijdag" not in summary_str
        assert "jan@example.nl" not in summary_str
        assert "12345678" not in summary_str
        stored_tx_str = json.dumps(runtime.store.rehearsal_turn_transcripts)
        assert "jan@example.nl" not in stored_tx_str
        assert "12345678" not in stored_tx_str

    # Authentic learner mastery MUST remain untouched after rehearsal (AC-NT-015)
    skills_after = client.get("/v1/progress", headers=HEADERS_A).json()["skill_states"]
    assert skills_after == skills_before


def test_ws_error_guards_sensitive_exclusion_and_disconnect_pcm_cleanup() -> None:
    runtime = ApiRuntimeState(clock=datetime(2026, 10, 24, 10, 0, tzinfo=UTC))
    client = TestClient(create_app(runtime))
    pcm_b64 = base64.b64encode(b"\xaa\xbb" * 400).decode("ascii")

    with client.websocket_connect("/v1/rehearsal/ws", headers=HEADERS_A) as ws:
        _ready = ws.receive_json()

        # Malformed JSON, non-object envelope, unsupported type
        ws.send_text("{not-valid-json")
        assert ws.receive_json()["code"] == "invalid_json"

        ws.send_text('["not-a-dict"]')
        assert ws.receive_json()["code"] == "invalid_message_envelope"

        ws.send_json({"type": "unknown_cmd"})
        assert ws.receive_json()["code"] == "unsupported_message_type"

        ws.send_json({"type": "request_counterpart", "turn_id": "nonexistent_turn"})
        assert ws.receive_json()["code"] == "unknown_turn_id"

        # Covert continuous stream and unconsented audio_chunk rejected with 0 retained PCM
        ws.send_json(
            {
                "type": "audio_chunk",
                "turn_event_id": "bad-covert",
                "sequence_id": 1,
                "pcm_base64": pcm_b64,
                "continuous_stream": True,
                "capture_origin": "bee_wearable",
            }
        )
        err_covert = ws.receive_json()
        assert err_covert["code"] == "continuous_microphone_streaming_prohibited"
        assert err_covert["retained_pcm_bytes"] == 0

        ws.send_json(
            {
                "type": "audio_chunk",
                "turn_event_id": "bad-consent",
                "sequence_id": 1,
                "pcm_base64": pcm_b64,
                "streaming_transcription_consented": False,
            }
        )
        err_consent = ws.receive_json()
        assert err_consent["code"] == "streaming_consent_required"
        assert err_consent["retained_pcm_bytes"] == 0

        ws.send_json(
            {
                "type": "audio_chunk",
                "turn_event_id": "bad-b64",
                "sequence_id": 1,
                "pcm_base64": "***not_b64***",
            }
        )
        assert ws.receive_json()["code"] == "invalid_pcm_base64"

        ws.send_json({"type": "audio_chunk", "turn_event_id": "", "sequence_id": 0})
        assert ws.receive_json()["code"] == "invalid_audio_chunk_fields"

        # Sensitive topic in audio_chunk & phrase_attempt rejected fail-closed (SEC-NT-006)
        ws.send_json(
            {
                "type": "audio_chunk",
                "turn_event_id": "sens-audio",
                "sequence_id": 1,
                "pcm_base64": pcm_b64,
                "is_final": True,
                "transcript_hint": "Mijn advocaat belt over de rechtbank.",
            }
        )
        err_sens_audio = ws.receive_json()
        assert err_sens_audio["code"] == "sensitive_content_excluded"
        assert err_sens_audio["retained_pcm_bytes"] == 0

        ws.send_json(
            {
                "type": "phrase_attempt",
                "turn_event_id": "sens-phrase",
                "transcript": "Mijn verblijfsvergunning bij immigration is afgewezen.",
            }
        )
        assert ws.receive_json()["code"] == "sensitive_content_excluded"

        ws.send_json({"type": "phrase_attempt", "turn_event_id": ""})
        assert ws.receive_json()["code"] == "missing_turn_event_id"

        # Send non-final audio_chunk then disconnect abruptly -> finally block zeroes buffer
        ws.send_json(
            {
                "type": "audio_chunk",
                "turn_event_id": "abandoned-mid-turn",
                "sequence_id": 1,
                "pcm_base64": pcm_b64,
                "is_final": False,
            }
        )
        assert ws.receive_json()["type"] == "turn_haptic"
        assert ws.receive_json()["type"] == "transcription_update"

    assert len(runtime.store.rehearsal_raw_audio) == 0


def test_cli_rehearsal_simulator_wav_upload_typed_phrases_and_console_output(
    tmp_path: Path,
) -> None:
    # Build a synthetic stereo 8kHz 16-bit WAV file to exercise stereo->mono + 16kHz resampling
    wav_path = tmp_path / "sample_dutch.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(8_000)
        wf.writeframes(struct.pack("<" + "hh" * 6_000, *([400, -400] * 6_000)))

    chunks = load_pcm_chunks_from_file(wav_path, chunk_size_bytes=16_000)
    assert len(chunks) == 2
    assert all(len(c) > 0 and len(c) % 2 == 0 for c in chunks)

    pcm_path = tmp_path / "raw_sample.pcm"
    pcm_path.write_bytes(b"\x10\x00" * 500 + b"\xff")
    assert len(load_pcm_chunks_from_file(pcm_path)) == 1

    with pytest.raises(FileNotFoundError):
        load_pcm_chunks_from_file(tmp_path / "missing.wav")

    output_buf = io.StringIO()
    summary = asyncio.run(
        run_simulator_session(
            situation="school_admin_appointment",
            haptics_enabled=True,
            scripted_inputs=[
                "/replay",
                f"/audio {wav_path} Ik wil een afspraak met de schooladministratie maken.",
                "/tap confirm_time",
            ],
            out=output_buf,
        )
    )
    rendered = output_buf.getvalue()
    assert summary is not None
    assert summary["state"] == "completed"
    assert summary["other_speaker_transcript_retained"] is False
    assert summary["raw_pcm_retained_bytes"] == 0
    assert summary["authentic_mastery_updated"] is False
    assert "SHORT_DOUBLE" in rendered
    assert "STEADY_BUZZ" in rendered
    assert "POST-REHEARSAL BREAKDOWN SUMMARY" in rendered

    # Test suppressed haptics + early /quit path via CLI entrypoint
    exit_code = simulator_main(
        [
            "--situation",
            "shift_clarification",
            "--no-haptics",
            "--phrases",
            "/quit",
        ]
    )
    assert exit_code == 0
