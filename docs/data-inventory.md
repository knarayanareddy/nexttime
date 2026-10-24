# NextTime data inventory

Inventory date: 2026-10-24. No authentic learner or live Bee hardware data is present.

| Data | Current location | Classification/provenance | Retention and use |
| --- | --- | --- | --- |
| Fictional minimized attempt | `examples/real-world-attempt.example.json` | Synthetic self-evidence/outcome fixture | Source-controlled schema/privacy tests only |
| Situation rubric | `packages/rubrics/rubric_v1.json` | Draft authored rules; educator review not evidenced | Deterministic evaluation in domain/API/tests |
| API & gateway contracts | `packages/contracts/openapi.v1.json`, `packages/contracts/*.schema.json` | Public OpenAPI 3.1 and JSON Schema contracts | Drift-checked contract validation |
| Transient source records | `InMemoryArtifactStore.transient_sources` (process memory only) | Synthetic/fixture Bee segments during ingestion | Purged immediately after minimized draft persistence or failure; overdue >24h sweeps emit `OverdueTransientAlarm` (`NFR-NT-004`, `SEC-NT-005`) |
| Rehearsal turn artifacts | `InMemoryArtifactStore.rehearsal_raw_audio` / `rehearsal_turn_transcripts` | Synthetic push-to-talk PCM bytes and learner turn transcripts | Raw rehearsal audio deleted immediately upon turn receipt; unsaved transcripts expire after 24h (`SEC-NT-004`) |
| Minimized attempts, recurrences, comparisons, skills, tombstones | `InMemoryArtifactStore` & `ApiRuntimeState` (process memory only) | Synthetic learner-only excerpts, Beta skill posteriors, and deletion tombstones | Process lifetime only; purged across 4 verified stages on attempt deletion, consent revocation, or account deletion (`FR-NT-014`) |
| Configuration examples | `.env.example` | Safe placeholders, no Bee/AWS secrets | Real environment files are Git-ignored |

## Not present or collected

No real Bee/Apple Watch recording, live speaker diarization audio, real bystander speech, calendar event title, push notification token, accent label, or authentic participant record exists in this repository. Durable PostgreSQL tables, encrypted S3/KMS buckets, and production database privacy scans remain required before live deployment.

Fixtures must remain synthetic. Real transcripts, audio, learner exports, credentials, and sensitive evaluation slices must not be committed.
