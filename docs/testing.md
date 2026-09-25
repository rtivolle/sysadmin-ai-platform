# Testing

Backend tests use `pytest`; harness tests use Node's built-in test runner.

## 1. Test tiers

| Tier | Directory | Focus |
|---|---|---|
| Tier 1 — unit | `backend/tests/tier1_unit/` | Runtime/tool/auth/quota/adapter/model-manager/unit invariants and fail-closed behavior |
| Tier 2 — sandbox | `backend/tests/tier2_sandbox/` | Bubblewrap confinement, cgroups limits, workspace/path isolation |
| Tier 3 — concurrency | `backend/tests/tier3_concurrency/` | Approval lifecycle, quotas, race/adversarial concurrency paths |
| Tier 4 — recovery | `backend/tests/tier4_recovery/` | Audit outbox durability, backup/restore, DR drill behavior |
| E2E benchmark | `backend/tests/e2e/test_30_tasks.py` | Synthetic 30-task evaluation pack |
| Host qualification | `backend/tests/qualification/` | Non-pytest host drills (`sandbox_stress.sh`, `restore_drill.py`) |

`backend/tests/test_platform.py` contains live platform checks that depend on running services.

## 2. Golden commands

From repository root:

```bash
make test
make test-live
make benchmark
make harness-test
make harness-verify
make compile
```

Harness verification script:

```bash
node ./packages/harness-integration/scripts/verify-live-gateway.mjs
```

Equivalent direct commands (examples):

```bash
backend/.venv/bin/python3 -m pytest backend/tests/tier1_unit -q
backend/.venv/bin/python3 -m pytest backend/tests/tier2_sandbox -q
backend/.venv/bin/python3 -m pytest backend/tests/tier3_concurrency -q
backend/.venv/bin/python3 -m pytest backend/tests/tier4_recovery -q
backend/.venv/bin/python3 -m pytest backend/tests/e2e/test_30_tasks.py -q
```

## 3. Environment prerequisites and limits

- Tier 2 requires Bubblewrap, `timeout`, and writable delegated cgroups v2.
- Live checks require local services and expected ports available.
- Some checks intentionally skip when prerequisites are not present; skips are signal, not silent success.
- Port `3080` must be free for default agent platform startup (or override with `SYSADMIN_AGENT_PORT`).

## 4. How to interpret results

- Treat this project as **prototype qualification in progress**.
- Use `docs/status/TEST_READY.md` as the canonical source of latest measured outcomes and open qualification gaps.
- Do not claim production readiness from unit/integration success alone.

## 5. Change-based verification guidance

After changing:

- **General backend logic**: `make test`
- **Runtime/quota/approval/adapter**: Tier 1 + Tier 3
- **Sandbox/workspace isolation**: Tier 2
- **Audit/outbox/backup/restore**: Tier 4
- **Harness integration**: `make harness-test` + `make harness-verify` (manual commands: `node --test ./packages/harness-integration/tests/` for package tests, plus `node ./packages/harness-integration/scripts/verify-live-gateway.mjs` for live gateway verification)
- **Compile sanity check**: `make compile`
- **User-visible docs or claims**: update corresponding docs and `status/TEST_READY.md` evidence where applicable
