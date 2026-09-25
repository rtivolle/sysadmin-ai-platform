# ADR-0014: A local PostgreSQL control store for key lifecycle and the durable token ledger

- **Date:** 2026-09-24
- **Status:** Proposed — **implemented at service level, owner acceptance pending**;
  supersedes the "no PostgreSQL" decision of [ADR-0001](ADR-0001-file-based-keys-valkey-no-postgresql.md)
  for key lifecycle and durable control state (file keys remain the provisioning input).
- **Source of divergence:** `docs/specs/02 — Service Quotas & Passerelle API`
  (LiteLLM virtual keys via `POST /key/generate`, `database_url`);
  `docs/plans/DEVELOPMENT_PLAN.md` §3 correction **S4** ("Add local PostgreSQL for
  keys and durable control state; use Valkey for compatible distributed
  counters/leases") and §4 architecture (`LiteLLM ---- PostgreSQL | durable
  ledger`); §4 quota contract ("Record usage durably and reconcile after
  restart… never silently count zero"). Work item **PR-D5** in
  `docs/plans/PRODUCTION_READINESS.md` records ADR-0001 as the only divergence
  with a fully unqualified contract.

## Context

ADR-0001 kept bearer identity in `backend/config/keys/*.key` and put all shared
runtime state in Valkey. Its own "Corrective work" section named what that cost:
no runtime key lifecycle (keys cannot be rotated, revoked or issued through an
API; revocation means editing a file), no atomic or audited key transition, no
restart-durability test, and no durable relational ledger for the daily quota
state the plan had assigned to PostgreSQL. Every other divergence in
`docs/decisions/` is at least partially qualified by tests; PR-D5 could not close
while this one had neither a mechanism nor a test.

The owner selected the PostgreSQL leg for this work (2026-09-24 session) rather
than accepting file-only identity permanently.

## Decision

Run **one private, native PostgreSQL cluster** — no Docker, no system-wide
instance — as the durable control store, and keep the two stores' roles
separate:

| Concern | Store | Why |
|---|---|---|
| Bearer-key lifecycle (issue / rotate / revoke / resolve) | **PostgreSQL** | needs atomic multi-row transitions, durability and an audit trail; "revoke by editing a file" was ADR-0001's known gap |
| Daily token ledger (reservations, settlements, reconciliation) | **PostgreSQL** (durable record) **+ Valkey** (atomic counters) | Valkey is good at atomic admission; PostgreSQL is what survives a restart, a lost AOF or a flushed keyspace |
| Sessions, leases, P1 elevation, approvals | **Valkey** (unchanged) | short-lived, high-churn, already fail-closed |

Rules that make the change safe:

1. **Explicit selection, never a silent fallback.** `SYSADMIN_CONTROL_STORE=postgres`
   (or an explicit `SYSADMIN_DATABASE_URL`) selects the store. With it selected and
   the store unreachable, the auth gateway and the quota manager raise
   `ConnectionError` and the HTTP layer answers **503**; they never fall back to
   the file map or to an in-memory counter (AGENTS.md §4).
2. **File mode is unchanged.** With no such variable set, every existing
   deployment behaves exactly as before — this is a mode switch, not a migration
   that silently rewrites identity.
3. **Only hashes are stored.** `sysadmin_api_keys.token_sha256` holds
   `sha256(token)`; plaintext key material stays in
   `backend/config/keys/*.key` (mode `0600`, Git-ignored). A database dump,
   backup or replica therefore cannot be replayed as a credential.
4. **Files bootstrap the store, the store is the runtime authority.**
   `control_store.cli import-file-keys` imports the provisioned files once; from
   then on a key is valid only if it is present and unrevoked in the store, so the
   two layers cannot disagree about identity.
5. **Conservative accounting.** An admitted-but-unreported reservation is charged
   at its estimate; reconciliation makes that charge permanent
   (`settle_expired`) and then raises the day's counter to the durable total in a
   single atomic step, so two processes reconciling at once cannot double-charge.
6. **Secrets never reach logs or audit events.** The DSN is only ever rendered
   through `dsn.redact()`; audit events carry a 12-character non-reversible
   fingerprint, never a token or hash prefix of the plaintext.

## Implementation

| Path | Role |
|---|---|
| `backend/config/postgres/postgres.sh` | native cluster lifecycle: `check` / `provision` / `start` / `stop` / `status` / `health` / `dsn` / `psql` / `backup` / `restore` / `destroy`. Cluster under `backend/data/postgres`, socket under `backend/run/postgres`, log under `backend/logs/postgres.log`, password in `backend/config/keys/postgres-password.key` (mode `0600`), loopback `5433`. Never installs a package: missing binaries exit **3** with the `apt-get`/`dnf`/`brew` command to run. |
| `backend/services/control_store/` | `dsn.py` (mode + DSN resolution, redaction), `connection.py` (lazy driver import, fail-closed wrapper), `executor.py`, `schema.py` (idempotent DDL), `key_store.py` (key lifecycle), `ledger.py` (durable ledger), `cli.py` (operator commands). Stdlib-only at import time. |
| `backend/services/auth_gateway/server.py` | `resolve_identity()` shared by ForwardAuth and LiteLLM custom auth; `GET /api/v1/admin/keys`, `POST /api/v1/admin/keys/{user}/rotate`, `POST /api/v1/admin/keys/{user}/revoke` (admin-only, audited, key returned once with `Cache-Control: no-store`). |
| `backend/services/auth_gateway/quota_manager.py` | mirrors reservations/settlements/direct usage into the ledger, reconciles the day counter to the durable floor once per user/day per process, and fails closed through `_UnavailableLedger` when the store is selected but unusable. |

## Consequences

- **Positive.** Key rotation and revocation now exist as atomic, audited
  operations with a test that would have caught the first implementation's bug
  (rotation revoked the key it had just issued); identity has one runtime
  authority; quota state has a durable floor, so a restart cannot hand a user a
  second daily budget; the database holds no usable credential.
- **Negative / accepted.** A second stateful service to provision, back up and
  monitor (PR-D1, PR-D2); one more fail-closed dependency on the admission path
  (a store outage now rejects new work with 503 instead of admitting it against a
  non-durable counter — the intended direction); key rotation now requires the
  operator to distribute the new key, which the admin API returns exactly once;
  LiteLLM's own virtual-key store and spend logs are **not** yet exercised (see
  below).
- **Not yet wired.** This change deliberately does **not** edit
  `install.sh`, `backend/platform.sh`, `backend/installer_tui.py` or
  `backend/config/roles/*`, because another lane (PR-H1 multi-host roles) holds
  uncommitted changes in exactly those files (AGENTS.md §8). Until the five-line
  wiring in `docs/configuration.md` §7 is applied, an operator selects the store
  per service through `backend/config/roles/deployment.env` (already sourced and
  exported by `platform.sh`) and starts the cluster with
  `backend/config/postgres/postgres.sh start`.

## Evidence

Full record, reproduction commands and the environment caveat:
[`docs/status/CONTROL_STORE.md`](../status/CONTROL_STORE.md).

Measured in this session on the macOS development host (2026-09-24):

- `backend/tests/tier1_unit/test_control_store_dsn.py` (11), `test_control_store_keys.py` (12),
  `test_control_store_ledger.py` (10), `test_durable_identity.py` (8),
  `test_quota_durable_ledger.py` (9), `test_postgres_control_store_script.py` (16):
  **66 new tests, all passing**.
- Whole tier-1/3/4 run after the change: **600 passed, 40 failed, 18 skipped**;
  the 40 failures are byte-identical to the pre-change baseline captured in the
  same session (`/tmp/dbimpl-baseline.txt` → `/tmp/dbimpl-after.txt`), i.e. the
  Linux-only target-adapter/config-deployer and the in-flight multi-host tests
  that fail on this host for environmental reasons.

**Not measured (no claim made):** a live PostgreSQL cluster (the server is not
installed on this host, and no package was installed), end-to-end key
issue/rotate/revoke against a running cluster, LiteLLM starting with
`general_settings.database_url` / `DATABASE_URL` and creating its own tables,
the reconciliation path against a live Valkey, and `platform.sh` starting or
stopping the cluster. Those are the corrective items below.

## Corrective work

1. **Wiring (blocked on the PR-H1 lane, then trivial):** add `postgres` to
   `platform.sh`'s service order / role table / start-stop-status dispatch, call
   `postgres.sh provision` from `install.sh`, and let `installer_tui.py` write
   `general_settings.database_url` instead of popping it.
2. **Live qualification:** run the cluster on the deployment host and record:
   provision → `apply-schema` → `import-file-keys` → rotate → revoke round trip;
   a LiteLLM start with `DATABASE_URL` set; a `pg_dump`/`pg_restore` cycle feeding
   the restore drill (PR-B4); and the multi-worker reconciliation case (PR-B3).
3. **Owner decision:** whether file-provisioned keys remain the provisioning
   input permanently, or whether LiteLLM virtual keys (`/key/generate`,
   Postgres-backed) replace them as the issuance path (spec 02 S4's original
   wording).
