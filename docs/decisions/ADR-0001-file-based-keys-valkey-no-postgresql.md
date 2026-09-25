# ADR-0001: File-based bearer keys + Valkey replace PostgreSQL-backed LiteLLM virtual keys

- **Date:** 2026-09-24
- **Status:** Proposed — owner acceptance pending
- **Source of divergence:** `docs/specs/02 — Service Quotas & Passerelle API` (LiteLLM virtual-key provisioning via `/key/generate`, `database_url: redis://…`); `docs/plans/DEVELOPMENT_PLAN.md` §3 correction S4 and §4 architecture (`LiteLLM ---- PostgreSQL … durable ledger`); §5 backlog QUO-01 ("Keys survive restart; rotation/revocation work").

## Context

The plan (§3, S4) required PostgreSQL as LiteLLM's database for virtual-key
setup and durable control state, with Valkey reserved for "compatible
distributed counters/leases". Spec 02 provisions per-user LiteLLM virtual keys
with `POST /key/generate` and stores LiteLLM's state via `database_url`.

The implementation does not use PostgreSQL at all. Per-user bearer keys are
random files in `backend/config/keys/<user>.key` (mode `0600`, Git-ignored),
loaded at request time by `auth_gateway/server.py::load_valid_tokens` into a
token→user map. LiteLLM runs with **no** `database_url` — `backend/config/litellm/config.yaml`
has no `general_settings.database_url`, and `backend/installer_tui.py` explicitly
pops any `database_url` from the generated LiteLLM config. LiteLLM's custom auth
(`auth_gateway/litellm_auth.py::sysadmin_custom_auth`) re-reads the same
file-based token map and enforces per-user limits, so the file keys are the
single source of truth for identity at both the auth gateway and LiteLLM.
Valkey holds sessions, quota leases/reservations, approvals, and P1 state.

## Decision

Keep bearer identity as file-provisioned random keys (no PostgreSQL, no LiteLLM
virtual-key store). Use Valkey for all shared, fail-closed runtime state
(sessions, leases, daily token ledger, approvals, P1). Do not introduce a
PostgreSQL-backed virtual-key store in this prototype.

## Consequences

- **Positive:** One identity source (the key files) feeds both the auth gateway
  and LiteLLM custom auth, so the two layers cannot disagree on identity; no
  database to back up/migrate; keys are trivially durable across restarts
  because they are files.
- **Negative:** There is no runtime key lifecycle: keys cannot be rotated,
  revoked, or issued per-user from an API. Revoking a user means editing/deleting
  a file and restarting the services that load it. `load_valid_tokens` re-reads
  the directory on every request, so a file change is observed without restart,
  but the operation is not audited, atomic, or guarded against races. No
  PostgreSQL means no durable relational ledger for the quota/control state the
  plan assigned to it; that state now lives in Valkey (AOF-persisted).

## Evidence

- **File-key identity loading (indirectly exercised):**
  - `backend/tests/tier1_unit/test_auth_login.py::test_login_routes_use_provisioned_hashes`
    validates the PBKDF2 login credential path.
  - Live challenger and platform suites read real key files to authenticate:
    `backend/tests/tier3_concurrency/test_empirical_challenger.py::test_forwardauth_identity_spoofing_prevented`
    and `backend/tests/test_platform.py::test_auth_gateway` / `test_traefik_gateway`
    (both require `backend/config/keys/*` present).
- **No qualifying test for key rotation or revocation.** Grepping
  `backend/tests` for rotation/revocation of *bearer* keys returns only P1
  elevation-token revocation
  (`backend/tests/tier4_recovery/test_m3_adversarial_p1_dr.py::test_p1_elevation_token_revocation_lifecycle`,
  `::test_p1_elevation_multi_token_revocation`), which is a different credential
  type. The login-password rotation flag in
  `backend/config/keys/provision-logins.py --rotate` has no test.
- **No qualifying test for key restart durability.** Files survive restart by
  construction, but the QUO-01 contract ("Keys survive restart; rotation/
  revocation work") is not pinned by any test.

## Corrective work

- Add a bearer-key rotation/revocation mechanism (an admin API that atomically
  replaces or removes a key file/entry, audited and replicated to every layer
  that loads keys) and tests pinning it.
- Add a restart-durability test that proves a provisioned key set survives a
  service restart and still authenticates, matching QUO-01.
- Record the owner's decision on whether file-based keys are the permanent
  identity model or an interim until PostgreSQL virtual keys.
