# Sysadmin AI Platform — golden commands. Run `make` or `make help`.
SHELL := /bin/bash
PY    := backend/.venv/bin/python3
NODE  := node

.DEFAULT_GOAL := help
.PHONY: help install start stop restart status logs chat test test-unit test-sandbox \
        test-concurrency test-recovery test-live benchmark harness-test harness-verify \
        stress-sandbox compile clean-cache update update-check

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies, binaries and random credentials
	./install.sh

update: ## Update platform modules (fast-forward + deps + restart running services)
	./update.sh

update-check: ## Report whether platform module updates are available (exit 1 if so)
	./update.sh --check

start: ## Start all backend services
	./platform.sh start

stop: ## Stop all backend services
	./platform.sh stop

restart: ## Restart all backend services
	./platform.sh restart

status: ## Show service status
	./platform.sh status

logs: ## Tail service logs (make logs SERVICE=litellm)
	./platform.sh logs $(or $(SERVICE),all)

chat: ## Open the interactive sysadmin CLI
	./sysadmin-chat

test: ## Run the full pytest suite
	$(PY) -m pytest -q

test-unit: ## Tier 1 unit suites
	$(PY) -m pytest backend/tests/tier1_unit -q

test-sandbox: ## Tier 2 Bubblewrap / cgroups suites
	$(PY) -m pytest backend/tests/tier2_sandbox -q

test-concurrency: ## Tier 3 concurrency, quota and approval suites
	$(PY) -m pytest backend/tests/tier3_concurrency -q

test-recovery: ## Tier 4 outbox, backup and DR suites
	$(PY) -m pytest backend/tests/tier4_recovery -q

test-live: ## Full live stack test (starts and stops services)
	./platform.sh test

benchmark: ## 30-task evaluation pack
	$(PY) -m pytest backend/tests/e2e/test_30_tasks.py -q

harness-test: ## Harness package unit tests
	$(NODE) --test 'packages/harness-integration/tests/*.test.mjs'

harness-verify: ## Boot the real dsh profile and verify the wiring
	$(NODE) packages/harness-integration/scripts/verify-harness.mjs

stress-sandbox: ## Kernel-backed sandbox stress qualification (memory/pids/CPU/deadline/network)
	backend/tests/qualification/sandbox_stress.sh

compile: ## Byte-compile backend sources and tests
	$(PY) -m compileall -q backend/services backend/tests && echo "compile OK"

clean-cache: ## Remove __pycache__ and .pytest_cache
	find backend -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache
