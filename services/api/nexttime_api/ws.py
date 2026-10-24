from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from nexttime_domain import (
    ConsentError,
    HapticPreferences,
    RehearsalPacingTrigger,
    SelfExcerpt,
    SensitiveContentError,
    Situation,
    Speaker,
    TranscriptTurn,
    ensure_non_sensitive_excerpts,
    redact_deterministic_identifiers,
)
from nexttime_rehearsal import (
    ALLOWED_PTT_CAPTURE_ORIGINS,
    AudioChunkTranscriber,
    CovertCaptureProhibitedError,
    DeterministicFixtureTranscribeTransport,
    PcmAudioChunk,
    TranscribeTransportError,
)

from .auth import require_learner_principal
from .problem import ApiProblemError
from .schemas import RehearsalCompleteRequest, RehearsalTurnSubmitRequest
from .state import ApiRuntimeState, RehearsalSessionEntry

SUPPORTED_CLIENT_MESSAGES = ("audio_chunk", "phrase_attempt", "request_counterpart")
SUPPORTED_SERVER_MESSAGES = (
    "session_ready",
    "transcription_update",
    "turn_haptic",
    "counterpart_audio_ready",
    "breakdown_summary",
    "error",
)
_ALLOWED_WS_TARGETS = frozenset({"ios_rehearsal_client", "cli_rehearsal_simulator"})

_DEFAULT_FIXTURE_TURN_UTTERANCES: dict[tuple[Situation, str], str] = {
    (Situation.REPAIR, "t1"): "Ik wil een afspraak voor de reparatie maken.",
    (Situation.REPAIR, "t2"): "Ja, ik bevestig de datum en de tijd voor vrijdag om tien uur.",
    (Situation.SCHOOL_ADMIN, "t1"): "Ik wil een afspraak met de schooladministratie maken.",
    (Situation.SCHOOL_ADMIN, "t2"): "Ja, ik bevestig maandag om negen uur.",
    (Situation.SHIFT, "t1"): "Ik wil de volgende stap voor mijn dienst bespreken.",
    (Situation.SHIFT, "t2"): "Ja, ik bevestig de datum en de tijd voor dinsdag om acht uur.",
}


def _parse_bool_query(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _resolve_ws_auth_header(websocket: WebSocket) -> str | None:
    header_val = websocket.headers.get("authorization")
    if header_val:
        return header_val
    query_tok = websocket.query_params.get("token")
    if query_tok is None or not query_tok.strip():
        return None
    cleaned = query_tok.strip()
    return cleaned if cleaned.lower().startswith("bearer ") else f"Bearer {cleaned}"


def _active_turn_id(entry: RehearsalSessionEntry, requested_turn_id: object) -> str:
    if isinstance(requested_turn_id, str) and requested_turn_id.strip():
        return requested_turn_id.strip()
    turns = entry.service.scenario.turns
    committed = len(entry.committed_sequences)
    if committed < len(turns):
        return turns[committed].turn_id
    return turns[-1].turn_id


def _zero_turn_chunks(chunks: list[PcmAudioChunk]) -> None:
    for chunk in chunks:
        chunk.zero_immediately()
    chunks.clear()


def _haptic_frame(runtime: ApiRuntimeState, event_dict: dict[str, Any]) -> dict[str, Any]:
    return {"type": "turn_haptic", **event_dict}


async def handle_rehearsal_websocket(websocket: WebSocket, runtime: ApiRuntimeState) -> None:
    """Bidirectional Rehearsal WebSocket endpoint (/v1/rehearsal/ws).

    Connects AudioChunkTranscriber, ModelGateway (GatewayBreakdownExtractor), and
    PacingHapticsEngine while enforcing immediate raw PCM zeroing on turn boundaries
    and disconnect (FR-NT-014, SEC-NT-004).
    """
    auth_header = _resolve_ws_auth_header(websocket)
    try:
        principal = require_learner_principal(runtime.authenticator, auth_header)
    except ApiProblemError as error:
        await websocket.close(code=1008, reason=error.code)
        return

    target_device = websocket.query_params.get("target_device", "ios_rehearsal_client").strip()
    if target_device not in _ALLOWED_WS_TARGETS:
        await websocket.close(code=1008, reason="unverified_bee_haptic_command_prohibited")
        return

    rehearsal_id_param = websocket.query_params.get("rehearsal_id")
    if rehearsal_id_param is not None and rehearsal_id_param.strip():
        try:
            entry = runtime._require_owned_rehearsal(  # noqa: SLF001
                actor_id=principal.actor_id,
                rehearsal_id=rehearsal_id_param.strip(),
            )
        except ApiProblemError as error:
            await websocket.close(code=1008, reason=error.code)
            return
    else:
        raw_sit = websocket.query_params.get("situation", Situation.REPAIR.value).strip()
        try:
            situation = Situation(raw_sit)
        except ValueError:
            await websocket.close(code=1008, reason="invalid_situation")
            return
        entry = runtime.create_ephemeral_simulator_rehearsal(
            actor_id=principal.actor_id,
            situation=situation,
        )

    if entry.service.state.value == "scheduled":
        entry.service.begin(actor_learner_id=principal.actor_id)
        runtime.store.rehearsal_sessions[entry.rehearsal_id]["state"] = entry.service.state.value

    preferences = HapticPreferences(
        haptics_enabled=_parse_bool_query(
            websocket.query_params.get("haptics_enabled"), default=True
        ),
        reduced_motion=_parse_bool_query(
            websocket.query_params.get("reduced_motion"), default=False
        ),
    )

    await websocket.accept()

    serialized_session = runtime._serialize_rehearsal(entry)  # noqa: SLF001
    await websocket.send_json(
        {
            "type": "session_ready",
            "rehearsal_id": entry.rehearsal_id,
            "learner_id": principal.actor_id,
            "situation": serialized_session["situation"],
            "target_function": serialized_session["target_function"],
            "state": serialized_session["state"],
            "current_turn_id": serialized_session["current_turn_id"],
            "total_turns": len(serialized_session["turns"]),
            "estimated_seconds": serialized_session["estimated_seconds"],
            "support_phrases": serialized_session["support_phrases"],
            "supported_client_messages": list(SUPPORTED_CLIENT_MESSAGES),
            "supported_server_messages": list(SUPPORTED_SERVER_MESSAGES),
            "provenance": "fixture",
        }
    )

    pending_pcm_by_turn: dict[str, list[PcmAudioChunk]] = {}
    started_ptt_turns: set[str] = set()
    dialogue_turns: list[TranscriptTurn] = []
    timeline_ms = 0
    haptic_seq_counter = 0

    def _emit_ws_haptic(
        trigger: RehearsalPacingTrigger,
        *,
        source_key: str,
        turn_id: str | None,
    ) -> dict[str, Any]:
        nonlocal haptic_seq_counter
        haptic_seq_counter += 1
        event = entry.haptics.generate(
            source_event_id=f"{source_key}:{haptic_seq_counter}",
            trigger=trigger,
            occurred_at=runtime.now(),
            turn_id=turn_id,
            target_device=target_device,
            preferences=preferences,
        )
        return _haptic_frame(runtime, runtime._serialize_haptic_event(event))  # noqa: SLF001

    try:
        while True:
            raw_text = await websocket.receive_text()
            try:
                message = json.loads(raw_text)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "invalid_json",
                        "title": "Malformed WebSocket JSON frame",
                        "detail": "Client messages must be valid JSON objects.",
                    }
                )
                continue

            if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "invalid_message_envelope",
                        "title": "Invalid message envelope",
                        "detail": "Message must be a JSON object with a string 'type' field.",
                    }
                )
                continue

            msg_type = message["type"]

            if msg_type == "request_counterpart":
                turn_id = _active_turn_id(entry, message.get("turn_id"))
                session_view = runtime._serialize_rehearsal(entry)  # noqa: SLF001
                turn_payload = next(
                    (t for t in session_view["turns"] if t["turn_id"] == turn_id),
                    None,
                )
                if turn_payload is None:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "unknown_turn_id",
                            "title": "Unknown rehearsal turn",
                            "detail": f"Turn '{turn_id}' does not exist in this scenario.",
                        }
                    )
                    continue

                dialogue_turns.append(
                    TranscriptTurn(
                        speaker=Speaker.OTHER,
                        text=str(turn_payload["counterpart_text_nl"]),
                        start_ms=timeline_ms,
                        end_ms=timeline_ms + 2000,
                        confidence=1.0,
                    )
                )
                timeline_ms += 2200

                req_id = str(message.get("request_id") or f"cp-{turn_id}")
                await websocket.send_json(
                    {
                        "type": "counterpart_audio_ready",
                        "rehearsal_id": entry.rehearsal_id,
                        "turn_id": turn_id,
                        "counterpart_persona": session_view["counterpart_persona"],
                        "counterpart_text_nl": turn_payload["counterpart_text_nl"],
                        "counterpart_text_en": turn_payload["counterpart_text_en"],
                        "expected_functions": turn_payload["expected_functions"],
                        "repair_if_missing": turn_payload["repair_if_missing"],
                        "counterpart_audio": turn_payload["counterpart_audio"],
                        "support_phrases": session_view["support_phrases"],
                        "state": entry.service.state.value,
                    }
                )
                await websocket.send_json(
                    _emit_ws_haptic(
                        RehearsalPacingTrigger.COUNTERPART_TURN_READY,
                        source_key=f"counterpart:{req_id}",
                        turn_id=turn_id,
                    )
                )

            elif msg_type == "audio_chunk":
                turn_event_id = str(message.get("turn_event_id") or "").strip()
                raw_seq = message.get("sequence_id")
                raw_b64 = message.get("pcm_base64")
                if (
                    not turn_event_id
                    or isinstance(raw_seq, bool)
                    or not isinstance(raw_seq, int)
                    or raw_seq < 1
                    or not isinstance(raw_b64, str)
                ):
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "invalid_audio_chunk_fields",
                            "title": "Invalid audio_chunk message",
                            "detail": (
                                "Requires turn_event_id, integer sequence_id >= 1, and pcm_base64."
                            ),
                        }
                    )
                    continue

                try:
                    decoded_pcm = bytearray(base64.b64decode(raw_b64, validate=True))
                except (binascii.Error, ValueError):
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "invalid_pcm_base64",
                            "title": "Invalid base64 PCM payload",
                            "detail": "Audio chunk payload is not valid base64.",
                        }
                    )
                    continue

                chunk = PcmAudioChunk(
                    sequence_id=raw_seq,
                    pcm_buffer=decoded_pcm,
                    sample_rate_hz=int(message.get("sample_rate_hz", 16_000)),
                    channels=int(message.get("channels", 1)),
                    sample_width_bytes=int(message.get("sample_width_bytes", 2)),
                )
                consented = bool(message.get("streaming_transcription_consented", True))
                capture_origin = str(
                    message.get("capture_origin") or "iphone_rehearsal_ptt"
                ).strip()
                continuous_stream = bool(message.get("continuous_stream", False))
                is_final = bool(message.get("is_final", False))
                turn_id = _active_turn_id(entry, message.get("turn_id"))

                if continuous_stream or capture_origin not in ALLOWED_PTT_CAPTURE_ORIGINS:
                    chunk.zero_immediately()
                    _zero_turn_chunks(pending_pcm_by_turn.pop(turn_event_id, []))
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "continuous_microphone_streaming_prohibited",
                            "title": "Prohibited audio capture origin",
                            "detail": "Continuous or non-PTT audio streaming is prohibited.",
                            "retained_pcm_bytes": 0,
                        }
                    )
                    continue

                if not consented:
                    chunk.zero_immediately()
                    _zero_turn_chunks(pending_pcm_by_turn.pop(turn_event_id, []))
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "streaming_consent_required",
                            "title": "Streaming transcription consent required",
                            "detail": "Explicit learner consent is required before PTT streaming.",
                            "retained_pcm_bytes": 0,
                        }
                    )
                    continue

                turn_chunks = pending_pcm_by_turn.setdefault(turn_event_id, [])
                turn_chunks.append(chunk)

                if not is_final:
                    if turn_event_id not in started_ptt_turns:
                        started_ptt_turns.add(turn_event_id)
                        await websocket.send_json(
                            _emit_ws_haptic(
                                RehearsalPacingTrigger.PTT_RECORDING_STARTED,
                                source_key=f"ptt-start:{turn_event_id}",
                                turn_id=turn_id,
                            )
                        )
                    buffered_bytes = sum(len(c.pcm_buffer) for c in turn_chunks)
                    await websocket.send_json(
                        {
                            "type": "transcription_update",
                            "rehearsal_id": entry.rehearsal_id,
                            "turn_event_id": turn_event_id,
                            "turn_id": turn_id,
                            "sequence_id": raw_seq,
                            "is_partial": True,
                            "transcript": "",
                            "redacted": False,
                            "confidence": 0.0,
                            "mapped_function": None,
                            "support": None,
                            "state": entry.service.state.value,
                            "current_turn_id": turn_id,
                            "committed_turn_count": len(entry.committed_sequences),
                            "retained_pcm_bytes": buffered_bytes,
                            "raw_audio_deleted": False,
                        }
                    )
                    continue

                # Turn boundary completion: transcribe and immediately zero/delete all PCM!
                chunks_to_process = pending_pcm_by_turn.pop(turn_event_id, [])
                started_ptt_turns.discard(turn_event_id)
                save_self_excerpt = bool(message.get("save_self_excerpt", False))
                hint = message.get("transcript_hint")
                transcriber = runtime.chunk_transcriber
                if transcriber._transport.provenance == "fixture":  # noqa: SLF001
                    canned_text = (
                        str(hint).strip()
                        if isinstance(hint, str) and hint.strip()
                        else _DEFAULT_FIXTURE_TURN_UTTERANCES.get(
                            (entry.service.scenario.situation, turn_id),
                            "Ik wil een afspraak voor de reparatie.",
                        )
                    )
                    transcriber = AudioChunkTranscriber(
                        transport=DeterministicFixtureTranscribeTransport(
                            canned_transcript=canned_text,
                            canned_confidence=0.92,
                        )
                    )

                try:
                    tx_result = await transcriber.transcribe_ptt_turn(
                        learner_id=principal.actor_id,
                        rehearsal_id=entry.rehearsal_id,
                        turn_event_id=turn_event_id,
                        chunks=chunks_to_process,
                        streaming_transcription_consented=consented,
                        capture_origin=capture_origin,
                        continuous_stream=continuous_stream,
                        save_self_excerpt=save_self_excerpt,
                        transcribed_at=runtime.now(),
                    )
                except (
                    CovertCaptureProhibitedError,
                    ConsentError,
                    ValueError,
                    TranscribeTransportError,
                ) as error:
                    _zero_turn_chunks(chunks_to_process)
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "audio_chunk_processing_failed",
                            "title": "Audio chunk transcription rejected",
                            "detail": str(error),
                            "retained_pcm_bytes": 0,
                        }
                    )
                    continue

                minimized_text, was_redacted = redact_deterministic_identifiers(
                    tx_result.transcript
                )
                if minimized_text.strip():
                    try:
                        ensure_non_sensitive_excerpts(
                            (SelfExcerpt(minimized_text, 0, 2000, tx_result.confidence),)
                        )
                    except SensitiveContentError:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "code": "sensitive_content_excluded",
                                "title": "Sensitive topic excluded",
                                "detail": (
                                    "Medical, legal, immigration, disciplinary, financial, and "
                                    "crisis topics are excluded; transient audio was deleted."
                                ),
                                "retained_pcm_bytes": 0,
                                "raw_audio_deleted": True,
                            }
                        )
                        continue

                seq_num = len(entry.committed_sequences) + 1
                try:
                    turn_submit = runtime.submit_rehearsal_turn(
                        actor_id=principal.actor_id,
                        rehearsal_id=entry.rehearsal_id,
                        req=RehearsalTurnSubmitRequest(
                            turn_event_id=turn_event_id,
                            turn_id=turn_id,
                            sequence=seq_num,
                            transcript=minimized_text,
                            phrase_id=None,
                            confidence=tx_result.confidence,
                            mode="speech",
                            save_self_excerpt=save_self_excerpt,
                        ),
                    )
                except ApiProblemError as error:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": error.code,
                            "title": error.title,
                            "detail": error.detail,
                            "retained_pcm_bytes": 0,
                        }
                    )
                    continue

                if minimized_text.strip() and not turn_submit["duplicate"]:
                    dialogue_turns.append(
                        TranscriptTurn(
                            speaker=Speaker.LEARNER,
                            text=minimized_text,
                            start_ms=timeline_ms,
                            end_ms=timeline_ms + 2500,
                            confidence=tx_result.confidence,
                        )
                    )
                    timeline_ms += 2700

                await websocket.send_json(
                    {
                        "type": "transcription_update",
                        "rehearsal_id": entry.rehearsal_id,
                        "turn_event_id": turn_event_id,
                        "turn_id": turn_id,
                        "sequence_id": raw_seq,
                        "is_partial": False,
                        "transcript": minimized_text,
                        "redacted": was_redacted,
                        "confidence": tx_result.confidence,
                        "below_confidence_threshold": tx_result.below_confidence_threshold,
                        "mapped_function": turn_submit["mapped_function"],
                        "support": turn_submit["support"],
                        "state": turn_submit["state"],
                        "current_turn_id": turn_submit["current_turn_id"],
                        "committed_turn_count": turn_submit["committed_turn_count"],
                        "retained_pcm_bytes": 0,
                        "raw_audio_deleted": bool(turn_submit["raw_audio_deleted"]),
                        "chunk_receipts": [
                            {
                                "sequence_id": r.sequence_id,
                                "bytes_zeroed": r.bytes_zeroed,
                                "deleted_immediately": True,
                                "deleted_at": r.deleted_at,
                            }
                            for r in tx_result.chunk_receipts
                        ],
                        "transcript_delete_after": turn_submit["transcript_delete_after"],
                        "authentic_mastery_updated": False,
                    }
                )
                await websocket.send_json(
                    _emit_ws_haptic(
                        RehearsalPacingTrigger.PTT_RECORDING_RELEASED,
                        source_key=f"ptt-release:{turn_event_id}",
                        turn_id=turn_id,
                    )
                )
                if turn_submit["state"] == "completed":
                    await websocket.send_json(
                        runtime.build_rehearsal_breakdown_summary(
                            entry=entry,
                            dialogue_turns=dialogue_turns,
                        )
                    )

            elif msg_type == "phrase_attempt":
                if bool(message.get("exit_early", False)):
                    runtime.complete_rehearsal(
                        actor_id=principal.actor_id,
                        rehearsal_id=entry.rehearsal_id,
                        req=RehearsalCompleteRequest(
                            final_turn_sequence=len(entry.committed_sequences),
                            exit_early=True,
                        ),
                    )
                    await websocket.send_json(
                        _emit_ws_haptic(
                            RehearsalPacingTrigger.REHEARSAL_COMPLETED,
                            source_key="phrase-exit-early",
                            turn_id=None,
                        )
                    )
                    await websocket.send_json(
                        runtime.build_rehearsal_breakdown_summary(
                            entry=entry,
                            dialogue_turns=dialogue_turns,
                        )
                    )
                    continue

                turn_event_id = str(message.get("turn_event_id") or "").strip()
                if not turn_event_id:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "missing_turn_event_id",
                            "title": "Missing turn_event_id",
                            "detail": "phrase_attempt requires a non-empty turn_event_id.",
                        }
                    )
                    continue

                turn_id = _active_turn_id(entry, message.get("turn_id"))
                raw_phrase_text = str(message.get("transcript") or "")
                minimized_text, was_redacted = redact_deterministic_identifiers(raw_phrase_text)
                confidence = float(message.get("confidence", 0.95))
                if minimized_text.strip():
                    try:
                        ensure_non_sensitive_excerpts(
                            (SelfExcerpt(minimized_text, 0, 2000, confidence),)
                        )
                    except SensitiveContentError:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "code": "sensitive_content_excluded",
                                "title": "Sensitive topic excluded",
                                "detail": (
                                    "Medical, legal, immigration, disciplinary, financial, and "
                                    "crisis topics are excluded from rehearsal processing."
                                ),
                            }
                        )
                        continue

                raw_mode = str(message.get("mode") or "text")
                if raw_mode not in {"speech", "text", "tap"}:
                    raw_mode = "text"
                phrase_id_raw = message.get("phrase_id")
                phrase_id = str(phrase_id_raw).strip() if isinstance(phrase_id_raw, str) else None
                save_self_excerpt = bool(message.get("save_self_excerpt", False))
                seq_num = len(entry.committed_sequences) + 1

                try:
                    turn_submit = runtime.submit_rehearsal_turn(
                        actor_id=principal.actor_id,
                        rehearsal_id=entry.rehearsal_id,
                        req=RehearsalTurnSubmitRequest(
                            turn_event_id=turn_event_id,
                            turn_id=turn_id,
                            sequence=seq_num,
                            transcript=minimized_text,
                            phrase_id=phrase_id,
                            confidence=confidence,
                            mode=raw_mode,  # type: ignore[arg-type]
                            save_self_excerpt=save_self_excerpt,
                        ),
                    )
                except ApiProblemError as error:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": error.code,
                            "title": error.title,
                            "detail": error.detail,
                        }
                    )
                    continue

                if minimized_text.strip() and not turn_submit["duplicate"]:
                    dialogue_turns.append(
                        TranscriptTurn(
                            speaker=Speaker.LEARNER,
                            text=minimized_text,
                            start_ms=timeline_ms,
                            end_ms=timeline_ms + 2500,
                            confidence=confidence,
                        )
                    )
                    timeline_ms += 2700

                if turn_submit["state"] == "completed":
                    trigger = RehearsalPacingTrigger.REHEARSAL_COMPLETED
                elif turn_submit["support"] is not None:
                    trigger = RehearsalPacingTrigger.SUPPORT_REPAIR_OFFERED
                elif (
                    int(turn_submit["committed_turn_count"])
                    == len(entry.service.scenario.turns) - 1
                ):
                    trigger = RehearsalPacingTrigger.TURN_BUDGET_BOUNDARY
                else:
                    trigger = RehearsalPacingTrigger.COUNTERPART_TURN_READY

                await websocket.send_json(
                    {
                        "type": "transcription_update",
                        "rehearsal_id": entry.rehearsal_id,
                        "turn_event_id": turn_event_id,
                        "turn_id": turn_id,
                        "sequence_id": seq_num,
                        "is_partial": False,
                        "transcript": minimized_text,
                        "redacted": was_redacted,
                        "confidence": confidence,
                        "below_confidence_threshold": confidence < 0.75,
                        "mapped_function": turn_submit["mapped_function"],
                        "support": turn_submit["support"],
                        "state": turn_submit["state"],
                        "current_turn_id": turn_submit["current_turn_id"],
                        "committed_turn_count": turn_submit["committed_turn_count"],
                        "retained_pcm_bytes": 0,
                        "raw_audio_deleted": True,
                        "transcript_delete_after": turn_submit["transcript_delete_after"],
                        "authentic_mastery_updated": False,
                    }
                )
                await websocket.send_json(
                    _emit_ws_haptic(
                        trigger,
                        source_key=f"phrase:{turn_event_id}",
                        turn_id=turn_id,
                    )
                )
                if turn_submit["state"] == "completed":
                    await websocket.send_json(
                        runtime.build_rehearsal_breakdown_summary(
                            entry=entry,
                            dialogue_turns=dialogue_turns,
                        )
                    )

            else:
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "unsupported_message_type",
                        "title": "Unsupported WebSocket message type",
                        "detail": (
                            f"Unsupported message type '{msg_type}'. "
                            f"Supported: {', '.join(SUPPORTED_CLIENT_MESSAGES)}."
                        ),
                    }
                )
    except WebSocketDisconnect:
        return
    finally:
        for leftover_chunks in pending_pcm_by_turn.values():
            _zero_turn_chunks(leftover_chunks)
        pending_pcm_by_turn.clear()
