from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from nexttime_domain import ConsentError
from nexttime_rehearsal import (
    MAX_CHUNK_BYTES,
    ConsentedAudioChunkTranscriber,
    CovertCaptureProhibitedError,
    DeterministicFixtureTranscribeTransport,
    EventStreamFormatError,
    PcmAudioChunk,
    TranscribeTransportError,
    UnconfiguredAwsTranscribeTransport,
    UnsupportedTranscriptionProviderError,
    decode_eventstream_frame,
    encode_audio_event_frame,
    encode_eventstream_frame,
    encode_transcript_event_frame,
    parse_transcript_event_frame,
)

LEARNER_ID = "01000000000000000000000002"


def test_aws_eventstream_binary_codec_crc_and_error_handling() -> None:
    audio_payload = b"\x10\x00\x20\x00" * 50
    frame = encode_audio_event_frame(audio_payload)
    decoded = decode_eventstream_frame(frame)
    assert decoded.headers[":event-type"] == "AudioEvent"
    assert decoded.payload == audio_payload

    tx_frame = encode_transcript_event_frame(
        transcript="Kunt u dat bevestigen",
        is_partial=False,
        confidence=0.88,
    )
    segs = parse_transcript_event_frame(tx_frame)
    assert len(segs) == 1
    assert segs[0].transcript == "Kunt u dat bevestigen"
    assert segs[0].is_partial is False
    assert segs[0].confidence == 0.88

    # Non-TranscriptEvent frames parse cleanly as empty tuple
    assert parse_transcript_event_frame(frame) == ()

    # Stream exception frame raises EventStreamFormatError
    exc_frame = encode_eventstream_frame(
        headers={":message-type": "exception", ":exception-type": "BadRequestException"},
        payload=b"{}",
    )
    with pytest.raises(EventStreamFormatError, match="BadRequestException"):
        parse_transcript_event_frame(exc_frame)

    # Corrupt prelude byte triggers prelude CRC mismatch
    tampered_prelude = bytearray(frame)
    tampered_prelude[7] ^= 0xFF
    with pytest.raises(EventStreamFormatError):
        decode_eventstream_frame(bytes(tampered_prelude))

    # Corrupt payload byte triggers message CRC mismatch
    tampered_payload = bytearray(frame)
    tampered_payload[-6] ^= 0xFF
    with pytest.raises(EventStreamFormatError, match="message CRC32 mismatch"):
        decode_eventstream_frame(bytes(tampered_payload))

    # Short frame rejected
    with pytest.raises(EventStreamFormatError, match="16-byte minimum"):
        decode_eventstream_frame(b"short")

    # Empty header name rejected
    with pytest.raises(EventStreamFormatError, match="header name length"):
        encode_eventstream_frame(headers={"": "bad"}, payload=b"")

    # Malformed TranscriptEvent JSON and non-list Results rejected
    bad_json_frame = encode_eventstream_frame(
        headers={":message-type": "event", ":event-type": "TranscriptEvent"},
        payload=b"{not-json",
    )
    with pytest.raises(EventStreamFormatError, match="Invalid TranscriptEvent JSON"):
        parse_transcript_event_frame(bad_json_frame)

    bad_results_frame = encode_eventstream_frame(
        headers={":message-type": "event", ":event-type": "TranscriptEvent"},
        payload=b'{"Transcript":{"Results":"oops"}}',
    )
    with pytest.raises(EventStreamFormatError, match="Results must be a list"):
        parse_transcript_event_frame(bad_results_frame)


def test_transcriber_enforces_ptt_consent_zeroes_pcm_immediately_and_blocks_whisper() -> None:
    now = datetime(2026, 10, 24, 11, 0, tzinfo=UTC)

    # Whisper or unapproved providers rejected without SPEC_DEVIATIONS entry
    with pytest.raises(UnsupportedTranscriptionProviderError, match="Whisper"):
        ConsentedAudioChunkTranscriber(provider="openai_whisper")

    # Unconfigured live transport fails closed while STILL zeroing all PCM buffers!
    unconfigured = ConsentedAudioChunkTranscriber(transport=UnconfiguredAwsTranscribeTransport())
    live_chunk = PcmAudioChunk.from_bytes(sequence_id=1, pcm_bytes=b"\x01\x02" * 400)
    with pytest.raises(TranscribeTransportError) as exc_live:
        asyncio.run(
            unconfigured.transcribe_ptt_turn(
                learner_id=LEARNER_ID,
                rehearsal_id="reh_1",
                turn_event_id="te_live",
                chunks=[live_chunk],
                streaming_transcription_consented=True,
                transcribed_at=now,
            )
        )
    assert exc_live.value.code == "aws_transcribe_streaming_unconfigured"
    assert len(live_chunk.pcm_buffer) == 0

    # Continuous microphone / Bee wearable streaming rejected AND PCM zeroed immediately
    transcriber = ConsentedAudioChunkTranscriber(
        transport=DeterministicFixtureTranscribeTransport(
            canned_transcript="Kunt u vrijdag om tien uur bevestigen?",
            canned_confidence=0.93,
            emit_partial=True,
        )
    )
    covert_chunk = PcmAudioChunk.from_bytes(sequence_id=1, pcm_bytes=b"\x05\x06" * 300)
    with pytest.raises(CovertCaptureProhibitedError, match="continuous_microphone"):
        asyncio.run(
            transcriber.transcribe_ptt_turn(
                learner_id=LEARNER_ID,
                rehearsal_id="reh_1",
                turn_event_id="te_covert",
                chunks=[covert_chunk],
                streaming_transcription_consented=True,
                capture_origin="bee_wearable_background",
                continuous_stream=True,
                transcribed_at=now,
            )
        )
    assert len(covert_chunk.pcm_buffer) == 0

    # Missing or revoked streaming consent rejected AND PCM zeroed immediately
    unconsented_chunk = PcmAudioChunk.from_bytes(sequence_id=1, pcm_bytes=b"\x07\x08" * 300)
    with pytest.raises(ConsentError):
        asyncio.run(
            transcriber.transcribe_ptt_turn(
                learner_id=LEARNER_ID,
                rehearsal_id="reh_1",
                turn_event_id="te_noconsent",
                chunks=[unconsented_chunk],
                streaming_transcription_consented=False,
                transcribed_at=now,
            )
        )
    assert len(unconsented_chunk.pcm_buffer) == 0

    # Consented multi-chunk push-to-talk turn (with duplicate chunk seq=1 deduplication)
    c1 = PcmAudioChunk.from_bytes(sequence_id=1, pcm_bytes=b"\x11\x22" * 800)
    c1_dup = PcmAudioChunk.from_bytes(sequence_id=1, pcm_bytes=b"\x11\x22" * 800)
    c2 = PcmAudioChunk.from_bytes(sequence_id=2, pcm_bytes=b"\x33\x44" * 800)

    result = asyncio.run(
        transcriber.transcribe_ptt_turn(
            learner_id=LEARNER_ID,
            rehearsal_id="reh_1",
            turn_event_id="te_ok",
            chunks=[c1, c1_dup, c2],
            streaming_transcription_consented=True,
            save_self_excerpt=False,
            transcribed_at=now,
        )
    )
    assert result.transcript == "Kunt u vrijdag om tien uur bevestigen?"
    assert result.confidence == 0.93
    assert result.below_confidence_threshold is False
    assert result.partial_count == 1
    assert result.retained_pcm_bytes == 0
    assert len(result.chunk_receipts) == 2
    assert all(
        r.deleted_immediately is True and r.bytes_zeroed == 1600 for r in result.chunk_receipts
    )
    assert len(c1.pcm_buffer) == 0
    assert len(c1_dup.pcm_buffer) == 0
    assert len(c2.pcm_buffer) == 0
    assert result.transcript_delete_after == "2026-10-25T11:00:00.000Z"
    assert result.provenance == "fixture"


def test_transcriber_low_confidence_and_audio_chunk_bounds() -> None:
    now = datetime(2026, 10, 24, 11, 30, tzinfo=UTC)
    low_conf_transcriber = ConsentedAudioChunkTranscriber(
        transport=DeterministicFixtureTranscribeTransport(
            canned_transcript="Eh vrijdag misschien",
            canned_confidence=0.61,
            emit_partial=False,
        )
    )
    res = asyncio.run(
        low_conf_transcriber.transcribe_ptt_turn(
            learner_id=LEARNER_ID,
            rehearsal_id="reh_2",
            turn_event_id="te_low",
            chunks=[PcmAudioChunk.from_bytes(sequence_id=1, pcm_bytes=b"\x01\x00" * 500)],
            streaming_transcription_consented=True,
            save_self_excerpt=True,
            transcribed_at=now,
        )
    )
    assert res.below_confidence_threshold is True
    assert res.partial_count == 0
    assert res.transcript_delete_after is None

    # Empty chunk list, bad sample rate, odd byte length, out-of-order seq, >30s turn rejected
    with pytest.raises(ValueError, match="At least one"):
        asyncio.run(
            low_conf_transcriber.transcribe_ptt_turn(
                learner_id=LEARNER_ID,
                rehearsal_id="reh_2",
                turn_event_id="te_empty",
                chunks=[],
                streaming_transcription_consented=True,
                transcribed_at=now,
            )
        )

    bad_rate = PcmAudioChunk.from_bytes(
        sequence_id=1, pcm_bytes=b"\x01\x00" * 100, sample_rate_hz=44_100
    )
    with pytest.raises(ValueError, match="16kHz mono 16-bit PCM"):
        asyncio.run(
            low_conf_transcriber.transcribe_ptt_turn(
                learner_id=LEARNER_ID,
                rehearsal_id="reh_2",
                turn_event_id="te_rate",
                chunks=[bad_rate],
                streaming_transcription_consented=True,
                transcribed_at=now,
            )
        )
    assert len(bad_rate.pcm_buffer) == 0

    odd_bytes = PcmAudioChunk.from_bytes(sequence_id=1, pcm_bytes=b"\x01\x02\x03")
    with pytest.raises(ValueError, match="byte length"):
        asyncio.run(
            low_conf_transcriber.transcribe_ptt_turn(
                learner_id=LEARNER_ID,
                rehearsal_id="reh_2",
                turn_event_id="te_odd",
                chunks=[odd_bytes],
                streaming_transcription_consented=True,
                transcribed_at=now,
            )
        )
    assert len(odd_bytes.pcm_buffer) == 0

    out_of_order = [
        PcmAudioChunk.from_bytes(sequence_id=2, pcm_bytes=b"\x01\x00" * 100),
        PcmAudioChunk.from_bytes(sequence_id=1, pcm_bytes=b"\x01\x00" * 100),
    ]
    with pytest.raises(ValueError, match="Out-of-order"):
        asyncio.run(
            low_conf_transcriber.transcribe_ptt_turn(
                learner_id=LEARNER_ID,
                rehearsal_id="reh_2",
                turn_event_id="te_ooo",
                chunks=out_of_order,
                streaming_transcription_consented=True,
                transcribed_at=now,
            )
        )

    # Exceeding 30-second push-to-talk turn cap (>960,000 bytes at 32,000 bytes/sec)
    over_30s_chunks = [
        PcmAudioChunk.from_bytes(sequence_id=i, pcm_bytes=b"\x01\x00" * (MAX_CHUNK_BYTES // 2))
        for i in range(1, 17)
    ]
    with pytest.raises(ValueError, match="30-second bound"):
        asyncio.run(
            low_conf_transcriber.transcribe_ptt_turn(
                learner_id=LEARNER_ID,
                rehearsal_id="reh_2",
                turn_event_id="te_30s",
                chunks=over_30s_chunks,
                streaming_transcription_consented=True,
                transcribed_at=now,
            )
        )
    assert all(len(c.pcm_buffer) == 0 for c in over_30s_chunks)
