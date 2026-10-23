# NextTime validation record

Recorded 2026-10-23. Project tests, builds, fixture execution, and dependency audit ran under source-built CPython 3.12.12 with zlib, SQLite, and OpenSSL support; Python 3.11 was not substituted.

## Passing checks

| Check | Result |
| --- | --- |
| Ruff lint + format check | Passed |
| Strict mypy | Passed; 15 source files |
| Unit tests | 66/66 passed |
| Branch-aware domain + gateway coverage | 95.51%; required gate 90% retained |
| Canonical schema/contract tests | 10/10 passed; schema copy byte-identical and gateway input/output contracts verified |
| Privacy/security tests | 3/3 passed |
| Third-party dependency audit | No known vulnerabilities; editable local package skipped |
| Source distribution + wheel | Both built successfully |
| Fixture walking path | Consent-aware minimization, duplicate-source safety, bounded self-evidenced breakdown proposal (`<= 3` breakdowns with ASR/model confidence), unconfirmed no-update outcome, two synthetic reviewed attempts with linked comparison/replay-safe progress, and a 90-second/two-turn rehearsal with quiet-hours scheduling and duplicate-safe fixture notification requests |
| **Total tests** | **79/79 passed** |

The synthetic transcript path excludes other-speaker and ambiguous turns, excludes other-speaker-like turns from self memos, applies narrow deterministic identifier redaction, and always returns `review_required`. Cached source references remain subject to consent and revocation checks. The source deletion deadline is computed as 24 hours after successful minimization; it is metadata for a future deletion adapter, not proof of deletion.

The breakdown-proposal slice (`FR-NT-005`) accepts only minimized self-excerpts (`other_speaker_transcript_retained=False`), blocks lexical markers for excluded sensitive situations (`SEC-NT-006`), proposes at most three functional breakdowns with verified learner-owned self-evidence excerpts and conservative combined `min(asr_confidence, model_confidence)` confidence, downgrades criterion assertions below `0.75` to `unknown` (`NFR-NT-006`, `SEC-NT-008`), rejects prohibited emotion/identity/accent/fluency-score inferences (`SEC-NT-007`), and produces schema-valid `awaiting_learner_review` attempt drafts ready for `AttemptReviewService`. `GatewayBreakdownExtractor` exercises the shared bounded `ModelGateway` (`INT-NT-005`: input/output schema validation, at most one repair retry, token/cost/timeout budgets, circuit breaker, safe telemetry without raw transcripts, and labeled deterministic fallback when AWS Bedrock is unconfigured or invalid). No live AWS Bedrock call is claimed.

The original rubric fixture still returns `partial`, `confirmation_required: true`, `required_confirmation_unresolved`, and `authentic_skill_update: false`. A separate in-memory reviewed copy requires every proposed breakdown to be reviewed before attempt confirmation, recomputes corrected criterion outcomes, and applies confirmed breakdown-skill updates once per skill. Tests prove rejected breakdowns and unknown outcomes do not update mastery, contradictory confirmation evidence fails, duplicate confirmation does not reapply updates, and cross-learner/stale-version access fails. No authentic learner outcome is inferred.

The linked-progress fixture accepts only confirmed, minimized authentic-attempt documents with the same learner, recurrence, situation, and rubric, ordered by recording time. It reports criterion evidence deltas and uncertainty, distinguishes explicit improvement from new positive evidence after an unknown prior result, and makes no causal claim. Replaying a confirmed review result returns the existing comparison without reapplying its supplied skill projection; rehearsal metadata remains separate and cannot change authentic skills. Tests also reject unconfirmed, cross-learner, mislinked, out-of-order, and conflicting replay inputs; each projection service is explicitly scoped to its first confirmed learner. Both walking attempts are synthetic reviewed fixtures, not Bee observations.

The deterministic rehearsal service targets only a confirmed `confirmation_missing` breakdown, requires a learner-marked recurrence, and offers an explicit quiet-hours choice. Its fixture notification is `fixture_request_not_sent`; duplicate notification/turn events are safe, ambiguous turns progress through repair/easier-alternative support to abandonment, and text/tap modes are supported. Completing the two-turn rehearsal leaves authentic skill states unchanged. No audio, iOS, Bedrock, Polly, actual notification, or learning outcome is claimed.

## Reproduce

```bash
make lint typecheck test-unit test-contract test-security build dev
```

Use a Python 3.12 runtime with zlib, SQLite, and SSL/TLS so the normal coverage and audit commands are self-contained. Do not skip the audit or run project tests under 3.11.

## Not evidenced

No Bee/Apple Watch data, CLI/MCP ingestion, diarization model, iOS behavior, authentic transcript, actual transient deletion, revocation worker, durable learner/attempt/idempotency store, database scan, rehearsal targeting/audio, ASR accent/device evaluation, participant outcome, performance, deployment, or release behavior was executed. Confirmed-breakdown updates are deliberately narrow; durable/API/iOS progress presentation, criterion-wide skill-model expansion, native rehearsal audio/notification delivery, and deletion adapters remain incomplete. The comparison ledger is in-memory and uses supplied review-result skill projections rather than claiming a second independent skill-update engine. The build-complete gate remains open.
