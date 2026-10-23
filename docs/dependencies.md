# NextTime dependencies

## Locked local toolchain

- Python `>=3.12,<3.13`; validated with CPython 3.12.12.
- uv 0.9.5 and `uv.lock`; Hatchling 1.27.0 builds the source distribution/wheel.
- Ruff 0.13.2, mypy 1.18.2, pytest 9.0.3, pytest-cov 6.3.0.
- jsonschema 4.25.1 with format support and pip-audit 2.9.0.
- No third-party production runtime dependency for the current domain package.

`make bootstrap` uses `uv sync --frozen`. Build-system and development dependencies are resolved in the lockfile; sibling projects have separate environments and locks.

## Security audit

The first SSL-capable audit found `PYSEC-2026-1845` in pytest 8.4.2. Both Python foundations updated to pytest 9.0.3, regenerated their locks, and reran all tests. The final NextTime audit reported no known vulnerabilities on 2026-10-23.

Normal command:

```bash
make test-security
# equivalent audit portion: uv run pip-audit --skip-editable
```

Because the recorded source-built 3.12 interpreter lacks SSL, the final audit used pip-audit 2.9.0 under SSL-capable system Python 3.11.2 with `--path` targeting NextTime's Python 3.12 `site-packages`. `--skip-editable` excludes only the local `nexttime-domain` source distribution, not third-party packages. Project tests still ran under 3.12.12.

Advisory results are point-in-time and do not replace license, secrets, infrastructure, or production privacy/security reviews.

## External dependencies still blocked

Bee CLI/MCP and hardware, iOS/Swift, AWS services, persistence, notification, and audio/speech SDKs are not installed or simulated. They require separate version/access preflight and lockfile changes when implemented.
