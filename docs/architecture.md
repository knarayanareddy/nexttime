# NextTime architecture

## Implemented boundaries

```text
Bee CLI / Local Spool ──► packages/bee-adapter (BeeAdapter protocol, 30s bounded CLI, log redaction)
                                     │
                                     ▼
                services/bee-bridge (consent/revocation gate, lease cursor, post-persist ack)
                                     │
                                     ▼
        packages/domain (pure stdlib: speaker separation, minimization, rubric, breakdowns,
                         review, deterministic 90s rehearsal, linked progress, deletion models)
                                     │
                 ┌───────────────────┴───────────────────┐
                 ▼                                       ▼
   services/minimization                   packages/model-gateway
   (4-stage DeletionPipelineWorker,        (bounded ModelGateway, repair retry,
    24h transient/rehearsal sweeps,         budget/circuit guards, deterministic
    overdue alarms, tombstones)             breakdown fallback)
                 └───────────────────┬───────────────────┘
                                     ▼
                     services/api (FastAPI /v1 REST service,
                     RFC 9457 problem+json, 24h Idempotency-Key,
                     learner vs bridge token isolation, OpenAPI 3.1)
```

- `packages/domain/nexttime_domain/` (pure Python stdlib, AST-verified free of web/ORM/cloud imports):
  - `speaker_separation.py` (`FR-NT-003`): separates learner vs other-speaker turns across `bee_speaker_labels`, `aws_diarization` (requiring an enrolled speaker cluster to assert learner speech), `learner_labeled`, and `self_only`, downgrading temporal crosstalk overlaps and low-confidence labels to `unknown` (`review_required=True`).
  - `transcripts.py`, `minimization.py`, and `breakdowns.py` (`FR-NT-002`, `FR-NT-004`, `FR-NT-005`): enforce capture-mode consent, drop non-learner speech, scrub deterministic identifiers, block sensitive-situation lexical markers, and propose at most three self-evidenced breakdowns.
  - `rubric.py`, `review.py`, `skills.py`, `rehearsal.py`, `haptics.py`, and `progress.py` (`FR-NT-006`..`FR-NT-013`, `FR-NT-015`, `INT-NT-003`): evaluate authentic outcomes conservatively, require learner review before Beta skill updates, schedule quiet-hours-aware 90-second Dutch rehearsals that never mutate authentic mastery, generate bounded iPhone turn-boundary haptic pulse sequences (`SHORT_DOUBLE`, `STEADY_BUZZ`) without inventing Bee wearable commands or interrupting mid-utterance, and compute non-causal linked-attempt `OutcomeComparison` deltas.
  - `deletion.py` (`FR-NT-014`): defines `ConsentGrant`, `TransientTranscriptRecord`, `DeletionJob`, `StageReceipt`, `DeletionTombstone`, `DeletionStorePort`, overdue transient source scanning (`NFR-NT-004`, `SEC-NT-005`), and minimized learner export (`build_learner_export`).
- `services/calendar/nexttime_calendar/` (`FR-NT-008`, `INT-NT-006`, `SEC-NT-006`, `AC-NT-012`): implements RFC 5545 `.ics` feed synchronization (`ICalendarFeedAdapter`), incremental read-only Google Calendar OAuth synchronization (`GoogleCalendarAdapter` enforcing `calendar.events.readonly`), sensitive-topic exclusion + 3-situation classification (`classify_calendar_event_situation`), SHA-256 opaque event references (`calref_*` with zero raw title/attendee retention), and learner-confirmed `CalendarSyncService` / `PreMeetingRehearsalCard` queueing.
- `services/rehearsal/nexttime_rehearsal/` (`INT-NT-004`, `SEC-NT-004`, `SPEC.md §1.2`): implements AWS `application/vnd.amazon.eventstream` binary framing + CRC32 verification (`aws_eventstream.py`) and `ConsentedAudioChunkTranscriber` (`transcriber.py`) enforcing iPhone push-to-talk origin (rejecting continuous/wearable mic streams and unapproved Whisper providers), streaming consent attestation, 16 kHz mono 16-bit PCM bounds (`<=64 KiB` chunk, `<=30s` turn), immediate in-place `bytearray` PCM zeroing (`retained_pcm_bytes == 0`), 24h unsaved transcript TTL, and fail-closed `UnconfiguredAwsTranscribeTransport` + `DeterministicFixtureTranscribeTransport`.
- `packages/bee-adapter/` & `services/bee-bridge/` (`FR-NT-001` local slice): define the `BeeAdapter` protocol, `BeeCliAdapter` (`asyncio.create_subprocess_exec` with explicit operator-supplied args, 30s timeout, 1 MiB output cap, and secret/utterance log redaction), `LocalSpoolBeeAdapter` (fixture-labeled local JSON spool), and `BeeIngestionBridge` (consent/revocation gate, per-learner lease cursor, immediate transient source purge after draft persistence or failure, and source acknowledgement only after persistence).
- `services/minimization/` (`FR-NT-014`, `SEC-NT-004`, `SEC-NT-005`): implements `InMemoryArtifactStore` and `DeletionPipelineWorker` executing verified 4-stage deletion (`transient_source` -> `audio_and_rehearsal` -> `caches_and_prompts` -> `derived_learner_state`), retryable failure alarms, immediate raw rehearsal audio deletion, 24-hour unsaved rehearsal transcript sweeps, consent revocation cascades, and account deletion tombstones.
- `services/api/` & `packages/contracts/openapi.v1.json`: FastAPI `/v1` backend + `/v1/rehearsal/ws` bidirectional WebSocket (`ws.py`) connecting `AudioChunkTranscriber`, `ModelGateway` (`GatewayBreakdownExtractor`), and `PacingHapticsEngine`, alongside `scripts/simulate_rehearsal.py` for terminal Dutch conversation simulation without Xcode/SwiftUI. Exposes consent, bridge ingestion, attempt listing/review/exclusion, `.ics` calendar candidate sync/confirmation & pre-meeting rehearsal cards, recurrence & optional calendar-link scheduling, rehearsal session generation/turn mapping/PTT chunk transcription/haptics/completion, non-gamified learner progress history (`GET /v1/progress`), export, and account deletion with RFC 9457 `application/problem+json`, 24-hour `Idempotency-Key` protection, optimistic `expected_version` / `If-Match` concurrency, and strict learner vs bridge token scoping.

## Deliberately absent

No live Bee CLI/MCP binary or physical Bee/Apple Watch hardware connection, native iOS SwiftUI app, live Amazon Polly/Transcribe/Bedrock SDK calls, PostgreSQL/S3/KMS cloud persistence, CDK stacks, or APNs/local device notifications are implemented or claimed.
