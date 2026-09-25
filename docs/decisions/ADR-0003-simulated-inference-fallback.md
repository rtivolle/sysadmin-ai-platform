# ADR-0003: Simulated inference fallback in place of a deployed standalone vLLM

- **Date:** 2026-09-24
- **Status:** Proposed — owner acceptance pending
- **Source of divergence:** `docs/specs/01 — Service Inférence`; `docs/plans/DEVELOPMENT_PLAN.md` §4 ("Standalone vLLM fast/heavy endpoints"), §6 (M0 fallback), PR-C1 (real vLLM on the RTX 8000).

## Context

The plan's architecture (§4) ships "standalone vLLM fast/heavy endpoints" as the
real inference backend. The implementation has no deployed vLLM: the inference
engine (`backend/services/inference_engine/server.py`) returns **deterministic
simulated completions** that emit valid ReAct traces for common prompts (search,
runbook, diff, restart) whenever no real backend is reachable. Real backends are
reached only when (a) `UPSTREAM_VLLM_URL` is set (proxy to `<url>/v1`), or (b) a
locally-managed model is `running` in the model registry (routed to its own
vLLM/llama.cpp port). A registered-but-stopped model returns `503`, not a
simulated answer.

The plan anticipated this in §6 ("If M0 inference fails, retain application work
against a local fake OpenAI-compatible endpoint while resolving the stack"). The
simulator is a development/test aid, not a model; PR-C1 is the outstanding work
item for real vLLM on the hardware.

## Decision

Ship the OpenAI-compatible inference facade with three modes — local managed
model, upstream vLLM proxy, and a clearly-labelled simulator fallback. Keep the
simulator deterministic so it can drive ReAct-loop and e2e tests without a GPU.

## Consequences

- **Positive:** The full request path (auth → quota → ReAct → tools → audit) is
  exercisable with no GPU, enabling the 480+ test suite and the synthetic
  30-task pack to run hermetically.
- **Negative:** No real model quality, latency, VRAM, or tokenization behaviour
  is exercised by the default path. The platform reports
  `{"upstream_vllm": "local-simulated"}` on `/health`. Real-model performance
  (INF-01/INF-02, PR-C1) remains unmeasured; the owner-scored 30-task evaluation
  (PR-B5) depends on it.

## Evidence

- **Simulator drives the ReAct loop and e2e tests:**
  `backend/tests/tier1_unit/test_agent_runtime.py::test_react_loop_diagnose_log`,
  `::test_react_loop_mutating_hitl_approval`; `backend/tests/tier1_unit/test_m2_readonly_slice.py::test_chat_sse_streaming_response`
  (all use `simulate_chat_completion` via `mock_inference`).
- **Simulation is a labelled fallback, not silent:** `backend/tests/tier1_unit/test_model_manager.py::test_registered_gguf_unavailable_returns_503_instead_of_simulation`
  pins that a registered-but-unavailable model is a `503`, never a simulated
  answer; `::test_inference_routes_running_gguf_and_fails_closed_when_registry_is_unavailable`
  pins fail-closed registry behaviour.
- **No qualifying test for real vLLM inference** — `vllm` is not installed here;
  the model manager's vLLM path is exercised only against injected process/HTTP
  seams (`test_model_manager.py::test_vllm_start_and_stop_lifecycle`,
  `::test_vllm_start_unhealthy_is_error`). Real 14B/32B load is not measured.

## Corrective work

- PR-C1: stand up real vLLM on the RTX 8000, pin the stack, and point
  `inference_engine` at it; keep the simulator as a clearly-labelled dev fallback
  (acceptance criterion already stated in the roadmap).
- Record the owner's acceptance that simulated completions may be present in any
  environment without a configured real backend.
