# NextTime validation record

Recorded 2026-10-24. Project tests, OpenAPI contract checks, wheel/sdist builds, fixture execution, and dependency vulnerability audits ran under CPython 3.12.12 with `uv` and OpenSSL support.

## Passing checks

| Check | Result |
| --- | --- |
| Ruff lint + format check | Passed (`ruff check .` and `ruff format --check .`) |
| Strict mypy | Passed (`0` issues across `34` source files in `packages/` and `services/`) |
| Unit tests | `85/85` passed |
| Branch-aware coverage (`domain`, `model-gateway`, `bee-adapter`, `minimization`, `bee-bridge`, `api`) | `91.87%` branch coverage (exceeds `90%` gate) |
| Canonical schema & OpenAPI 3.1 contract tests | `12/12` passed (`real-world-attempt`, `breakdown_propose.v1`, `openapi.v1.json` zero-drift, and pure-stdlib domain AST check) |
| Privacy & authorization security tests | `4/4` passed (`AC-NT-019` cross-learner IDOR denial, `SEC-NT-003` learner/bridge token scope separation, consent revocation cascade, and sensitive-topic fail-closed checks) |
| Third-party dependency audit | `0` known vulnerabilities (`uv run pip-audit --skip-editable`) |
| Source distribution + wheel | Built cleanly (`uv build`) |
| Fixture walking path (`make dev`) | Passed |
| **Total automated tests** | **`101/101` passed** |

## Validated local slices

1. **Speaker turn separation (`FR-NT-003`):** `packages/domain/nexttime_domain/speaker_separation.py` separates learner turns from other speakers across `bee_speaker_labels`, `aws_diarization` (requiring an enrolled speaker cluster before attributing learner speech), `learner_labeled`, and `self_only`. Temporal crosstalk overlaps, low speaker/ASR confidence, and unrecognized labels downgrade to `unknown` (`review_required=True`) and are excluded from learner excerpts.
2. **Deletion & consent revocation worker (`FR-NT-014`, `SEC-NT-004`, `SEC-NT-005`, `NFR-NT-004`):** `packages/domain/nexttime_domain/deletion.py` and `services/minimization/nexttime_minimization/` execute verified 4-stage deletion (`transient_source`, `audio_and_rehearsal`, `caches_and_prompts`, `derived_learner_state`) with stage receipts, fault/alarm reporting and idempotent retry recovery, >24h overdue transient source alarms, immediate raw rehearsal audio deletion, 24h unsaved rehearsal transcript sweeps, tombstones, recent-auth-gated account deletion, and minimized learner data export.
3. **Local Bee CLI/spool ingestion bridge (`FR-NT-001` local slice):** `packages/bee-adapter/` and `services/bee-bridge/` implement `BeeAdapter`, `BeeCliAdapter` (`asyncio.create_subprocess_exec`, 30s timeout, 1 MiB output cap, secret/email/phone/utterance log redaction, zero invented Bee CLI flags), `LocalSpoolBeeAdapter` (`provenance="fixture"`), and `BeeIngestionBridge` (consent/revocation gate, lease locking, duplicate-reference idempotency, fail-closed transient cleanup on sensitive content, and post-persistence cursor acknowledgement).
4. **FastAPI `/v1` rehearsal & learner progress backend (`services/api`):** Exposes `/v1/consents`, `/v1/attempts/ingest`, `/v1/attempts`, `/v1/attempts/{id}/review`, `/v1/attempts/{id}/exclude`, `/v1/recurrences`, `/v1/recurrences/{id}/calendar-link`, `/v1/recurrences/{id}/rehearsal`, `/v1/rehearsals/{id}`, `/v1/rehearsals/{id}/turns`, `/v1/rehearsals/{id}/complete`, `/v1/progress`, `/v1/export`, and `/v1/account` with RFC 9457 `application/problem+json` errors, 24-hour `Idempotency-Key` enforcement, optimistic `expected_version` / `If-Match` concurrency, 15-minute signed counterpart audio URLs, push-to-talk + accessible `tap`/`text` rehearsal turns that never mutate authentic skill mastery (`AC-NT-015`), and non-gamified linked-attempt `OutcomeComparison` progress history (`AC-NT-016`).

## Reproduce

```bash
make lint typecheck test-unit test-contract test-security build dev
```

## Not evidenced

Live Bee CLI/MCP commands and physical Bee/Apple Watch hardware (`AC-NT-020`), native iOS SwiftUI rehearsal UI/notifications (`FR-NT-010`, `INT-NT-003`), live AWS Polly/Transcribe/Bedrock calls (`INT-NT-004`, `INT-NT-005`), durable PostgreSQL/S3/KMS storage and CDK infrastructure (`make test-integration`, `make test-infra`, `make test-e2e`), ASR accent/device study slices (`SEC-NT-008`), and latency/SLO benchmarks (`NFR-NT-001`, `NFR-NT-002`) remain unexecuted.
