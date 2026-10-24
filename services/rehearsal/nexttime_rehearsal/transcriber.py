from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from nexttime_domain import ConsentError, require_processing_consent

from .aws_eventstream import (
    decode_eventstream_frame,
    encode_audio_event_frame,
    encode_transcript_event_frame,
    parse_transcript_event_frame,
)

REQUIRED_SAMPLE_RATE_HZ = 16_000
REQUIRED_CHANNELS = 1
REQUIRED_SAMPLE_WIDTH_BYTES = 2
MAX_CHUNK_BYTES = 65_536
MAX_PTT_TURN_DURATION_MS = 30_000
REHEARSAL_MAPPING_CONFIDENCE_THRESHOLD = 0.75
ALLOWED_PTT_CAPTURE_ORIGINS = frozenset({"iphone_rehearsal_ptt", "cli_rehearsal_simulator"})


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Transcriber timestamp must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CovertCaptureProhibitedError(PermissionError):
    """Raised when continuous/always-on or wearable background mic streaming is attempted."""


class UnsupportedTranscriptionProviderError(ValueError):
    """Raised when a non-spec provider (such as Whisper) is requested without deviation."""


class TranscribeTransportError(RuntimeError):
    """Raised when the Amazon Transcribe Streaming transport is unconfigured or fails."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass
class PcmAudioChunk:
    """Mutable PCM s16le buffer zeroed immediately after framing (SEC-NT-004)."""

    sequence_id: int
    pcm_buffer: bytearray
    sample_rate_hz: int = REQUIRED_SAMPLE_RATE_HZ
    channels: int = REQUIRED_CHANNELS
    sample_width_bytes: int = REQUIRED_SAMPLE_WIDTH_BYTES

    @classmethod
    def from_bytes(
        cls,
        *,
        sequence_id: int,
        pcm_bytes: bytes,
        sample_rate_hz: int = REQUIRED_SAMPLE_RATE_HZ,
        channels: int = REQUIRED_CHANNELS,
        sample_width_bytes: int = REQUIRED_SAMPLE_WIDTH_BYTES,
    ) -> PcmAudioChunk:
        return cls(
            sequence_id=sequence_id,
            pcm_buffer=bytearray(pcm_bytes),
            sample_rate_hz=sample_rate_hz,
            channels=channels,
            sample_width_bytes=sample_width_bytes,
        )

    def zero_immediately(self) -> int:
        length = len(self.pcm_buffer)
        if length:
            self.pcm_buffer[:] = b"\x00" * length
            self.pcm_buffer.clear()
        return length


@dataclass(frozen=True)
class ChunkDeletionReceipt:
    sequence_id: int
    bytes_zeroed: int
    deleted_immediately: Literal[True]
    deleted_at: str


@dataclass(frozen=True)
class TurnTranscriptionResult:
    turn_event_id: str
    rehearsal_id: str
    learner_id: str
    transcript: str
    confidence: float
    below_confidence_threshold: bool
    partial_count: int
    chunk_receipts: tuple[ChunkDeletionReceipt, ...]
    retained_pcm_bytes: Literal[0]
    transcript_delete_after: str | None
    provider: Literal["aws_transcribe_streaming"]
    provenance: Literal["fixture", "live"]


class TranscribeEventStreamTransportPort(Protocol):
    @property
    def provenance(self) -> Literal["fixture", "live"]: ...

    async def exchange_audio_frames(
        self,
        *,
        language_code: str,
        sample_rate_hz: int,
        frames: Sequence[bytes],
    ) -> Sequence[bytes]: ...


class UnconfiguredAwsTranscribeTransport:
    """Fail-closed default when live Amazon Transcribe Streaming is not configured."""

    @property
    def provenance(self) -> Literal["fixture", "live"]:
        return "live"

    async def exchange_audio_frames(
        self,
        *,
        language_code: str,
        sample_rate_hz: int,
        frames: Sequence[bytes],
    ) -> Sequence[bytes]:
        raise TranscribeTransportError(
            "aws_transcribe_streaming_unconfigured",
            "Live Amazon Transcribe Streaming HTTP/2 transport is not configured.",
        )


class DeterministicFixtureTranscribeTransport:
    """Fixture-labeled EventStream transport verifying AudioEvent CRC framing in tests."""

    def __init__(
        self,
        *,
        canned_transcript: str = "Ik wil een afspraak voor de reparatie.",
        canned_confidence: float = 0.91,
        emit_partial: bool = True,
    ) -> None:
        self._transcript = canned_transcript
        self._confidence = canned_confidence
        self._emit_partial = emit_partial

    @property
    def provenance(self) -> Literal["fixture", "live"]:
        return "fixture"

    async def exchange_audio_frames(
        self,
        *,
        language_code: str,
        sample_rate_hz: int,
        frames: Sequence[bytes],
    ) -> Sequence[bytes]:
        if language_code != "nl-NL" or sample_rate_hz != REQUIRED_SAMPLE_RATE_HZ:
            raise TranscribeTransportError("invalid_stream_params", "Expected nl-NL at 16000 Hz")
        non_empty_audio = 0
        for raw_frame in frames:
            decoded = decode_eventstream_frame(raw_frame)
            if decoded.headers.get(":event-type") != "AudioEvent":
                raise TranscribeTransportError("unexpected_frame", "Expected AudioEvent frame")
            if decoded.payload:
                non_empty_audio += 1

        if non_empty_audio == 0:
            return ()

        responses: list[bytes] = []
        if self._emit_partial:
            first_words = " ".join(self._transcript.split()[:3])
            responses.append(
                encode_transcript_event_frame(
                    transcript=first_words,
                    is_partial=True,
                    confidence=self._confidence,
                )
            )
        responses.append(
            encode_transcript_event_frame(
                transcript=self._transcript,
                is_partial=False,
                confidence=self._confidence,
            )
        )
        return tuple(responses)


def _zero_all_chunks(
    chunks: Sequence[PcmAudioChunk],
    *,
    deleted_iso: str,
) -> list[ChunkDeletionReceipt]:
    return [
        ChunkDeletionReceipt(
            sequence_id=chunk.sequence_id,
            bytes_zeroed=chunk.zero_immediately(),
            deleted_immediately=True,
            deleted_at=deleted_iso,
        )
        for chunk in chunks
    ]


class ConsentedAudioChunkTranscriber:
    """Bounded push-to-talk Dutch audio chunk transcriber (INT-NT-004, SEC-NT-004).

    Enforces NextTime SPEC.md boundaries:
    - Rejects continuous/always-on or Bee background microphone streams (SPEC.md §1.2).
    - Rejects unapproved Whisper substitution (INT-NT-004, SPEC_DEVIATIONS.md Rule 3).
    - Requires explicit streaming transcription consent before sending audio frames.
    - Zeroes and deletes every raw PCM audio chunk immediately; retains 0 PCM bytes.
    """

    def __init__(
        self,
        *,
        transport: TranscribeEventStreamTransportPort | None = None,
        provider: str = "aws_transcribe_streaming",
    ) -> None:
        if provider != "aws_transcribe_streaming":
            raise UnsupportedTranscriptionProviderError(
                "Only aws_transcribe_streaming is permitted by INT-NT-004; "
                "Whisper or unverified providers require an approved SPEC_DEVIATIONS.md entry."
            )
        self._transport: TranscribeEventStreamTransportPort = (
            transport or UnconfiguredAwsTranscribeTransport()
        )

    async def transcribe_ptt_turn(
        self,
        *,
        learner_id: str,
        rehearsal_id: str,
        turn_event_id: str,
        chunks: Sequence[PcmAudioChunk],
        streaming_transcription_consented: bool,
        consent_revoked: bool = False,
        capture_origin: str = "iphone_rehearsal_ptt",
        continuous_stream: bool = False,
        save_self_excerpt: bool = False,
        transcribed_at: datetime | None = None,
    ) -> TurnTranscriptionResult:
        now = (transcribed_at or datetime.now(UTC)).astimezone(UTC)
        now_iso = _iso_utc(transcribed_at or now)

        if continuous_stream or capture_origin not in ALLOWED_PTT_CAPTURE_ORIGINS:
            _zero_all_chunks(chunks, deleted_iso=now_iso)
            raise CovertCaptureProhibitedError("continuous_microphone_streaming_prohibited")

        try:
            require_processing_consent(
                consent_attested=streaming_transcription_consented,
                revoked=consent_revoked,
                retention_policy_present=True,
            )
        except ConsentError:
            _zero_all_chunks(chunks, deleted_iso=now_iso)
            raise

        if not chunks:
            raise ValueError("At least one push-to-talk PCM audio chunk is required")

        encoded_frames: list[bytes] = []
        receipts: list[ChunkDeletionReceipt] = []
        seen_sequences: set[int] = set()
        last_seq = 0
        total_duration_ms = 0.0

        try:
            for chunk in chunks:
                if chunk.sequence_id < 1:
                    raise ValueError("Chunk sequence_id must be >= 1")
                if chunk.sequence_id in seen_sequences:
                    chunk.zero_immediately()
                    continue
                if chunk.sequence_id <= last_seq:
                    raise ValueError("Out-of-order audio chunk sequence_id")
                if (
                    chunk.sample_rate_hz != REQUIRED_SAMPLE_RATE_HZ
                    or chunk.channels != REQUIRED_CHANNELS
                    or chunk.sample_width_bytes != REQUIRED_SAMPLE_WIDTH_BYTES
                ):
                    raise ValueError("Audio chunk must be 16kHz mono 16-bit PCM")
                raw_len = len(chunk.pcm_buffer)
                if raw_len == 0 or raw_len > MAX_CHUNK_BYTES or raw_len % 2 != 0:
                    raise ValueError("Invalid PCM chunk byte length")

                bytes_per_ms = (
                    chunk.sample_rate_hz * chunk.channels * chunk.sample_width_bytes
                ) / 1000.0
                total_duration_ms += raw_len / bytes_per_ms
                if total_duration_ms > MAX_PTT_TURN_DURATION_MS:
                    raise ValueError("Push-to-talk turn exceeds 30-second bound")

                encoded_frames.append(encode_audio_event_frame(bytes(chunk.pcm_buffer)))
                zeroed = chunk.zero_immediately()
                receipts.append(
                    ChunkDeletionReceipt(
                        sequence_id=chunk.sequence_id,
                        bytes_zeroed=zeroed,
                        deleted_immediately=True,
                        deleted_at=now_iso,
                    )
                )
                seen_sequences.add(chunk.sequence_id)
                last_seq = chunk.sequence_id
        except Exception:
            _zero_all_chunks(chunks, deleted_iso=now_iso)
            raise

        # Append end-of-stream empty AudioEvent frame and clear frame list after exchange
        encoded_frames.append(encode_audio_event_frame(b""))
        try:
            response_frames = await self._transport.exchange_audio_frames(
                language_code="nl-NL",
                sample_rate_hz=REQUIRED_SAMPLE_RATE_HZ,
                frames=tuple(encoded_frames),
            )
        finally:
            encoded_frames.clear()

        if any(len(c.pcm_buffer) != 0 for c in chunks):
            raise RuntimeError("pcm_retention_invariant_violated")

        partial_count = 0
        final_parts: list[str] = []
        final_confidences: list[float] = []
        for resp_frame in response_frames:
            for seg in parse_transcript_event_frame(resp_frame):
                if seg.is_partial:
                    partial_count += 1
                elif seg.transcript:
                    final_parts.append(seg.transcript)
                    final_confidences.append(seg.confidence)

        combined_text = " ".join(final_parts).strip()
        mean_conf = (
            round(sum(final_confidences) / len(final_confidences), 4) if final_confidences else 0.0
        )
        delete_after = None if save_self_excerpt else _iso_utc(now + timedelta(hours=24))

        return TurnTranscriptionResult(
            turn_event_id=turn_event_id,
            rehearsal_id=rehearsal_id,
            learner_id=learner_id,
            transcript=combined_text,
            confidence=mean_conf,
            below_confidence_threshold=(mean_conf < REHEARSAL_MAPPING_CONFIDENCE_THRESHOLD),
            partial_count=partial_count,
            chunk_receipts=tuple(receipts),
            retained_pcm_bytes=0,
            transcript_delete_after=delete_after,
            provider="aws_transcribe_streaming",
            provenance=self._transport.provenance,
        )


AudioChunkTranscriber = ConsentedAudioChunkTranscriber
