# NextTime foundation architecture

## Implemented boundaries

```text
consent/retention inputs ──► processing gate (in-memory policy only)
                                     │
fictional minimized attempt ──► schema + semantic/privacy validation
                                     │
                                     ▼
                     deterministic situation rubric
                                     │
                          outcome + uncertainty reasons
                                     │
                                     ▼
                  confirmed-authentic-attempt skill update gate
```

- `consent.py` rejects absent/revoked consent or absent retention policy before domain processing. Capture/ingestion workers are not implemented.
- `minimization.py` validates retained-data flags, confirmation/minimization coupling, revocation state, and transient deletion bounds; a recursive scanner rejects prohibited other-speaker transcript fields.
- `rubric.py` evaluates only minimized attempts. Low ASR confidence, disputed extraction, ambiguous diarization, missing evidence, and open interactions remain uncertain rather than becoming success.
- `skills.py` changes state only for confirmed authentic evidence; unknown and rejected breakdowns leave state unchanged. Rehearsal completion has no update path.
- `transcripts.py` and `breakdowns.py` minimize synthetic source turns to learner-only excerpts, reject sensitive-situation lexical markers and prohibited emotion/identity/accent inferences, propose at most three self-evidenced breakdowns bounded by ASR and model confidence, and assemble canonical `awaiting_learner_review` attempt drafts.
- `packages/model-gateway/` provides the bounded `ModelGateway` port, single schema-repair retry, token/cost/timeout/circuit-breaker guards, `UnconfiguredBedrockProvider` fail-closed default, and `GatewayBreakdownExtractor` falling back deterministically to `DeterministicBreakdownExtractor`.
- `packages/contracts/` and `packages/rubrics/` hold source-controlled copies used by contract/domain tests, including the canonical attempt schema and the versioned `breakdown_propose.v1` gateway input/output JSON Schemas.

## Deliberately absent

No Bee adapter, transcript minimization pipeline, iOS app, rehearsal audio, database, transient store, deletion job, API, notification, or cloud adapter exists. The current code validates policies over supplied values; it does not prove deletion or authentic capture.

## Independence and output limits

NextTime has no production dependency on another project. The fixture produces `partial`, requires confirmation, and leaves authentic skill state unchanged. No real transcript, audio, learner identity, or device observation is read.
