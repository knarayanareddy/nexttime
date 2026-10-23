# NextTime — Specification Traceability

**Status:** local minimization, learner-review, deterministic rehearsal, and linked-progress slices validated; most requirements remain incomplete
**Coverage:** 55 unique requirement and acceptance IDs from [`SPEC.md`](./SPEC.md)
**Evidence warning:** linked evidence is local source/fixture evidence only; no live integration, device, user-study, or release evidence is implied.

This is the working register required by the implementation specification. Requirement summaries aid navigation only; `SPEC.md` remains normative.

## Status rules

- `not_started`: no implementation exists.
- `in_progress`: active implementation exists but is not yet verified.
- `implemented`: code exists; required tests or release evidence remain incomplete.
- `verified`: all required tests and evidence for this row are linked and current.
- `blocked`: an explicit external dependency or decision prevents completion; identify it in `docs/` and evidence.
- `deviation_pending`: a requested change is awaiting explicit approval in [`SPEC_DEVIATIONS.md`](./SPEC_DEVIATIONS.md); a MUST requirement remains incomplete.

Use paths relative to this project root for implementation, tests, and evidence. Do not enter a path until it exists. Do not use fixture/simulated evidence to satisfy a live-integration gate.

## Register

| ID | Requirement or scenario summary | Status | Implementation paths | Test paths | Demo/device evidence |
| --- | --- | --- | --- | --- | --- |
| `FR-NT-001` | Ingest actual Bee/Apple Watch Bee recording references and transcripts through documented CLI, MCP, or Agent Skill connectivity. | `not_started` | — | — | — |
| `FR-NT-002` | Require capture mode, consent attestation, situation, and retention policy before processing. | `implemented` | `packages/domain/nexttime_domain/transcripts.py` | `tests/unit/test_transcripts.py` | — |
| `FR-NT-003` | Separate learner turns using Bee labels, diarization, learner labeling, or self-only mode; ambiguous turns require review. | `in_progress` | `packages/domain/nexttime_domain/transcripts.py` | `tests/unit/test_transcripts.py` | — |
| `FR-NT-004` | Minimize transient transcripts and retain no long-term other-speaker transcript in the canonical attempt. | `implemented` | `packages/domain/nexttime_domain/minimization.py`<br>`packages/domain/nexttime_domain/transcripts.py` | `tests/unit/test_minimization.py`<br>`tests/unit/test_transcripts.py` | — |
| `FR-NT-005` | Propose at most three functional breakdowns with self-evidence excerpts and ASR/model confidence. | `not_started` | — | — | — |
| `FR-NT-006` | Require learner confirmation/correction before a low-confidence or consequential breakdown updates the learner model. | `implemented` | `packages/domain/nexttime_domain/review.py`<br>`packages/domain/nexttime_domain/skills.py` | `tests/unit/test_review.py`<br>`tests/unit/test_consent_and_skills.py` | — |
| `FR-NT-007` | Evaluate the authentic attempt with a deterministic situation rubric; ambiguous outcomes remain unknown. | `verified` | `packages/domain/nexttime_domain/rubric.py` | `tests/unit/test_rubric.py`<br>`docs/validation.md` | — |
| `FR-NT-008` | Let the learner mark the next occurrence or explicitly link a calendar event and rehearsal time. | `implemented` | `packages/domain/nexttime_domain/rehearsal.py` | `tests/unit/test_rehearsal.py`<br>`docs/validation.md` | — |
| `FR-NT-009` | Generate a schema-valid 90-second rehearsal targeting one confirmed functional breakdown. | `implemented` | `packages/domain/nexttime_domain/rehearsal.py` | `tests/unit/test_rehearsal.py`<br>`docs/validation.md` | — |
| `FR-NT-010` | Deliver local notification and an iOS audio role-play with push-to-talk learner turns. | `not_started` | — | — | — |
| `FR-NT-011` | Map rehearsal responses to expected communicative functions and offer bounded support; do not update real-world mastery from rehearsal alone. | `implemented` | `packages/domain/nexttime_domain/rehearsal.py` | `tests/unit/test_rehearsal.py`<br>`docs/validation.md` | — |
| `FR-NT-012` | Link the next authentic attempt to the same recurrence/rubric and compare outcomes. | `implemented` | `packages/domain/nexttime_domain/progress.py` | `tests/unit/test_progress.py`<br>`docs/validation.md` | — |
| `FR-NT-013` | Show functional progress, evidence, uncertainty, and rehearsal history without gamified streak pressure. | `implemented` | `packages/domain/nexttime_domain/progress.py` | `tests/unit/test_progress.py`<br>`docs/validation.md` | — |
| `FR-NT-014` | Support correction, exclusion, consent revocation, export, and deletion. | `in_progress` | `packages/domain/nexttime_domain/review.py` | `tests/unit/test_review.py` | — |
| `FR-NT-015` | Work without Bedrock by using educator-reviewed deterministic rehearsal templates. | `implemented` | `packages/domain/nexttime_domain/rehearsal.py` | `tests/unit/test_rehearsal.py`<br>`docs/validation.md` | — |
| `INT-NT-001` | Bee iOS app and Developer Mode prerequisites MUST be documented and exercised; actual Bee data MUST appear in the main demo. | `not_started` | — | — | — |
| `INT-NT-002` | `BeeAdapter` MUST support at least one real documented path: official CLI or MCP. A transcript-upload-only path does not satisfy the track. | `not_started` | — | — | — |
| `INT-NT-003` | The rehearsal client is iOS 17+ SwiftUI; Bee/Apple Watch performs authentic capture, while iPhone performs rehearsal playback/interaction. | `not_started` | — | — | — |
| `INT-NT-004` | Polly supplies rehearsal counterpart audio; learner speech uses on-device Speech where Dutch support is verified, otherwise explicitly consented Transcribe Streaming. | `not_started` | — | — | — |
| `INT-NT-005` | Bedrock performs bounded breakdown/scenario proposals through the shared model gateway; deterministic templates remain authoritative fallbacks. | `not_started` | — | — | — |
| `INT-NT-006` | Calendar access is optional and incremental; the core flow works with learner-marked recurrence and local notifications. | `not_started` | — | — | — |
| `NFR-NT-001` | A new Bee transcript begins processing within 60 seconds after bridge ingestion under normal connectivity. | `not_started` | — | — | — |
| `NFR-NT-002` | Rehearsal audio starts within 1.5 seconds after cached download or 4 seconds on first generation; push-to-talk response begins within 2.5 seconds p95. | `not_started` | — | — | — |
| `NFR-NT-003` | No long-term record contains other-speaker raw transcript; automated schema and database scans enforce this. | `in_progress` | `packages/domain/nexttime_domain/minimization.py`<br>`packages/domain/nexttime_domain/transcripts.py` | `tests/security`<br>`tests/unit/test_transcripts.py` | — |
| `NFR-NT-004` | Transient source transcripts delete within 24 hours of successful minimization or immediately on failure/revocation. | `in_progress` | `packages/domain/nexttime_domain/transcripts.py` | `tests/unit/test_transcripts.py` | — |
| `NFR-NT-005` | Duplicate Bee recording references create one attempt; rehearsal notifications and outcome updates are idempotent. | `in_progress` | `packages/domain/nexttime_domain/transcripts.py`<br>`packages/domain/nexttime_domain/review.py`<br>`packages/domain/nexttime_domain/rehearsal.py`<br>`packages/domain/nexttime_domain/progress.py` | `tests/unit/test_transcripts.py`<br>`tests/unit/test_review.py`<br>`tests/unit/test_rehearsal.py`<br>`tests/unit/test_progress.py` | — |
| `NFR-NT-006` | Real-world outcome assertions below confidence 0.75 or without required evidence remain `unknown` pending learner review. | `implemented` | `packages/domain/nexttime_domain/rubric.py`<br>`packages/domain/nexttime_domain/review.py` | `tests/unit/test_rubric.py`<br>`tests/unit/test_review.py` | — |
| `SEC-NT-001` | Consent is capture-mode-specific and revocable; self memo is the required alternative when participant consent is unavailable. | `in_progress` | `packages/domain/nexttime_domain/transcripts.py` | `tests/unit/test_transcripts.py` | — |
| `SEC-NT-002` | Other-speaker transcript exists only transiently in consented mode and is never included in the canonical attempt, analytics, prompts for unrelated tasks, or training. | `in_progress` | `packages/domain/nexttime_domain/minimization.py`<br>`packages/domain/nexttime_domain/transcripts.py` | `tests/security`<br>`tests/unit/test_transcripts.py` | — |
| `SEC-NT-003` | Bee credentials remain in the trusted bridge; iOS receives only learner-scoped API tokens. | `not_started` | — | — | — |
| `SEC-NT-004` | Raw rehearsal audio deletes immediately after transcription; rehearsal transcripts delete after 24 hours unless the learner explicitly saves a self excerpt. | `not_started` | — | — | — |
| `SEC-NT-005` | Transient source deletion is alarmed and verified; revoked consent prevents further bridge acknowledgement/ingestion. | `not_started` | — | — | — |
| `SEC-NT-006` | No sensitive situation ingestion; classifiers and user controls exclude medical/legal/immigration/disciplinary/financial/crisis content. | `not_started` | — | — | — |
| `SEC-NT-007` | No inference of emotion, intelligence, employment status, ethnicity, health, or native identity. | `not_started` | — | — | — |
| `SEC-NT-008` | Evaluation reports ASR errors by accent and device; low-confidence recognition never becomes a negative real-world outcome without review. | `not_started` | — | — | — |
| `AC-NT-001` | Given no consent attestation, when a transcript arrives, then processing is rejected and transient data is deleted. | `in_progress` | `packages/domain/nexttime_domain/consent.py`<br>`packages/domain/nexttime_domain/transcripts.py` | `tests/unit/test_consent_and_skills.py`<br>`tests/unit/test_transcripts.py` | — |
| `AC-NT-002` | Given self-memo mode, when other-speaker-like turns appear, then they are excluded or sent to review, never persisted as conversation evidence. | `implemented` | `packages/domain/nexttime_domain/minimization.py`<br>`packages/domain/nexttime_domain/transcripts.py` | `tests/unit/test_minimization.py`<br>`tests/unit/test_transcripts.py`<br>`docs/validation.md` | — |
| `AC-NT-003` | Given duplicate Bee references, when ingested concurrently, then one attempt results. | `in_progress` | `packages/domain/nexttime_domain/transcripts.py` | `tests/unit/test_transcripts.py` | — |
| `AC-NT-004` | Given ambiguous diarization, when extraction completes, then review is required and no skill update occurs. | `verified` | `packages/domain/nexttime_domain/rubric.py`<br>`packages/domain/nexttime_domain/skills.py` | `tests/unit`<br>`docs/validation.md` | — |
| `AC-NT-005` | Given a minimized attempt, when serialized/scanned, then it passes schema and contains no other-speaker transcript collection. | `verified` | `packages/domain/nexttime_domain/minimization.py` | `tests/contract`<br>`tests/unit/test_minimization.py`<br>`docs/validation.md` | — |
| `AC-NT-006` | Given a rejected breakdown, when review completes, then learner state and rehearsal targeting do not use it. | `in_progress` | `packages/domain/nexttime_domain/review.py`<br>`packages/domain/nexttime_domain/rehearsal.py` | `tests/unit/test_review.py`<br>`tests/unit/test_rehearsal.py` | — |
| `AC-NT-007` | Given ASR confidence below 0.75, when outcome is proposed, then status remains unknown pending learner review. | `verified` | `packages/domain/nexttime_domain/rubric.py` | `tests/unit/test_rubric.py`<br>`docs/validation.md` | — |
| `AC-NT-008` | Given goal stated but no appointment confirmation, when rubric runs, then outcome is partial/failed according to evidence, not success. | `verified` | `packages/domain/nexttime_domain/rubric.py` | `tests/unit/test_rubric.py`<br>`docs/validation.md` | — |
| `AC-NT-009` | Given sufficient confirmation evidence, when rubric runs, then deterministic success and criterion explanation result. | `verified` | `packages/domain/nexttime_domain/rubric.py` | `tests/unit/test_rubric.py`<br>`docs/validation.md` | — |
| `AC-NT-010` | Given a learner-marked recurrence, when rehearsal is generated, then it targets only the confirmed breakdown and lasts at most 120 seconds. | `implemented` | `packages/domain/nexttime_domain/rehearsal.py` | `tests/unit/test_rehearsal.py`<br>`docs/validation.md` | — |
| `AC-NT-011` | Given Bedrock unavailable, when rehearsal is requested, then the reviewed template delivers the same target function. | `implemented` | `packages/domain/nexttime_domain/rehearsal.py` | `tests/unit/test_rehearsal.py`<br>`docs/validation.md` | — |
| `AC-NT-012` | Given quiet hours, when default time is computed, then learner must choose notification behavior rather than being interrupted automatically. | `implemented` | `packages/domain/nexttime_domain/rehearsal.py` | `tests/unit/test_rehearsal.py`<br>`docs/validation.md` | — |
| `AC-NT-013` | Given duplicate turn/notification events, when processed, then no duplicate rehearsal turn or notification persists. | `implemented` | `packages/domain/nexttime_domain/rehearsal.py` | `tests/unit/test_rehearsal.py`<br>`docs/validation.md` | — |
| `AC-NT-014` | Given ambiguous rehearsal speech, when mapped, then retry/support is offered and no negative authentic outcome is recorded. | `implemented` | `packages/domain/nexttime_domain/rehearsal.py` | `tests/unit/test_rehearsal.py`<br>`docs/validation.md` | — |
| `AC-NT-015` | Given completed rehearsal only, when progress loads, then authentic mastery remains unchanged. | `implemented` | `packages/domain/nexttime_domain/rehearsal.py`<br>`packages/domain/nexttime_domain/progress.py` | `tests/unit/test_rehearsal.py`<br>`tests/unit/test_progress.py`<br>`docs/validation.md` | — |
| `AC-NT-016` | Given a linked second authentic attempt, when confirmed, then criterion deltas and skill updates occur once. | `implemented` | `packages/domain/nexttime_domain/progress.py`<br>`packages/domain/nexttime_domain/review.py` | `tests/unit/test_progress.py`<br>`tests/unit/test_review.py`<br>`docs/validation.md` | — |
| `AC-NT-017` | Given revoked consent, when the next bridge sync/process occurs, then ingestion stops and deletion starts. | `in_progress` | `packages/domain/nexttime_domain/transcripts.py` | `tests/unit/test_transcripts.py` | — |
| `AC-NT-018` | Given account deletion, when jobs finish, then attempts, transcripts, audio, caches, prompts, and derived states under service control are deleted. | `not_started` | — | — | — |
| `AC-NT-019` | Given another learner's IDs, when API access is attempted, then all object access is denied. | `not_started` | — | — | — |
| `AC-NT-020` | Given actual Bee hardware/Apple Watch Bee, when the demo runs, then real recorded/processed data flows through documented CLI/MCP into two authentic attempt evaluations. | `not_started` | — | — | — |
