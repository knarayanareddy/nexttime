# NextTime dependencies

## Locked local toolchain

- Python `>=3.12,<3.13`; validated with CPython 3.12.12.
- `uv` (`uv.lock`); Hatchling `1.27.0` builds the source distribution and wheel.
- Runtime dependencies (`services/api` HTTP boundary only; `packages/domain` remains pure stdlib):
  - `fastapi==0.162.0` (transitively `pydantic==2.14.1`, `starlette==1.9.0`)
  - `uvicorn==0.54.0`
- Development/audit dependencies:
  - `httpx==0.28.1`
  - `jsonschema[format]==4.25.1`
  - `mypy==1.18.2`
  - `pip-audit==2.9.0`
  - `pytest==9.0.3`
  - `pytest-cov==6.3.0`
  - `ruff==0.13.2`

`make bootstrap` uses `uv sync --frozen`. Contract tests (`tests/contract/test_openapi_contract.py`) enforce via AST inspection that `packages/domain/nexttime_domain` imports zero web, HTTP, ORM, or cloud libraries.

## Security audit

```bash
make test-security
# equivalent audit portion: uv run pip-audit --skip-editable
```

`uv run pip-audit --skip-editable` ran directly under SSL-capable CPython 3.12.12 on 2026-10-24 and reported zero known vulnerabilities across all 69 locked packages. `--skip-editable` excludes only the local workspace distribution (`nexttime-domain`), not third-party packages.

Advisory results are point-in-time and do not replace license, secrets, infrastructure, or production privacy/security reviews.

## External dependencies still blocked

Official Bee CLI/MCP binaries and physical Bee/Apple Watch hardware, iOS/Swift client tooling, AWS SDKs (`boto3` for Bedrock/Polly/Transcribe/S3/KMS), and PostgreSQL persistence drivers are not installed or simulated as live providers. They require separate version/access preflight and lockfile updates when integrated.
