# NextTime data inventory

Inventory date: 2026-10-23. No authentic learner or Bee data is present.

| Data | Current location | Classification/provenance | Retention and use |
| --- | --- | --- | --- |
| Fictional minimized attempt | `examples/real-world-attempt.example.json` | Synthetic self-evidence/outcome fixture | Source-controlled schema/privacy tests only |
| Situation rubric | `packages/rubrics/rubric_v1.json` | Draft authored rules; educator review not evidenced | Deterministic fixture evaluation only |
| Skill state | Python domain/review models in tests and walking copy | Synthetic alpha/beta/count values and confirmed-breakdown updates | In-memory only; one update per confirmed skill in the review service; no learner profile stored |
| Reviewed attempt versions | `tests/unit/test_review.py`, walking copy | Synthetic breakdown decisions, criterion corrections, exclusion/revocation states, and outcome versions | Process-local only; rejected/unknown evidence does not update mastery; no durable attempt history or deletion receipt |
| Source/minimized transcripts | `packages/domain/nexttime_domain/transcripts.py`, `tests/unit/test_transcripts.py` | Synthetic learner, other-speaker, and self-memo turns | Process lifetime only; minimized results exclude other/ambiguous turns; deletion deadline is metadata, not a deletion receipt |
| Schema copy | `packages/contracts/` | Non-sensitive attempt contract | Byte-drift and round-trip tests |
| Configuration examples | `.env.example` | Safe placeholders, no Bee/AWS secrets | Real environment files are Git-ignored |

## Not present or collected

No real Bee recording/reference, learner or other-speaker transcript, diarization, audio, calendar event, notification, name, accent label, device observation, or authentic progress record exists. No transient source store, database, analytics, prompt log, or training dataset exists.

Other-speaker transcript is prohibited from the canonical attempt. The recursive scanner is a source-level guard, not proof of production storage scanning or deletion. Before live ingestion, capture-specific consent, self-only fallback, sensitive-situation exclusion, transient deletion within 24 hours, raw rehearsal-audio deletion, object authorization, export/correction, and verified revocation/deletion workers remain required.

Fixtures must remain synthetic. Real transcripts, audio, learner exports, credentials, and sensitive evaluation slices must not be committed.
