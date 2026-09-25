# ADR-0012: Separate daily token ledger replaces monetary `max_budget` / `2000` credits

- **Date:** 2026-09-24
- **Status:** Proposed — owner acceptance pending (divergence already recorded in `docs/development.md` §5 and `DEVELOPMENT_PLAN.md` §3, S5)

- **Source of divergence:** `docs/specs/02` (`max_budget: 2000`, `default_max_internal_user_budget: 2000.0` interpreted as 2M tokens/day); `docs/plans/DEVELOPMENT_PLAN.md` §3 (S5).

## Context

Spec 02 configures LiteLLM with `max_budget: 2000` / `default_max_internal_user_budget: 2000.0`
and reads it as "2 million tokens per user per day". LiteLLM budgets are
monetary; this does not express a token cap. The plan §3 requires "a separate
token ledger and midnight rollover."

The implementation keeps LiteLLM's `default_max_internal_user_budget: 2000.0` in
`backend/config/litellm/config.yaml` but adds a **separate Valkey-backed daily
token ledger** (`auth_gateway/quota_manager.py`): `reserve_daily_token_budget`
reserves an estimate before dispatch, `settle_daily_token_reservation` replaces
it with actual usage exactly once, attributed to the admission day, with a
configurable timezone rollover (`QUOTA_TIMEZONE`, default UTC) and an 8-day
reservation TTL. RPM/TPM stay as LiteLLM rate limits; the daily token ceiling
(2M/day, 10M P1) is enforced independently.

## Decision

Enforce daily token limits with a dedicated Valkey ledger using reserve/settle
semantics and midnight rollover, rather than LiteLLM monetary budgets.

## Consequences

- **Positive:** A real token/day cap with rollover semantics, exact-once
  settlement, and fail-closed behaviour under store outage.
- **Negative:** Diverges from spec 02's monetary-budget approach; the token
  ledger must be reconciled after restart (conservative reservation retained
  until settlement).

## Evidence

- **Reservation/settlement lifecycle:**
  `backend/tests/tier1_unit/test_daily_token_reservation.py::test_local_reservation_admits_until_budget_exhausted`,
  `::test_local_duplicate_reservation_id_rejected`,
  `::test_local_settlement_replaces_estimate_and_is_idempotent`,
  `::test_local_settlement_frees_reserved_capacity`,
  `::test_required_shared_store_outage_fails_closed`,
  `::test_live_reservation_settlement_roundtrip`,
  `::test_live_reservation_is_atomic_across_managers`.
- **Rate-limit and rollover enforcement:**
  `backend/tests/tier3_concurrency/test_rate_limits.py::test_rpm_quota_exhaustion_429`,
  `::test_tpm_rate_limit_429`, `::test_daily_token_budget_exhaustion_and_rollover`;
  `backend/tests/tier3_concurrency/test_empirical_challenger.py::test_quotamanager_daily_budget_2m_and_midnight_rollover`.

## Corrective work

- None; the divergence is already documented (`docs/development.md` §5). Record
  the owner's acceptance of the token-ledger semantics (including timezone
  choice) over monetary budgets.
