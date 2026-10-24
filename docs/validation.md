# NextTime validation record

Recorded 2026-10-24. Project tests, OpenAPI contract checks, wheel/sdist builds, fixture execution, and dependency vulnerability audits ran under CPython 3.12.12 with `uv` and OpenSSL support.

## Passing checks

| Check | Result |
| --- | --- |
| Ruff lint + format check | Passed (`ruff check .` and `ruff format --check .`) |
| Strict mypy | Passed (`0` issues across `45` source files in `packages/` and `services/`) |
| Unit tests | `105/105` passed |
| Branch-aware coverage (`domain`, `model-gateway`, `bee-adapter`, `calendar`, `rehearsal`, `minimization`, `bee-bridge`, `api`) | `92.64%` branch coverage (exceeds `90%` gate) |
| Canonical schema & OpenAPI 3.1 contract tests | `12/12` passed (`real-world-attempt`, `breakdown_propose.v1`, `openapi.v1.json` zero-drift across 24 REST routes + `/v1/rehearsal/ws` WebSocket check, and pure-stdlib domain AST check) |
| Privacy & authorization security tests | `5/5` passed (`AC-NT-019` cross-learner IDOR denial, `SEC-NT-003` learner/bridge token scope separation, consent revocation cascade, calendar zero-title/sensitive-topic exclusion, immediate PCM zeroing, and Bee haptic command prohibition) |
| Third-party dependency audit | `0` known vulnerabilities (`uv run pip-audit --skip-editable`) |
| Source distribution + wheel + static web bundle | Built cleanly (`uv build` + `pnpm --dir apps/web build`) |
| Fixture walking path (`make dev`), CLI simulator (`scripts/simulate_rehearsal.py`), & Playwright demo recording (`docs/media/nexttime-demo.webm`) | Passed |
| **Total automated tests** | **`122/122` passed** |

## Validated local slices

1. **Speaker turn separation (`FR-NT-003`):** `packages/domain/nexttime_domain/speaker_separation.py` separates learner turns from other speakers across `bee_speaker_labels`, `aws_diarization` (requiring an enrolled speaker cluster before attributing learner speech), `learner_labeled`, and `self_only`. Temporal crosstalk overlaps, low speaker/ASR confidence, and unrecognized labels downgrade to `unknown` (`review_required=True`) and are excluded from learner excerpts.
2. **Deletion & consent revocation worker (`FR-NT-014`, `SEC-NT-004`, `SEC-NT-005`, `NFR-NT-004`):** `packages/domain/nexttime_domain/deletion.py` and `services/minimization/nexttime_minimization/` execute verified 4-stage deletion (`transient_source`, `audio_and_rehearsal`, `caches_and_prompts`, `derived_learner_state`) with stage receipts, fault/alarm reporting and idempotent retry recovery, >24h overdue transient source alarms, immediate raw rehearsal audio deletion, 24h unsaved rehearsal transcript sweeps, tombstones, recent-auth-gated account deletion, and minimized learner data export.
3. **Local Bee CLI/spool ingestion bridge (`FR-NT-001` local slice):** `packages/bee-adapter/` and `services/bee-bridge/` implement `BeeAdapter`, `BeeCliAdapter` (`asyncio.create_subprocess_exec`, 30s timeout, 1 MiB output cap, secret/email/phone/utterance log redaction, zero invented Bee CLI flags), `LocalSpoolBeeAdapter` (`provenance="fixture"`), and `BeeIngestionBridge` (consent/revocation gate, lease locking, duplicate-reference idempotency, fail-closed transient cleanup on sensitive content, and post-persistence cursor acknowledgement).
4. **Calendar ingestion adapter & pre-meeting rehearsal card queue (`FR-NT-008`, `INT-NT-006`, `SEC-NT-006`, `AC-NT-012`):** `services/calendar/nexttime_calendar/` parses RFC 5545 `.ics` feeds and incremental read-only Google Calendar OAuth payloads, drops sensitive topics (`SEC-NT-006`), stores only `calref_<24hex>` opaque IDs with zero raw title retention, matches the 3 supported Dutch situations where a confirmed breakdown exists, enforces quiet-hours notification choices (`21:00–08:00` local), and queues `PreMeetingRehearsalCard` items only after explicit learner confirmation.
5. **Consented push-to-talk audio chunk transcriber (`INT-NT-004`, `SEC-NT-004`, `SPEC.md §1.2`):** `services/rehearsal/nexttime_rehearsal/` frames 16 kHz mono 16-bit s16le PCM chunks (`<=64 KiB` chunk, `<=30s` turn) using AWS `application/vnd.amazon.eventstream` binary CRC32 framing, rejects continuous/wearable covert mic streams and unapproved Whisper providers, enforces streaming consent, zeroes every PCM `bytearray` buffer in place immediately (`retained_pcm_bytes == 0`), flags `<0.75` confidence, and schedules 24h unsaved transcript deletion.
6. **Turn-boundary iPhone haptic feedback generator (`INT-NT-003`, `SPEC.md §1.3/§4.1/§8.2`, `ui/DESIGN.md §6–7`):** `packages/domain/nexttime_domain/haptics.py` maps push-to-talk and rehearsal turn boundaries to `SHORT_DOUBLE` and `STEADY_BUZZ` pulse sequences on `ios_rehearsal_client`, rejects unverified Bee wearable haptic commands, blocks real-time mid-speech pacing interruptions and repeated haptics during active recording, and honors system haptic/accessibility settings while preserving visual captions and VoiceOver announcements.
7. **FastAPI `/v1` rehearsal & learner progress backend + WebSocket streaming (`services/api`, `scripts/simulate_rehearsal.py`):** Exposes `/v1/rehearsal/ws` (bidirectional WebSocket connecting `AudioChunkTranscriber`, `ModelGateway` / `GatewayBreakdownExtractor`, and `PacingHapticsEngine` with immediate turn-boundary PCM deletion) plus `/v1/consents`, `/v1/attempts/ingest`, `/v1/attempts`, `/v1/attempts/{id}/review`, `/v1/attempts/{id}/exclude`, `/v1/calendar/ics-sync`, `/v1/calendar/candidates`, `/v1/calendar/candidates/{id}/confirm`, `/v1/rehearsal-cards`, `/v1/recurrences`, `/v1/recurrences/{id}/calendar-link`, `/v1/recurrences/{id}/rehearsal`, `/v1/rehearsals/{id}`, `/v1/rehearsals/{id}/turns`, `/v1/rehearsals/{id}/transcribe-chunks`, `/v1/rehearsals/{id}/haptics`, `/v1/rehearsals/{id}/complete`, `/v1/progress`, `/v1/export`, and `/v1/account` with RFC 9457 `application/problem+json` errors, 24-hour `Idempotency-Key` enforcement, optimistic `expected_version` / `If-Match` concurrency, 15-minute signed counterpart audio URLs, push-to-talk + accessible `tap`/`text` rehearsal turns that never mutate authentic skill mastery (`AC-NT-015`), and non-gamified linked-attempt `OutcomeComparison` progress history (`AC-NT-016`).

## Reproduce

```bash
make lint typecheck test-unit test-contract test-security build dev
```

## Not evidenced

Live Bee CLI/MCP commands and physical Bee/Apple Watch hardware (`AC-NT-020`), native iOS SwiftUI rehearsal UI/notifications (`FR-NT-010`, `INT-NT-003`), live AWS Polly/Transcribe/Bedrock calls (`INT-NT-004`, `INT-NT-005`), durable PostgreSQL/S3/KMS storage and CDK infrastructure (`make test-integration`, `make test-infra`, `make test-e2e`), ASR accent/device study slices (`SEC-NT-008`), and latency/SLO benchmarks (`NFR-NT-001`, `NFR-NT-002`) remain unexecuted.
