# NextTime dependencies

## Locked local toolchain

- Python `>=3.12,<3.13`; validated with CPython 3.12.12.
- `uv` (`uv.lock`); Hatchling `1.27.0` builds the source distribution and wheel.
- Runtime dependencies (`services/api` HTTP/WebSocket boundary only; `packages/domain` remains pure stdlib):
  - `fastapi==0.162.0` (transitively `pydantic==2.14.1`, `starlette==1.9.0`)
  - `uvicorn==0.54.0`
  - `websockets==15.0.1` (ASGI WebSocket protocol support for `/v1/rehearsal/ws`)
- Development/audit/recording dependencies:
  - `httpx==0.28.1`
  - `imageio-ffmpeg==0.6.0`
  - `jsonschema[format]==4.25.1`
  - `mypy==1.18.2`
  - `pip-audit==2.9.0`
  - `playwright==1.64.1`
  - `pytest==9.0.3`
  - `pytest-cov==6.3.0`
  - `pyyaml==6.0.3`
  - `ruff==0.13.2`
  - `types-jsonschema==4.26.0.20261006`
  - `types-pyyaml==6.0.12.20260906`
- Frontend workspace (`apps/web/package.json` + `apps/web/pnpm-lock.yaml`, Node.js 22.x + `pnpm@10.12.4`):
  - `react==18.3.1`, `react-dom==18.3.1`
  - `typescript==5.8.3`, `vite==6.3.5`, `@vitejs/plugin-react==4.5.2`

`make bootstrap` uses `uv sync --frozen`. Contract tests (`tests/contract/test_openapi_contract.py`) enforce via AST inspection that `packages/domain/nexttime_domain` imports zero web, HTTP, ORM, or cloud libraries.

## Security audit

```bash
make test-security
# equivalent audit portion: uv run pip-audit --skip-editable
```

`uv run pip-audit --skip-editable` ran directly under SSL-capable CPython 3.12.12 on 2026-10-24 and reported zero known vulnerabilities across all 70 locked packages. `--skip-editable` excludes only the local workspace distribution (`nexttime-domain`), not third-party packages.

Advisory results are point-in-time and do not replace license, secrets, infrastructure, or production privacy/security reviews.

## External dependencies still blocked

Official Bee CLI/MCP binaries and physical Bee/Apple Watch hardware, iOS/Swift client tooling, AWS SDKs (`boto3` for Bedrock/Polly/Transcribe/S3/KMS), and PostgreSQL persistence drivers are not installed or simulated as live providers. They require separate version/access preflight and lockfile updates when integrated.
