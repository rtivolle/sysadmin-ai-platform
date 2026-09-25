# ADR-0004: llama.cpp (GGUF) engine added alongside vLLM in the model manager

- **Date:** 2026-09-24
- **Status:** Proposed — owner acceptance pending
- **Source of divergence:** `docs/specs/01 — Service Inférence` (vLLM/Dynamo only); `docs/plans/DEVELOPMENT_PLAN.md` §4 (vLLM fast/heavy endpoints, no llama.cpp).

## Context

The specs and plan name only vLLM (and Dynamo) as inference engines. The model
manager (`backend/services/model_manager/`) was extended with a second engine:
`llamacpp`, which serves a single user-selected GGUF file via a loopback-only
OpenAI-compatible `llama-server`. Registration with `engine="llamacpp"` requires
a strict `gguf_file` basename (ending `.gguf`) plus optional `ctx_size`,
`n_gpu_layers`, and `flash_attn`. The downloader fetches only that one file
(`hf_hub_download`); `llamacpp_server.py` supervises the process, resolves the
file under the server-owned model directory, and rejects symlinks/escapes.

This was a pragmatic addition: the only real model ever loaded on this host was a
9B GGUF in llama.cpp (see `docs/status/TEST_READY.md`, "Direct 9B model load
smoke test"), while vLLM is not installed here.

## Decision

Support two engines in the model manager — `vllm` (default) and `llamacpp` — with
the engine recorded in the registry and the inference engine routing a running
local model to the correct port regardless of engine.

## Consequences

- **Positive:** A GGUF path exists for hosts where vLLM is unavailable; the engine
  is explicit in the registry and LiteLLM entries.
- **Negative:** Two supervision/liveness code paths to maintain; the llama.cpp
  path is not live-qualified (no manager-mediated download or start has run).
  `llama.cpp` and GGUF are not mentioned in the source specs, so this broadens
  the inference surface beyond the documented design.

## Evidence

- **Unit-qualified with injected seams:**
  `backend/tests/tier1_unit/test_model_manager.py::test_validate_engine_and_gguf_filename`,
  `::test_gguf_download_fetches_only_selected_file`,
  `::test_gguf_download_rejects_symlink_result_and_records_failure`,
  `::test_llamacpp_command_defaults_and_child_cuda_path`,
  `::test_llamacpp_path_is_confined_and_rejects_symlink`,
  `::test_llamacpp_liveness_does_not_probe_or_signal_on_transient_health_failure`,
  `::test_llamacpp_process_identity_rejects_recycled_pid`,
  `::test_llamacpp_stop_waits_for_pid_before_releasing_port`,
  `::test_llamacpp_start_and_stop_lifecycle`,
  `::test_llamacpp_start_refuses_external_file_before_spawning`.
- **Fail-closed routing of GGUF models:**
  `::test_inference_routes_running_gguf_and_fails_closed_when_registry_is_unavailable`,
  `::test_registered_gguf_unavailable_returns_503_instead_of_simulation`.
- **No qualifying live test:** a real manager-mediated GGUF download or
  llama.cpp start has not run through the admin/API path; only a separate direct
  llama.cpp smoke test is recorded (`docs/status/TEST_READY.md`).

## Corrective work

- Run the full llama.cpp lifecycle (register → download → start → route a real
  completion through LiteLLM) on the GPU host and record it (Phase 2 live
  acceptance per `TEST_READY.md`).
- Record the owner's decision on whether the GGUF/llama.cpp path is a supported
  production engine or a dev-only convenience.
