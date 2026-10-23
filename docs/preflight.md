# NextTime preflight

Recorded 2026-10-23 for the local vertical slices. Project code, tests, builds, fixtures, and audits target Python 3.12; Python 3.11 is not a substitute project runtime.

## Available and exercised

| Capability | Recorded version/state |
| --- | --- |
| Project interpreter | Source-built CPython 3.12.12 with zlib 1.3.1, SQLite 3.45.3, and OpenSSL 3.0.19 |
| Package manager | uv 0.9.5; `uv.lock` is frozen for normal bootstrap |
| Quality tools | Ruff 0.13.2; mypy 1.18.2; pytest 9.0.3; pytest-cov 6.3.0 |
| Runtime dependencies | None for the current domain package |
| Audit runner | The same CPython 3.12.12 environment; `pip-audit --skip-editable` completed over TLS |

The restored source-built 3.12 interpreter includes the native modules needed by coverage, package builds, and TLS audits. `make test-security` completed in the project environment without an external audit interpreter. Normal development should likewise use a complete Python 3.12 installation with zlib, SQLite, and SSL/TLS.

## Blocked or not implemented

- Actual Bee/Apple Watch hardware, Developer Mode, and documented CLI/MCP ingestion.
- macOS/Xcode/SwiftUI iOS application, notifications, and on-device Speech validation.
- AWS credentials and deployed Bee bridge, Polly, Transcribe, Bedrock, storage, and database.
- Transcript lifecycle/deletion workers, authentic participant data, and accent/device evaluation.
- User research, learning outcomes, deployment, and release evidence.

## First local checks

```bash
python3.12 --version
uv --version
make bootstrap
make lint typecheck test-unit test-contract test-security build
make dev
```

Do not bypass the audit because a local interpreter lacks SSL. Use a complete 3.12 runtime or an explicitly recorded SSL-capable audit runner against the locked environment.
