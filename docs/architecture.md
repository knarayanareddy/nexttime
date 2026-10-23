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
- `packages/contracts/` and `packages/rubrics/` hold source-controlled copies used by contract/domain tests. The wheel currently packages the Python domain, not a finished service or content bundle.

## Deliberately absent

No Bee adapter, transcript minimization pipeline, iOS app, rehearsal audio, database, transient store, deletion job, API, notification, or cloud adapter exists. The current code validates policies over supplied values; it does not prove deletion or authentic capture.

## Independence and output limits

NextTime has no production dependency on another project. The fixture produces `partial`, requires confirmation, and leaves authentic skill state unchanged. No real transcript, audio, learner identity, or device observation is read.
