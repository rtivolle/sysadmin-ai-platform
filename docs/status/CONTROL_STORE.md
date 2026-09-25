# Durable control store (PostgreSQL) — verification record

Date: 2026-09-25 (UTC). Scope: [ADR-0014](../decisions/ADR-0014-postgresql-control-store.md),
the optional PostgreSQL leg selected by `SYSADMIN_CONTROL_STORE=postgres`.

This file is the evidence record for that work. It lives here rather than in
[TEST_READY.md](TEST_READY.md) because another lane (PR-H1 multi-host roles) held
uncommitted changes in `TEST_READY.md` and
[`plans/PRODUCTION_READINESS.md`](../plans/PRODUCTION_READINESS.md) while this
work was done; AGENTS.md §8 forbids editing a file another lane is writing. The
snippet to fold into `TEST_READY.md` and the row to add to the roadmap's
tracking table are at the end of this file.

## What was implemented

| Area | Deliverable |
|---|---|
| Cluster lifecycle | `backend/config/postgres/postgres.sh`: `check`, `provision` (initdb with `--pwfile`, role, database, password file `0600`), `start`, `stop`, `restart`, `status`, `health`, `dsn` (redacted), `psql`, `backup`, `restore`, `destroy`. Cluster in `backend/data/postgres`, socket in `backend/run/postgres`, log in `backend/logs/postgres.log`, port 5433, loopback only. Never installs a package: missing binaries exit **3** with the exact install command. |
| Store library | `backend/services/control_store/`: `dsn.py`, `connection.py`, `executor.py`, `schema.py`, `key_store.py`, `ledger.py`, `cli.py`. Stdlib-only at import time; the PostgreSQL driver is imported lazily, so the suite runs on a host with no driver. |
| Identity | `auth_gateway/server.py::resolve_identity` used by ForwardAuth and LiteLLM custom auth; durable store is authoritative when selected, master credential always resolves to the admin identity, outage → `ConnectionError` → 503. |
| Key lifecycle | Atomic rotate (replacement inserted, other live keys revoked, one transaction), audited revoke, key returned exactly once over HTTP with `Cache-Control: no-store`; only `sha256(token)` is stored. |
| Durable ledger | Reservation/settlement mirror, direct-usage rows, conservative charge for unreported reservations, `settle_expired`, and a once-per-user/day atomic reconciliation floor on the Valkey counter. |
| Fail-closed behaviour | Selected-but-unusable store ⇒ `_UnavailableLedger` in the quota manager and a 503 from the auth gateway; a non-`ConnectionError` store fault is normalized to `ConnectionError`. |

## Measured (development host, 2026-09-25)

Command (a temporary venv is used — see "Environment caveat"):

```bash
PYTHONPATH=backend /tmp/dbimpl-venv/bin/python -m pytest \
  backend/tests/tier1_unit backend/tests/tier3_concurrency backend/tests/tier4_recovery \
  -q -p no:cacheprovider
```

| Run | Result |
|---|---|
| Before the change (same host, same session, same venv) | **534 passed, 40 failed, 18 skipped** (75 s class) |
| After the change | **600 passed, 40 failed, 18 skipped** (75.15 s) |
| Difference | **+66 passing, 0 regressions, 0 previously-failing tests fixed** — the 40 `FAILED` lines are byte-identical before and after (`comm` on the sorted failure lists) |

New tests (all passing):

| File | Tests | Covers |
|---|---|---|
| `tier1_unit/test_control_store_dsn.py` | 11 | mode selection, DSN composition, non-PostgreSQL scheme rejection, missing/blank password fails closed, redaction, health reporting |
| `tier1_unit/test_control_store_keys.py` | 12 | hash-only storage, resolve/revoke, rotation atomicity and exclusion of the new key, duplicate refusal, idempotent file import (skips `master`/`valkey-password`/`postgres-password`), audit events contain no token, outage ⇒ `ConnectionError` |
| `tier1_unit/test_control_store_ledger.py` | 10 | reservation idempotency, estimate charged until settled, exactly-once settlement, unknown-reservation completion, direct usage, per-user/day scoping, expired-reservation charge, prune, date handling |
| `tier1_unit/test_durable_identity.py` | 8 | file mode unchanged, store authoritative (unimported file key rejected), no fallback on outage, 503 through `/verify`, admin endpoints (409 in file mode, rotate/revoke/404/403, `no-store`) |
| `tier1_unit/test_quota_durable_ledger.py` | 9 | hooks are no-ops in file mode, reservation/settlement/direct mirroring, reconciliation floor blocks a fresh budget, once-per-day, fail-closed for both `ConnectionError` and other faults, `_UnavailableLedger` |
| `tier1_unit/test_postgres_control_store_script.py` | 16 | `check` exit 3 + guidance when binaries are missing, provision (initdb `--pwfile`, database creation, password `0600`, launch options), idempotent provision, start without cluster, health, status running/stopped, stop no-op, backup refuses a stopped cluster, dump `0600`, destroy requires `--yes`, usage errors, restore argument validation |

The rotation test found a real defect in the first implementation (rotation
revoked the key it had just issued); the SQL now excludes the replacement key,
which is why that assertion exists.

## Not measured (no claim made)

- **A live PostgreSQL cluster.** The server is not installed on this macOS host
  and no package was installed (the user-facing decision recorded for this work:
  no system package installation). Consequently, not measured: `provision` against
  a real `initdb`, the DDL against a real server, key issue/rotate/revoke round
  trips, `pg_dump`/`pg_restore`, and the reconciliation path against a live
  Valkey.
- **LiteLLM with `general_settings.database_url` / `DATABASE_URL`.** Its own
  tables (spend logs, virtual keys) have not been created or read in a run.
- **`platform.sh` lifecycle wiring.** Deliberately not applied: `platform.sh`,
  `install.sh`, `installer_tui.py` and `backend/config/roles/*` carried another
  lane's uncommitted PR-H1 work throughout this session (confirmed active at
  21:21 local by fresh modifications), which AGENTS.md §8 forbids editing
  concurrently. The exact additive diff is in
  [configuration.md §7](../configuration.md#7-durable-control-store-postgresql).
- **The full Linux suite figure** quoted elsewhere in `TEST_READY.md` (729 passed)
  — not reproducible on this host, see the caveat below. The comparison table
  above is baseline-vs-after on the *same* host in the *same* session.

## Environment caveat (why the numbers differ from the Linux baseline)

`backend/.venv/bin/python3` on this host is a **broken symlink to a Linux
interpreter** (`python3.14`, `x86_64-linux-gnu` site-packages), and the system
Python has no pytest, so `make test` cannot run here as-is. The runs above used a
throwaway venv (`/tmp/dbimpl-venv`, Python 3.14.7, pytest 9.1.1) with the subset
of dependencies needed by tier 1/3/4. That subset is the reason 40 tests fail on
this host; they are the Linux-specific target-adapter/config-deployer tests
(`sudo`, POSIX ownership, `/home/...` paths) and the in-flight multi-host tests,
and their failure set is identical with and without this change.

## To fold into TEST_READY.md

```markdown
## Durable control store — PostgreSQL (2026-09-25)

- **What changed.** Optional PostgreSQL leg ([ADR-0014](../decisions/ADR-0014-postgresql-control-store.md)):
  native cluster lifecycle (`backend/config/postgres/postgres.sh`), durable key
  lifecycle (hashed tokens, atomic rotate/revoke, audited) and a durable daily
  token ledger with a reconciliation floor; Valkey keeps counters/leases. Selected
  by `SYSADMIN_CONTROL_STORE=postgres`; selected-and-unreachable fails closed (503).
  Full record: [CONTROL_STORE.md](CONTROL_STORE.md).
- **Measured (development host, throwaway Python 3.14 venv):** tier1+tier3+tier4
  **600 passed, 40 failed, 18 skipped**; the same command before the change was
  **534 passed, 40 failed, 18 skipped** with an identical failure list (+66 tests,
  no regressions). 66 new tests across six files.
- **Not measured.** Live cluster provisioning/DDL/key round trips/dump-restore,
  LiteLLM with `database_url`, and `platform.sh` lifecycle wiring (deferred:
  another lane held those files; see configuration.md §7).
```

## To fold into plans/PRODUCTION_READINESS.md (§4 tracking)

```markdown
| PR-D5 | Divergence decision records | P2 | — | **in flight** | ADR-0014 (PostgreSQL control store) supplies the mechanism and tests that ADR-0001 lacked; ADR-0001 + index updated. Outstanding: platform lifecycle wiring (blocked on the PR-H1 lane's files) and live-cluster qualification. |
```
