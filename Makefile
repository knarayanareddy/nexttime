SHELL := /bin/bash
UV ?= uv
PIP_AUDIT ?= $(UV) run pip-audit
PIP_AUDIT_ARGS ?=

.PHONY: bootstrap setup dev lint typecheck test-unit test-contract test-integration test-e2e test-infra test-security build deploy evidence

bootstrap:
	$(UV) --version
	$(UV) sync --frozen

setup: bootstrap
	@test -f .env.local || cp .env.example .env.local
	@echo "Local fixture configuration ready. No Bee, AWS, database, or iOS service was started."

dev:
	$(UV) run python -m nexttime_domain.walking_skeleton

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

typecheck:
	$(UV) run mypy packages/domain packages/model-gateway

test-unit:
	$(UV) run pytest tests/unit --cov=nexttime_domain --cov=nexttime_model_gateway --cov-branch --cov-report=term-missing --cov-fail-under=90

test-contract:
	$(UV) run pytest tests/contract

test-security:
	$(UV) run pytest tests/security
	$(PIP_AUDIT) $(PIP_AUDIT_ARGS) --skip-editable

build:
	$(UV) build

test-integration:
	@echo "BLOCKED: database, transient storage, and real Bee bridge integration are not implemented." >&2
	@exit 1

test-e2e:
	@echo "BLOCKED: iOS and actual Bee/Apple Watch E2E paths are not implemented." >&2
	@exit 1

test-infra:
	@echo "BLOCKED: CDK application and infrastructure tests are not implemented." >&2
	@exit 1

deploy:
	@echo "BLOCKED: deployment is unavailable until infrastructure and live preflight exist." >&2
	@exit 1

evidence:
	@echo "Foundation only: no Bee, device, learning-outcome, or release evidence. See docs/validation.md."
