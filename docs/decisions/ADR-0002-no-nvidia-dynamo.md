# ADR-0002: NVIDIA Dynamo (KV-aware routing, P/D disaggregation) not implemented

- **Date:** 2026-09-24
- **Status:** Proposed — owner acceptance pending (deferral already recorded in the plan)
- **Source of divergence:** `docs/specs/01 — Service Inférence (NVIDIA Dynamo & vLLM)`; `docs/plans/DEVELOPMENT_PLAN.md` §1, §3 (S1–S3), §4, §5 OPT-01.

## Context

Spec 01 specifies NVIDIA Dynamo as the inference orchestrator: a KV-aware router
(claimed ">50 % TTFT reduction"), prefill/decode (P/D) disaggregation across
eight RTX 8000 GPUs, and a KV Block Manager, deployed as a `docker-compose.dynamo.yml`
of `dynamo-frontend` plus `dynamo-worker-{prefill,decode,fast}` containers.

The plan already flagged this in §3 (Turing is not in Dynamo's published
supported architecture list, KV-aware benefits lack a working event/discovery
path) and §1/§5: ship the validated vLLM baseline, treat Dynamo as an optional,
separately-scoped experiment (OPT-01) rather than a prerequisite.

The implementation contains **no Dynamo component**. There is no
`dynamo.frontend`, no discovery backend, no KV routing, no P/D disaggregation.
Inference flows LiteLLM → `backend/services/inference_engine/server.py`, which
either proxies to an upstream vLLM (`UPSTREAM_VLLM_URL`), routes to a locally
managed vLLM/llama.cpp server, or returns a simulated completion.

## Decision

Do not implement NVIDIA Dynamo. Keep the single-inference-engine facade with
vLLM (and llama.cpp) as the real backends. Defer any Dynamo investigation to
OPT-01 as a separately estimated experiment.

## Consequences

- **Positive:** No unvalidated Turing/Dynamo compatibility risk; no dependency on
  mutable `nvcr.io` images or a discovery/event path that was not proven.
- **Negative:** None of the claimed KV-aware routing benefits (TTFT reduction,
  cache re-use across agent turns) or P/D disaggregation capacity efficiency are
  realized. The agent's iterative-turn cache locality is not exploited.

## Evidence

- **No Dynamo code exists** anywhere in `backend/services/` or `packages/` (grep
  confirms); the inference facade is `backend/services/inference_engine/server.py`.
- **Fail-closed routing contract (adjacent, not Dynamo):**
  `backend/tests/tier1_unit/test_inference_gateway.py::test_quota_rejection_does_not_bypass_gateway`,
  `::test_gateway_failure_never_uses_direct_inference` pin that the gateway never
  bypasses quotas and never falls back to direct inference on gateway failure.
- **No qualifying test for KV routing / P/D disaggregation** — no such component
  exists to test. This is a scope reduction, not a contract that needs testing.

## Corrective work

- None required for the prototype; if the owner later wants Dynamo, OPT-01
  (≤5 person-days) is the entry point. Record the owner's go/no-go on Dynamo as a
  standalone decision.
