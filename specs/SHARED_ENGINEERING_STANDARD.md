# Shared Engineering Standard

**Version:** 1.0.0  
**Applies to:** AccessHold, CurioLab, NextTime, HarvestGrid, and RoomSmith  
**Normative:** every MUST in this document is release-blocking unless the selected project specification makes it stricter

## 1. Purpose and architectural principles

This standard defines the common engineering contract for all five implementations. Project-specific documents define domain behavior; this document prevents five agents from inventing incompatible conventions for security, testing, errors, AI safety, deployments, and completion evidence.

The implementations MUST follow these principles:

1. **Vertical slices over layers.** Each implementation phase must produce a testable user outcome, not an orphaned frontend or model notebook.
2. **Ports and adapters.** Sponsor devices and external services sit behind typed interfaces. Production and test adapters implement the same contract, and only production adapters are used for live evidence.
3. **Transactional core, probabilistic edge.** Domain state transitions, arithmetic, authorization, scheduling, policy, and game logic are deterministic code. Models produce bounded proposals, classifications, or dialogue.
4. **No silent degradation.** Fallbacks must be observable and labeled. A test fixture must not appear as a live provider result.
5. **Data minimization by default.** Collect the smallest useful field set, retain it for the shortest useful period, and make deletion real.
6. **Accessible equivalent path.** Camera, voice, color, timing, and gesture interactions require an equivalent non-excluding path where specified.
7. **Evidence as a build artifact.** Requirements, tests, device runs, and demo claims remain traceable.

## 2. Canonical technology baseline

A project specification may narrow this baseline. It MUST NOT add a second equivalent framework without an architecture decision record.

| Area | Baseline |
| --- | --- |
| TypeScript runtime | Node.js 22.x, TypeScript 5.x in strict mode, ESM |
| TypeScript package management | pnpm with committed lockfile; Corepack selects the pinned version |
| Python runtime | Python 3.12, `pyproject.toml`, `uv` lockfile, Ruff, mypy strict for domain code |
| Android/Fire OS | JDK 17, Kotlin 2.x, Gradle version catalog, compile SDK 35, minimum SDK 28 unless a project states otherwise |
| iOS | iOS 17+, Swift 5.10+, SwiftUI, Swift concurrency; Xcode project or Swift Package Manager manifest committed |
| Web | React 18+ with Vite and TypeScript unless a project explicitly chooses another framework |
| API | Fastify 5 for TypeScript or FastAPI for Python; OpenAPI 3.1 generated from implementation and checked for drift |
| Infrastructure | AWS CDK v2 in TypeScript, one explicit region, separate stacks by lifecycle |
| Relational data | PostgreSQL 16+; Aurora PostgreSQL in deployed AWS environments, PostgreSQL in local containers |
| Object data | S3 with bucket-owner-enforced ownership, blocked public access, default encryption, lifecycle rules |
| Authentication | Amazon Cognito for human accounts; short-lived scoped credentials for paired devices |
| Tests | Vitest/Jest as appropriate, pytest, JUnit/AndroidX Test, Playwright, and contract tests against real adapters |
| Infrastructure tests | CDK assertions plus `cdk synth`; policy and public-access checks in CI |
| Observability | OpenTelemetry-compatible traces, JSON logs, CloudWatch metrics/alarms, AWS X-Ray where supported |

### Version pinning

- Every direct dependency MUST be pinned by a committed lockfile.
- The implementing agent MUST record runtime, SDK, model, and infrastructure versions in `docs/dependency-manifest.md`.
- Deprecated or vulnerable dependencies MUST be upgraded or explicitly risk-accepted with an expiry date.
- Model identifiers and prompt versions are configuration, never hard-coded throughout business logic.

## 3. Required project structure

Project documents may add modules but MUST preserve these boundaries:

```text
<project-root>/
  apps/                       # user-facing applications
  services/                   # deployable backend workers/APIs
  packages/                   # shared domain, contracts, adapters, UI
  infra/                      # CDK application and tests
  content/                    # versioned domain content, if applicable
  tests/
    contract/
    integration/
    e2e/
    fixtures/
    performance/
  scripts/                    # reproducible development/bootstrap tasks
  docs/
  evidence/
  .env.example                # names and safe examples only
  .gitignore
  LICENSE
  Makefile or justfile        # canonical setup/test/run commands
```

### Canonical commands

Every implementation MUST expose equivalent commands, even if the underlying tool differs:

```text
make bootstrap       # install/check tools without exposing secrets
make setup           # generate local configuration and start dependencies
make dev             # run the complete local development path
make lint            # formatting and static lint checks
make typecheck       # all typed projects
make test-unit
make test-contract
make test-integration
make test-e2e
make test-infra
make test-security
make build
make deploy ENV=test
make evidence        # regenerate non-sensitive evidence manifest
```

CI MUST run lint, typecheck, unit, contract, integration, infrastructure, security, and build jobs. E2E tests requiring private credentials run in a protected environment, never on untrusted fork pull requests.

## 4. Environment and configuration contract

Environments are `local`, `test`, `staging`, and `prod`. `test` means a deployed cloud test environment; unit-test mode is not an environment.

Each environment MUST have separate accounts or strongly isolated resources, encryption keys, data buckets, secrets, and callback URLs. Production credentials MUST NOT be usable from local or test environments.

Configuration MUST be validated at process startup with a typed schema. Invalid configuration fails closed with an actionable error. `.env.example` documents every variable, whether it is required, its safe example, and its owning service.

Required common variables where applicable:

```text
APP_ENV=local
AWS_REGION=us-east-1
LOG_LEVEL=info
PUBLIC_BASE_URL=http://localhost:3000
API_BASE_URL=http://localhost:8080
COGNITO_USER_POOL_ID=
COGNITO_CLIENT_ID=
BEDROCK_MODEL_ID=
PROMPT_SET_VERSION=
DATA_RETENTION_DAYS=
DEMO_MODE=fixture|live
```

`DEMO_MODE` MUST NOT change authorization, validation, or domain outcomes. It selects adapters only. Live and fixture records carry a provenance value visible to operators and in demo evidence.

## 5. Domain, API, and event conventions

### 5.1 Values

- IDs are ULIDs stored as 26-character canonical strings.
- Timestamps are UTC RFC 3339 strings with millisecond precision; database columns use `timestamptz`.
- Money is an integer minor-unit amount plus ISO 4217 currency, never a float.
- Physical measurements include a unit and uncertainty/confidence where relevant.
- Coordinates use WGS84; exact residential coordinates require the stricter project privacy rules.
- Enumerations are lowercase snake case in persisted contracts.
- Unknown is represented explicitly; it MUST NOT be coerced to false, zero, healthy, accessible, ripe, or successful.

### 5.2 REST API

- Base path is `/v1`.
- Resource names are plural and lowercase.
- Mutations accepting retries require an `Idempotency-Key` header. The server stores the key, request hash, status, and response for at least 24 hours.
- A replay with the same key and same request returns the original result; a different request with the same key returns `409 idempotency_key_reused`.
- Optimistic concurrency uses an integer `version` and `If-Match` or an equivalent expected-version field.
- Errors use `application/problem+json`:

```json
{
  "type": "https://errors.example.com/validation",
  "title": "Request validation failed",
  "status": 422,
  "code": "validation_failed",
  "detail": "One or more fields are invalid.",
  "instance": "/v1/example",
  "request_id": "01J...",
  "fields": [{"path": "/field", "code": "required"}]
}
```

- Errors MUST NOT leak secrets, raw provider payloads, sensitive profile data, or stack traces.
- List endpoints use opaque cursor pagination and bounded page sizes.
- OpenAPI is generated and checked in CI. Hand-written documentation cannot drift silently.

### 5.3 Event envelope

All asynchronous domain events use this envelope unless a platform payload requires an adapter:

```json
{
  "specversion": "1.0",
  "id": "01J...",
  "type": "project.entity.action.v1",
  "source": "service-name",
  "subject": "entity-id",
  "time": "2026-10-23T12:00:00.000Z",
  "correlation_id": "01J...",
  "causation_id": "01J...",
  "tenant_id": "01J...",
  "schema_version": 1,
  "data": {}
}
```

Consumers MUST be idempotent and tolerate at-least-once delivery. Event schemas are versioned and backward compatible within a major version. Dead-letter queues have alarms and a documented replay procedure.

## 6. Architecture and data requirements

### 6.1 Backend boundaries

- Domain modules MUST NOT import web controllers, AWS SDK clients, ORM entities, or model prompts directly.
- Adapters translate external payloads into canonical contracts at the boundary and validate them before use.
- Database transactions cover all writes required for one domain transition. Outbox records are written in the same transaction where applicable.
- Background workers MUST support safe retry without repeating external side effects.
- Deletion and retention jobs are application features with tests, not manual console procedures.

### 6.2 Local infrastructure

Docker Compose MUST provide disposable local dependencies that can realistically run locally, such as PostgreSQL, LocalStack or service-specific emulators, Mailpit, and a fixture provider. An emulator is not evidence for a real Amazon integration.

`make setup` MUST seed a small deterministic dataset labeled `fixture`. Tests MUST NOT depend on execution order or the developer's existing database.

### 6.3 AWS infrastructure

CDK stacks SHOULD be separated into foundation, data, compute, workflow, and observability where this improves lifecycle safety. All stacks MUST have environment, project, owner, and data-classification tags.

Production MUST have:

- blocked public S3 access and least-privilege IAM;
- KMS encryption for sensitive data and queues where supported;
- TLS for all external and service ingress;
- Secrets Manager for third-party secrets;
- deletion protection for production relational data and explicit snapshot/restore testing;
- alarms for user-visible failures, queue age, workflow failures, and unusual model cost;
- budget alerts and per-request usage dimensions.

## 7. AI and model gateway

Every model call MUST go through one application-level gateway implementing this interface conceptually:

```text
generate(request):
  input:
    operation_name
    prompt_version
    input_schema_validated_payload
    output_schema
    model_policy
    correlation_id
  output:
    validated_payload | typed_failure
    provider_request_id
    model_id
    prompt_version
    latency_ms
    token_usage
    fallback_used
```

Required behavior:

1. Validate input and output against versioned schemas.
2. Use bounded attempts: initial call, at most one schema-repair retry, then a deterministic or human fallback. A second model may be used only if configured and tested.
3. Never include chain-of-thought in logs or UI. Store concise evidence references and decision rationale instead.
4. Redact secrets and apply project-specific PII rules before invocation.
5. Treat model output as untrusted input. Sanitize rendered text and never execute generated code, SQL, URLs, provider calls, or state transitions directly.
6. Apply timeouts, token limits, cost limits, circuit breakers, and operation-specific caching.
7. Record model ID, prompt version, schema version, latency, token usage, and fallback status without logging prohibited content.
8. Maintain golden fixtures for every prompt/schema pair and run regression tests before deployment.

Exact arithmetic, geometry, legal/policy conclusions, booking approval, food-safety decisions, learner outcome assertions, and game completion MUST NOT be delegated to free-form model output.

## 8. Security and privacy baseline

### 8.1 Application security

- Validate authorization on every object access; authentication alone is insufficient.
- Use deny-by-default roles and tenant scoping. Test cross-user and cross-tenant access.
- Protect cookies with `HttpOnly`, `Secure`, and appropriate `SameSite`; CSRF protection is required for cookie-authenticated mutations.
- Validate request size, content type, and schema. Rate-limit authentication, pairing, model, export, and approval endpoints.
- Use parameterized queries and safe HTML/text rendering. No unsanitized rich HTML from models or providers.
- Verify webhook signatures using the official algorithm, timestamp/replay protection where supported, and raw-body handling.
- Run dependency, container, IaC, and secret scans. Critical or exploitable high findings are release blockers.
- Conduct abuse-case tests for replay, enumeration, injection, oversized media, malicious filenames, and forged provider events.

### 8.2 Privacy

Every implementation MUST publish a data inventory containing field, purpose, lawful/consent basis, source, storage location, retention, sharing, and deletion behavior.

- Raw sensitive media uses the shortest project-specific retention and MUST NOT be used for advertising.
- Model training on user content is off by default and requires a separate explicit opt-in.
- Export and deletion cover primary records, derived features, search indexes, caches, and object storage; backups age out under a documented schedule.
- Logs and analytics contain IDs and coarse metrics rather than raw transcripts, addresses, access needs, child media, or room video.
- Consent records are versioned and revocable. Revocation stops future processing and starts specified deletion.
- Privacy tests prove that other users, volunteers, paired devices, and public endpoints receive only their minimum field set.

## 9. Accessibility and interaction baseline

User-facing applications MUST target WCAG 2.2 AA where applicable and document platform-specific equivalents.

- Complete core flows with keyboard/remote only and with a supported screen reader.
- Do not encode state by color alone; include text/icon/shape.
- Provide visible focus, scalable text, captions for speech/video, and reduced-motion behavior.
- Voice-only and camera-only flows require the alternatives specified by the project.
- No critical action depends solely on fine motor precision, rapid response, hearing, or distinguishing red from green.
- Error messages say what happened and how to recover.
- Automated accessibility scans are supplemented by manual tests; a scan alone is not completion evidence.

## 10. Reliability and performance baseline

Project specifications define stricter numeric budgets. All implementations MUST:

- set explicit upstream connection/request timeouts;
- retry only idempotent or safely keyed operations, with exponential backoff and jitter;
- distinguish transient, permanent, authorization, validation, and unavailable-capacity failures;
- expose health/readiness endpoints without leaking dependencies or secrets;
- persist enough state to resume after process restart;
- handle duplicate and out-of-order events;
- test expired credentials, rate limits, malformed responses, offline behavior, and partial provider outages;
- display stale data with its timestamp rather than presenting it as current.

Production backup restoration, dead-letter replay, token revocation, and regional/service outage procedures MUST be exercised at least in the staging environment.

## 11. Testing standard

### 11.1 Test layers

| Layer | Required coverage |
| --- | --- |
| Unit | Domain transitions, calculations, policy, validators, state machines, retry/approval logic |
| Schema/contract | Canonical JSON Schemas, OpenAPI, external adapter mappings, backward compatibility |
| Integration | Real database/queue/object store behavior, transactions, idempotency, retention/deletion |
| Sponsor adapter | Required Ring/Bee/Alexa+/Fire OS integration against actual device or permitted simulator |
| End to end | Primary user journeys and specified failure/recovery journeys |
| Security/privacy | Authorization matrix, cross-tenant access, replay, secret leakage, retention and export/delete |
| Accessibility | Automated scan plus manual remote/keyboard/screen-reader and fallback paths |
| Performance | Project latency/load budgets, large payloads, slow networks, reconnect storms |
| Evaluation | Project ground-truth datasets, baseline comparisons, subgroup/error analysis |
| Disaster recovery | Restore, replay, revocation, and resume workflows |

### 11.2 Coverage and quality gates

- Domain and policy modules require at least **90% branch coverage**.
- The overall backend requires at least **80% line coverage**; generated code and thin framework declarations may be excluded with justification.
- Coverage is not a substitute for assertions. Every P0 acceptance scenario must have an explicit expected outcome.
- Tests MUST be deterministic; time, randomness, IDs, and network responses are controlled.
- No skipped P0 test, quarantined security test, or uncommented snapshot update is allowed at release.

### 11.3 Acceptance scenario format

Project documents use `AC-<PROJECT>-###`. Implementation MUST map each scenario to a test or repeatable manual script:

```text
Given <initial state and actor>
When <one action or external event>
Then <observable domain result>
And <security, persistence, or side-effect result>
```

## 12. UX and content implementation baseline

- Every screen has loading, empty, error, offline/stale, permission-denied, and success states where relevant.
- Destructive or financial actions show the exact object and consequence before approval.
- Optimistic UI MUST NOT claim an external action completed before confirmation.
- Async actions expose status and a recovery route after refresh or reconnect.
- Content and prompts are versioned, reviewed, and rollback-capable.
- Demo fixtures are deterministic and labeled, but the core demo path uses the required live Amazon integration.
- No placeholder buttons, dead navigation, lorem ipsum, fake metrics, or hard-coded demo outcomes are permitted in the completion build.

## 13. Observability and product analytics

Every service MUST emit request ID, correlation ID, operation, status, duration, and safe resource IDs. Traces MUST cross API, workflow, and model boundaries where supported.

At minimum, alarm on:

- elevated 5xx rate and p95 latency;
- workflow failure and oldest queue age;
- expired/revoked sponsor credentials;
- repeated schema-validation failures;
- fallback rate and model-cost anomaly;
- failed deletion/retention jobs;
- failed device/session pairing where applicable.

Analytics events MUST be specified in the project document, use coarse non-sensitive properties, and have a defined decision they inform. Do not build a metrics dashboard disconnected from the product claim.

## 14. Implementation workflow for an autonomous agent

### Preflight gate

Before application code, the agent MUST:

1. Read the selected specification and canonical schema.
2. Verify toolchains, official platform requirements, and external adapter documentation.
3. Create the traceability and deviation registers.
4. Write an architecture decision record for any unavoidable version/platform incompatibility.
5. Produce a walking-skeleton plan that reaches the real sponsor integration early.

### Per-slice gate

A slice is complete only when:

- its user journey works through real domain code;
- unit, contract, integration, and relevant E2E tests pass;
- authorization, failure states, and observability are included;
- setup and architecture docs are updated;
- traceability records point to actual files and test results.

### Final gate

The agent MUST run a clean-checkout reproduction, build all deployment artifacts, synthesize infrastructure, execute the real-device/simulator demo, and produce the evidence manifest. It MUST NOT describe planned or mocked behavior as implemented.

## 15. Shared completion rubric

Each project specification adds domain-specific items to this 100-point rubric. Unless the project states otherwise, award points only with evidence.

| Category | Points | Full-credit requirement |
| --- | ---: | --- |
| Functional completeness | 25 | All P0 flows and state transitions work end to end; no placeholder core feature |
| Required platform integration | 15 | Real Amazon integration in the main path, with correct lifecycle and demonstrated device/simulator evidence |
| Architecture and reliability | 15 | Typed contracts, deterministic core, durable recovery, idempotency, failure handling, and measured performance |
| Security and privacy | 15 | Authorization matrix, data minimization, consent, retention/deletion, secret safety, and abuse tests all pass |
| UX and accessibility | 10 | Complete states, coherent platform-native UX, fallback paths, and manual accessibility evidence |
| Testing and evaluation | 10 | Required coverage, P0 acceptance scenarios, real-data evaluation, limitations and baseline comparison |
| Documentation and operability | 5 | Clean setup, architecture, runbook, monitoring, feedback, friction log, and traceability are complete |
| Demo and submission evidence | 5 | Under-three-minute legible demo, real integration visible, honest limitations, required submission artifacts |

**Completion threshold:** at least 90/100, no category below 70%, no open critical/high security issue, and every release-blocking gate passed. A score cannot compensate for a missing real integration or fabricated evidence.

## 16. Hackathon judging evidence matrix

The final `docs/validation.md` MUST map evidence to the four equally weighted criteria:

| Criterion | Required evidence |
| --- | --- |
| Tech Implementation | Architecture, real integration traces, failure tests, performance, evaluation results, and meaningful AWS/model use |
| Design | Platform-appropriate flows, accessibility results, empty/error/offline states, and usability observations |
| Potential Impact | Named user, current workaround, measured operational/learning outcome, pilot feedback, adoption/distribution assumptions |
| Quality of Idea | Closest incumbents, precise wedge, sponsor necessity, rejected broader scope, and why the demo is memorable |

Product feedback for every required tool/API/SDK and the development friction log are mandatory submission artifacts even when they do not affect the code-quality score.

## 17. Submission package checklist

Before calling a hackathon submission complete, the implementation agent MUST recheck the current official rules and produce:

- complete source and assets, an appropriate `LICENSE` for the public open-source repository route, and clean setup/run/test instructions;
- the working primary-track integration in runtime code and in the video, using the actual required device or permitted simulator;
- an English public demo video under three minutes that shows real functionality and does not disguise fixture behavior as live;
- an English project description covering features, functionality, architecture, and implementation;
- product feedback for every required tool/API/SDK, including onboarding, positives, shortcomings, and whether it would be used again;
- judge testing access, free availability through judging, credentials/instructions where needed, and valid deployment through the judging period;
- third-party asset, model, dataset, music, trademark, and media-permission records;
- an explanation of significant new work if any pre-hackathon project or content is used;
- the optional structured friction log captured during development;
- only genuinely completed mini-challenge selections and artifacts.

A public license alone does not satisfy the Open Source mini challenge. That option requires the separate new open-source project or qualifying external contribution described by the selected project specification and current rules. Mini challenges are optional and cannot compensate for a missing primary-track integration.
