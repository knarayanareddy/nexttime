# NextTime

**Track:** Bee
**Status:** consent-aware minimization, learner review/skill update, deterministic rehearsal, and linked-attempt comparison slices validated locally; Bee/iOS applications not implemented
**Agent entry point:** [`AGENTS.md`](./AGENTS.md)

NextTime evaluates authentic, consented functional-communication attempts and keeps rehearsal evidence separate from real-world mastery.

## Local foundation

Requirements: Python 3.12, `uv`, and `make`. Dependencies are locked in `uv.lock`; runtime domain code has no third-party runtime dependency.

```bash
make setup                 # uv sync --frozen and safe .env.local
make dev                   # fictional minimized-attempt + rehearsal fixtures
make lint typecheck
make test-unit test-contract test-security
make build
```

Unit tests enforce at least 90% branch-aware domain coverage. `make test-security` runs privacy tests and `pip-audit --skip-editable`; the editable local distribution is not a third-party dependency. The audit runner needs working SSL/TLS.

Native iOS, Bee integration, database/transient storage, infrastructure, E2E, and deployment targets intentionally fail while their real implementations are absent.

## Workspace map

| Path | Purpose |
| --- | --- |
| [`SPEC.md`](./SPEC.md) | Canonical requirements and vertical slices |
| [`schemas/real-world-attempt.schema.json`](./schemas/real-world-attempt.schema.json) | Canonical minimized-attempt contract |
| [`packages/domain/nexttime_domain/`](./packages/domain/nexttime_domain/) | Consent gates, learner-only minimization, rubric evaluation, learner review/correction, bounded authentic skill updates, deterministic rehearsal, and linked-attempt progress projection |
| [`packages/contracts/`](./packages/contracts/) | Byte-identical schema copy used by contract tests |
| [`packages/rubrics/`](./packages/rubrics/) | Draft educator-review-required rubric content |
| [`tests/`](./tests/) | Unit, schema-contract, and privacy-boundary tests |
| [`docs/`](./docs/) | Preflight, architecture, data inventory, dependencies, and validation |
| [`ui/`](./ui/) | Static design guidance, not an iOS application |

## External blockers

Actual Bee/Apple Watch and documented CLI/MCP connectivity, iOS/Swift tooling, Polly/Transcribe/Bedrock, persistence/deletion workers, participants, accent/device evaluation, and release evidence remain blocked or unimplemented. The original fixture remains unconfirmed and does not update skill state; a separate synthetic reviewed copy demonstrates one bounded update, not real learner progress. The rehearsal fixture targets a confirmed breakdown, schedules around quiet hours, deduplicates notification/turn events, and leaves authentic skills unchanged; its notification is explicitly not sent. A second synthetic reviewed attempt now exercises same-recurrence/rubric comparison, criterion evidence deltas, uncertainty, replay-safe progress recording, and separate rehearsal history. It is not a real Bee attempt or a causal learning result.

Maintain [`SPEC_TRACEABILITY.md`](./SPEC_TRACEABILITY.md) and [`SPEC_DEVIATIONS.md`](./SPEC_DEVIATIONS.md). Never use rehearsal or fixture data as authentic learner progress.
