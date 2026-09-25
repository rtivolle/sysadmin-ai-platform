# ADR-0008: Port and layout choices (3080 agent platform collision; Traefik 8080/8443; harness 3085/3180–3280)

- **Date:** 2026-09-24
- **Status:** Proposed — owner acceptance pending
- **Source of divergence:** `docs/specs/00` port map (dsh `3080`, Traefik `80/443`, LiteLLM `4000`, Valkey `6379`, SeaweedFS `8333/9333`, VictoriaLogs `9428`, inference `8000`).

## Context

Spec 00 assigns the DeepSeek Harness web surface to TCP `3080` and Traefik to
`80`/`443`. The implementation reuses `3080` for the **agent platform**
(`backend/services/agent_tools/server.py`, default `127.0.0.1:3080`) and moves
Traefik to `8080`/`8443` (+ dashboard `8081`), adds an auth gateway on `3081`,
a harness gateway on `3085`, and per-user harness instances on `3180–3280`.

This creates a real collision: `3080` is also the default DeepSeek Harness web
port, so on a host already running `dsh` the agent platform cannot bind. The
workaround (`SYSADMIN_AGENT_PORT=3090`) is documented in
`docs/status/TEST_READY.md` ("Host note: port 3080") and wired through
`backend/platform.sh` and `backend/config/traefik/dynamic.yml`.

## Decision

Keep the agent platform on loopback `3080` by default (with `SYSADMIN_AGENT_PORT`
override), Traefik on `8080/8443/8081`, auth gateway `3081`, harness gateway
`3085`, per-user harness `3180–3280`. Document that `3080` must be kept free of
the harness web UI, or both `platform.sh` and the Traefik `agent-service` route
must point at the same alternative port.

## Consequences

- **Positive:** Internal services are loopback-only; the split gives the auth
  gateway, agent platform, and harness gateway distinct ports for independent
  supervision.
- **Negative:** Diverges from the spec's published port map; `3080` is a shared
  namespace with the harness web default, so full-stack Traefik runs can break
  silently if the harness owns `3080`. The Traefik `agent-service` route is
  static and must be edited by hand when the port moves (the live harness run
  left Traefik stopped for exactly this reason).

## Evidence

- **Port wiring is exercised live:** `backend/tests/test_platform.py::test_traefik_gateway`
  (Traefik routing/ForwardAuth) and `::test_agent_tools_and_security` (agent
  platform) run on the default ports; the harness gateway path is covered by
  `packages/harness-integration/tests/gateway.test.mjs` and the live
  `scripts/verify-live-gateway.mjs` (37/37 checks on `3085`, instances on
  `3210/3211`).
- **No qualifying test for the 3080 collision** — the divergence is documented in
  `docs/status/TEST_READY.md` rather than pinned by a test.

## Corrective work

- Decide and record the canonical port for the agent platform (recommend a
  non-`3080` loopback port to avoid the harness collision) and make the Traefik
  `agent-service` route follow the same `SYSADMIN_AGENT_PORT` override
  automatically.
