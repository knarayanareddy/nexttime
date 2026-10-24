# NextTime

**Track:** Bee
**Status:** speaker turn separation (`FR-NT-003`), verified in-memory deletion/revocation worker (`FR-NT-014`), local CLI/spool Bee ingestion bridge (`FR-NT-001` local slice), bounded breakdown-proposal gateway (`FR-NT-005` / `INT-NT-005`), user-confirmed calendar candidate sync (`services/calendar`, `FR-NT-008` / `INT-NT-006`), consented push-to-talk audio chunk transcriber (`services/rehearsal`, `INT-NT-004` / `SEC-NT-004`), iPhone turn-boundary haptic event generator (`packages/domain/nexttime_domain/haptics.py`), and FastAPI `/v1` rehearsal & progress backend (`services/api`) validated locally; live Bee hardware, live AWS network transports, durable storage, and native iOS SwiftUI app remain unimplemented
**Agent entry point:** [`AGENTS.md`](./AGENTS.md)

NextTime evaluates authentic, consented functional-communication attempts in Dutch (`nl` with `en` support) and keeps 90-second rehearsal practice strictly separate from real-world mastery.

## Local commands

Requirements: Python 3.12 (`>=3.12,<3.13`), `uv`, and `make`. Dependencies are pinned in `pyproject.toml` and locked in `uv.lock`; `packages/domain` remains pure Python stdlib.

```bash
make setup                 # uv sync --frozen and safe .env.local
make dev                   # deterministic domain + rehearsal walking skeleton
make dev-api               # run FastAPI /v1 server on 0.0.0.0:8000
make lint typecheck        # ruff check + format check + strict mypy across all packages/services
make test-unit             # >=90% branch coverage across domain, adapters, workers, and API
make test-contract         # JSON Schema, OpenAPI 3.1 zero-drift, and stdlib boundary checks
make test-security         # IDOR/authz/privacy/revocation tests + pip-audit --skip-editable
make build                 # build sdist and wheel
```

Native iOS, live Bee hardware, PostgreSQL/S3 integration, CDK infrastructure, E2E, and deployment targets intentionally exit non-zero until those external systems are implemented.

## Workspace map

| Path | Purpose |
| --- | --- |
| [`SPEC.md`](./SPEC.md) | Canonical requirements and vertical slices |
| [`schemas/real-world-attempt.schema.json`](./schemas/real-world-attempt.schema.json) | Controlled minimized-attempt JSON Schema |
| [`packages/domain/nexttime_domain/`](./packages/domain/nexttime_domain/) | Pure-stdlib speaker separation, consent gates, minimization, rubric evaluation, breakdown proposals, learner review, deterministic 90s rehearsal, turn-boundary iPhone haptic cues (`SHORT_DOUBLE`, `STEADY_BUZZ`), linked progress, and deletion models |
| [`packages/model-gateway/nexttime_model_gateway/`](./packages/model-gateway/nexttime_model_gateway/) | Bounded `ModelGateway` port, repair/budget/circuit guards, and deterministic breakdown fallback |
| [`packages/bee-adapter/nexttime_bee_adapter/`](./packages/bee-adapter/nexttime_bee_adapter/) | `BeeAdapter` protocol, bounded `BeeCliAdapter`, log redaction, and fixture `LocalSpoolBeeAdapter` |
| [`services/calendar/nexttime_calendar/`](./services/calendar/nexttime_calendar/) | RFC 5545 `.ics` and incremental read-only Google Calendar candidate adapter (3 Dutch situations, `SEC-NT-006` exclusion, zero raw title retention, learner-confirmed `PreMeetingRehearsalCard` queue) |
| [`services/rehearsal/nexttime_rehearsal/`](./services/rehearsal/nexttime_rehearsal/) | Bounded push-to-talk Dutch audio chunk transcriber with AWS `vnd.amazon.eventstream` CRC32 codec, streaming consent gate, covert-mic rejection, and immediate in-place PCM zeroing |
| [`services/bee-bridge/nexttime_bee_bridge/`](./services/bee-bridge/nexttime_bee_bridge/) | Local consented Bee transcript ingestion listener (`nexttime-bee-bridge` CLI + lease cursor) |
| [`services/minimization/nexttime_minimization/`](./services/minimization/nexttime_minimization/) | `InMemoryArtifactStore` and 4-stage `DeletionPipelineWorker` (revocation, TTL sweeps, overdue alarms, tombstones) |
| [`services/api/nexttime_api/`](./services/api/nexttime_api/) | FastAPI `/v1` backend for consents, ingestion, attempt review, calendar candidate sync & confirmation, recurrence/rehearsal sessions, PTT chunk transcription, rehearsal haptics, learner progress history, export, and account deletion |
| [`packages/contracts/`](./packages/contracts/) | Attempt schema copy, model-gateway schemas, and drift-checked `openapi.v1.json` |
| [`packages/rubrics/`](./packages/rubrics/) | Draft educator-review-required situation rubrics |
| [`tests/`](./tests/) | Unit, contract, and security/privacy test suites |
| [`docs/`](./docs/) | Preflight, architecture, data inventory, dependencies, and validation records |

## External blockers

Physical Bee/Apple Watch hardware and verified official Bee CLI/MCP subcommands, native iOS 17+ SwiftUI client, live AWS Polly/Transcribe/Bedrock/S3/KMS/RDS infrastructure, educator-signed rubric review, and participant evaluation remain blocked or unimplemented. Maintain [`SPEC_TRACEABILITY.md`](./SPEC_TRACEABILITY.md) and [`SPEC_DEVIATIONS.md`](./SPEC_DEVIATIONS.md), and never claim rehearsal or fixture results as authentic learner outcomes.
