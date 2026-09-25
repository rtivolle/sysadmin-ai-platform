# ADR-0007: Single-host loopback topology (three-machine split deferred)

- **Date:** 2026-09-24
- **Status:** Proposed — owner acceptance pending
- **Source of divergence:** `docs/specs/00` (four-layer architecture across an 8-GPU cluster); `docs/plans/DEVELOPMENT_PLAN.md` §4 (single host, loopback) vs `docs/plans/MULTI_HOST_DEPLOYMENT.md` (PR-H1, unimplemented).

## Context

Spec 00 describes a four-layer stack (Traefik → dsh runtime → LiteLLM/Valkey →
Dynamo/vLLM) over an 8× RTX 8000 cluster with distinct port bindings and, for
production, a multi-machine deployment. The plan §4 sketches a single host but
also records a future three-machine split; `docs/plans/MULTI_HOST_DEPLOYMENT.md`
(web-delivery W / inference I / data D) is the unimplemented design tracked as
PR-H1.

The implementation is **one host, loopback-only** for every service except
Traefik (8080/8443) and the harness gateway (3085). `docs/architecture.md` §6
states this explicitly and calls out that "a single host is a single point of
failure." No `W`/`I`/`D` split, no remote Valkey/VictoriaLogs, no inter-machine
firewall matrix, no TLS between machines (PR-H2) exist.

## Decision

Run the entire stack on one host with loopback bindings for all internal
services. Defer the three-machine deployment (PR-H1) and inter-machine TLS
(PR-H2) until the owner chooses a production topology.

## Consequences

- **Positive:** Minimal attack surface (only Traefik and the harness gateway are
  exposed); no inter-machine credential transport to harden.
- **Negative:** Single point of failure; no scale-out; backups do not provide HA.
  The multi-host design (and its acceptance criteria: firewall matrices, key-copy
  runbook, fail-closed across a LAN partition) remains unimplemented.

## Evidence

- **Loopback bindings are pinned by config:** `backend/platform.sh` binds Valkey
  `127.0.0.1:6379`, VictoriaLogs `127.0.0.1:9428`, SeaweedFS `127.0.0.1`, and
  starts all services with loopback hosts (`docs/configuration.md` §2).
- **Single-host reachability is tested live:**
  `backend/tests/test_platform.py::test_valkey`, `::test_victorialogs`,
  `::test_seaweedfs` exercise loopback ports on one host.
- **No qualifying test for multi-machine topology** — `docs/plans/MULTI_HOST_DEPLOYMENT.md`
  is design-only; no firewall matrix, key-copy, or LAN-partition test exists.

## Corrective work

- PR-H1 (implement the three-machine split) and PR-H2 (inter-machine TLS) when a
  production topology is chosen; until then, record the owner's acceptance of
  single-host operation and its single-point-of-failure.
