# ADR-0010: DeepSeek Harness is opt-in; the backend ships its own ReAct runtime

- **Date:** 2026-09-24
- **Status:** Proposed — owner acceptance pending
- **Source of divergence:** `docs/specs/03 — Service Agent & Outils` (DeepSeek Harness + Cordis is THE runtime); `docs/plans/DEVELOPMENT_PLAN.md` §4 ("Per-user Harness runtime + home + session store").

## Context

Spec 03 names DeepSeek Harness (`dsh`, Cordis microkernel, TypeScript plugins)
as the agent runtime, with tools registered as Cordis plugins. The plan §4 also
shows "per-user Harness runtime".

The implementation ships **two** runtimes:

1. A hand-written server-side ReAct loop in
   `backend/services/agent_runtime/` (router, parser, react_loop, tool_registry,
   session_store, workspace, cancellation) exposed by the agent platform on
   `3080`. This is the default runtime used by `sysadmin-chat` and the Traefik
   `/api/v1/agent` route.
2. An opt-in multi-user DeepSeek Harness gateway in `packages/harness-integration/`
   that boots **one real `dsh` process per authenticated user** (isolated
   `DSH_HOME`, workspace, port 3180–3280, `SYSADMIN_LITELLM_KEY`), plus a
   Cordis profile (`profile/cordis.patch.yml`) and a sysadmin plugin bundle
   (`dsh-plugin-sysadmin`) whose command policy and audit mirror the backend.

The harness is single-tenant by design (one credential store, one session store,
one workspace per process), hence the per-user gateway. It is started with
`./platform.sh harness`, not part of `start_all`.

## Decision

Keep the backend ReAct runtime as the default and authoritative path, and ship
the DeepSeek Harness integration as an opt-in multi-user gateway that runs the
real `dsh` per user. Mirror the command policy and audit schema so both runtimes
enforce the same contract.

## Consequences

- **Positive:** The platform is runnable without `dsh` (no Node/dsh dependency
  for the core stack); the harness path satisfies the spec's Cordis/plugin model
  when the operator installs `dsh`.
- **Negative:** Two runtimes to keep in lock-step (policy, audit, tool dispatch).
  The harness path is not exercised by `start_all` and requires the external
  `@deepseek-ai/dsh` binary.

## Evidence

- **Backend runtime:** `backend/tests/tier1_unit/test_agent_runtime.py::test_parser_standard_react`,
  `::test_parser_malformed_json_recovery`, `::test_react_loop_diagnose_log`,
  `::test_react_loop_mutating_hitl_approval`, `::test_cross_user_session_isolation`.
- **Python/JS policy parity (the lock-step contract):**
  `packages/harness-integration/tests/policy.test.mjs` asserts the JS command
  policy matches the Python gate action-for-action.
- **Harness package:** `packages/harness-integration/tests/gateway.test.mjs`,
  `::session-persistence.test.mjs`, `::audit.test.mjs`, `::admin.test.mjs`,
  `::branding.test.mjs`, `::surface.test.mjs`; live verification
  `scripts/verify-harness.mjs` (8/8 real-dsh checks) and
  `scripts/verify-live-gateway.mjs` (37/37).
- **No qualifying test** that the two runtimes produce identical tool *behaviour*
  end-to-end (only policy classification is parity-tested). The canonical audit
  field set is pinned Python-side by
  `backend/tests/tier1_unit/test_audit_census.py` (e.g.
  `::test_writer_spools_canonical_schema`) and JS-side by
  `packages/harness-integration/tests/audit.test.mjs`, but no test asserts the two
  writers emit byte-identical field sets.

## Corrective work

- Decide which runtime is the production path and document it as such; if both
  remain, add an end-to-end parity check beyond command classification (tool
  results, approval flow, audit events) between the backend and the harness.
