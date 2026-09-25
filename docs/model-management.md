# Local model management

The **model manager** turns a HuggingFace link into a locally served,
selectable model: register a repo, download its snapshot (vLLM) or one explicitly
selected GGUF file (llama.cpp), start a loopback-only OpenAI-compatible server,
and have the model appear through the inference engine and LiteLLM. It is
admin-only and lives in `backend/services/model_manager/`.

> **Status.** The registry, downloader, vLLM and llama.cpp supervisors, and
> LiteLLM sync are implemented and unit-tested with injected process/HTTP seams.
> A manager-mediated llama.cpp start/download and real vLLM start were **not**
> run through this path at this stage. A separate direct llama.cpp smoke test is
> recorded in [status/TEST_READY.md](status/TEST_READY.md), but does not qualify
> the managed path. Treat local serving as unqualified until the full lifecycle
> runs through the GPU host's admin/API path.

## 1. Prerequisites

- **vLLM** is installed by `install.sh` on `all` and `inference` roles in the
  isolated `backend/.vllm-venv` environment using vLLM's automatic PyTorch
  backend selection. This is a large GPU-specific download; the host still
  needs a supported Linux/Python/GPU/driver stack. See the [vLLM installation
  guide](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/) for
  platform requirements. Set `VLLM_BIN` to override the installed executable.
- **llama.cpp** on the GPU host for GGUF models. Install/build `llama-server`
  for that host; it is not installed by `install.sh`. Set `LLAMACPP_BIN` if it
  is not on `PATH`. CUDA runtime libraries must be reachable in the child
  process; use `LLAMACPP_LD_LIBRARY_PATH` for additional library directories,
  prepended to inherited `LD_LIBRARY_PATH`.
- **A HuggingFace token** for gated/private repos, in
  `backend/config/keys/hf-token.key` (0600, Git-ignored) or `HF_TOKEN`. Public
  repos work without one.
- **Disk space** in the model store (`backend/data/models/`, Git-ignored).
- For air-gapped/mirrored hubs set `HF_ENDPOINT` and/or `HF_HUB_OFFLINE`.

## 2. Lifecycle

For native GPU installation and the full vLLM YAML configuration, see
[NVIDIA and vLLM setup](nvidia-vllm.md). Driver installation is opt-in;
`./install.sh --nvidia` prints a plan without installing anything.

```text
register  ->  download      ->  start (selected engine)  ->  running  ->  selectable
POST /models  POST /models/{n}/download  POST /models/{n}/start
                 |                |                |
             registered  ->  downloaded  ->   starting  ->  running
                                                              |
                                             stop / delete  v
```

1. **Register** a HuggingFace repo (or URL). `engine` defaults to `vllm`; the
   alternative `llamacpp` engine requires one `gguf_file` basename. The served name defaults to the
   repo's last path segment; `org/model` becomes `model`.
2. **Download** a vLLM snapshot with `huggingface_hub.snapshot_download`, or
   only the selected GGUF file with `huggingface_hub.hf_hub_download`. Hub cache
   reuse is supported; `HF_HUB_OFFLINE=1` disables network checks for an
   already-cached file. A disk-space floor (1 GiB by default) is checked first.
3. **Start** a local vLLM or llama.cpp server on a free port in `MODEL_PORT_START..END`
   (default 8100–8199). Readiness is `GET /health` plus child liveness.
4. On success the manager writes a managed entry into LiteLLM's `model_list`
   and restarts LiteLLM (`platform.sh service litellm restart`), so the model
   is selectable by its served name. The inference engine also lists it.
5. **Stop** sends SIGTERM, waits a grace period, then escalates to SIGKILL.
   **Delete** removes the registry entry and, with `?delete_files=1`, the files.

## 3. HTTP API (admin-only)

All endpoints require the `admin` role (the master key). A normal user gets
`403`; an anonymous caller gets `401`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/models` | List registered models with status and server info. |
| GET | `/api/v1/models/{name}` | One model plus download-job status. |
| POST | `/api/v1/models` | Register `{hf_repo\|reference, name?, revision?, engine?}`. `engine="llamacpp"` additionally requires `gguf_file` (basename ending `.gguf`); optional `ctx_size` (512–131072, default 2048), `n_gpu_layers` (`"all"` or non-negative integer, default `"all"`), and `flash_attn` (boolean, default `true`). |
| PATCH | `/api/v1/models/{name}` | Update loading parameters of an inactive model (refused with `409` while starting, downloading or running). Accepts only the engine's loading fields; `null` clears a field so the engine default applies at next start. |
| POST | `/api/v1/models/{name}/download` | Start a background snapshot download (`202`). |
| POST | `/api/v1/models/{name}/start` | Start the configured engine (`202`; poll the model). |
| POST | `/api/v1/models/{name}/stop` | Stop the configured engine and refresh LiteLLM. |
| POST | `/api/v1/models/{name}/restart` | Stop then start. |
| GET | `/api/v1/models/{name}/logs?tail=` | Tail the selected engine log (1 KiB–256 KiB). |
| DELETE | `/api/v1/models/{name}?delete_files=0\|1` | Unregister (and optionally delete files); refused while running. |

### 3.1 Loading parameters

Loading parameters are chosen at registration and adjustable afterwards with
`PATCH`. The accepted sets are closed; unknown or immutable fields (name,
`hf_repo`, `revision`, `engine`, `gguf_file`, `path`) are rejected with `400`.

| Engine | Field | Type / bounds | Engine flag |
|---|---|---|---|
| vLLM | `quantization` | string (e.g. `awq`, `gptq`, `fp8`) | `--quantization` |
| vLLM | `max_model_len` | integer ≥ 1 | `--max-model-len` |
| vLLM | `tensor_parallel_size` | integer ≥ 1 | `--tensor-parallel-size` |
| vLLM | `gpu_memory_utilization` | float in (0, 1] | `--gpu-memory-utilization` |
| vLLM | `max_num_seqs` | integer ≥ 1 | `--max-num-seqs` |
| vLLM | `dtype` | `auto`/`half`/`float16`/`bfloat16`/`float`/`float32` | `--dtype` |
| vLLM | `kv_cache_dtype` | `auto`/`fp8`/`fp8_e5m2`/`fp8_e4m3`/`fp8_inc`/`fp8_ds` | `--kv-cache-dtype` |
| vLLM | `enforce_eager` | boolean | `--enforce-eager`/`--no-enforce-eager` |
| vLLM | `enable_prefix_caching` | boolean | `--enable-prefix-caching`/`--no-enable-prefix-caching` |
| llama.cpp | `ctx_size` | integer 512–131072 | `--ctx-size` |
| llama.cpp | `n_gpu_layers` | `"all"` or integer ≥ 0 | `--n-gpu-layers` |
| llama.cpp | `flash_attn` | boolean | `--flash-attn on\|off` |
| llama.cpp | `threads` | integer ≥ 1 | `--threads` |
| llama.cpp | `batch_size` | integer ≥ 1 | `--batch-size` |
| llama.cpp | `mmap` | boolean (default `true`) | `--no-mmap` when false |
| llama.cpp | `mlock` | boolean (default `false`) | `--mlock` when true |

vLLM registration values override the operator `VLLM_CONFIG` file (see
[nvidia-vllm.md](nvidia-vllm.md)); an absent field inherits the file or the
vLLM default. Explicit booleans override in both directions. An absent llama.cpp
field falls back to its API default. Changes only take effect on the next start
of the model.

Mutations emit `model_register`, `model_update`, `model_download`, `model_start`,
`model_stop` and `model_delete` audit events (see
[status/AUDIT_CENSUS.md](status/AUDIT_CENSUS.md)).

## 4. How a model becomes "available"

- The **inference engine** (`:8000`) lists running registry models in
  `GET /v1/models` and routes a completion whose `model` matches a running
  model to that model's own vLLM or llama.cpp port. A registered-but-stopped
  model returns `503`, not a simulated answer. For llama.cpp, routing liveness
  validates the recorded PID's `/proc` command line; HTTP health is used during
  startup readiness, not on every chat request. A transient health miss never
  signals the model process. If process shutdown cannot be confirmed, the
  manager marks it `error` but retains PID/port ownership for an explicit stop
  retry; it does not auto-reap on subsequent reads.
- **LiteLLM** gets a managed `model_list` entry per running model, tagged
  `model_info.managed_by: sysadmin-model-manager`. Hand-written entries such as
  `fast-model`/`heavy-model` are never touched. Because this deployment runs
  LiteLLM from a file with no database, adding or removing a model requires a
  LiteLLM restart, which the manager performs automatically.

## 5. Files and configuration

| Path / variable | Meaning |
|---|---|
| `backend/data/models/<name>/` | Downloaded snapshot. |
| `backend/data/models/registry.json` | Registry (0600, atomic writes). |
| `backend/logs/models/<name>.log` | Selected engine stdout/stderr (rotated manually for now). |
| `backend/config/keys/hf-token.key` | Optional HuggingFace token (0600). |
| `MODELS_DIR` / `MODELS_REGISTRY` | Override the store / registry path. |
| `HF_TOKEN` / `HF_ENDPOINT` / `HF_HOME` / `HF_HUB_OFFLINE` | Hub auth, mirror, cache, offline. |
| `VLLM_BIN` | vLLM executable (platform defaults to the installed `.vllm-venv` binary, then `PATH`). |
| `VLLM_VENV_DIR` | Installer/platform vLLM environment (default `backend/.vllm-venv`). |
| `VLLM_CONFIG` | Operator-owned native YAML; defaults to `backend/config/vllm/serve.yaml`. Empty disables the file. |
| `VLLM_VERSION` | Optional exact installer version; also `--vllm-version VERSION`. |
| `MODEL_PORT_START` / `MODEL_PORT_END` | vLLM port range (default 8100–8199). |
| `MODEL_LOG_DIR` | vLLM log directory. |
| `LITELLM_CONFIG` | LiteLLM config path to manage. |
| `MODEL_INFERENCE_API_BASE` | Base URL used in generated LiteLLM entries (default `http://127.0.0.1:8000/v1`). |

## 6. Security and honest limitations

- **Admin-only, credential-derived.** Every mutation re-derives the caller's
  identity from credentials; a header cannot elevate. Model names and repo ids
  are validated (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`) and paths are confined to
  the models directory, so a request cannot traverse the filesystem.
- **GGUF path.** The API accepts a GGUF filename, never a host path. The
  llama.cpp supervisor independently resolves it under the server-owned model
  directory and rejects missing files, symlinks and paths outside it. Its
  server binds to loopback; the inference gateway returns `503` for a
  registered but stopped/unhealthy model rather than answering with simulation.
- **Supply chain.** Downloading a model pulls third-party weights and starting
  an inference engine executes them. Only an administrator should register and start models,
  and only from trusted repositories. The registry records `hf_repo` and
  `revision` so a served model can be traced to its source.
- **Failed LiteLLM restart.** If manager start writes a model entry but LiteLLM
  fails to restart, the manager stops the new model and removes its managed
  entry from the config file. A partially successful proxy restart may leave
  the running LiteLLM process with stale in-memory config until the next
  successful restart; the selected registered model still fails closed at the
  inference gateway rather than falling back to simulation.
- **Sovereignty.** A download is intentional egress. Set `HF_ENDPOINT` to an
  internal mirror and `HF_HUB_OFFLINE=1` for air-gapped runs.
- **Not qualified.** No GPU-fit guarantee (a 12 GB card may not hold a given
  model at the chosen context), no per-model GPU isolation, and no live manager
  vLLM/GGUF start or download qualification in this repository yet. The registry is a local
  file, not a shared/cluster store — one host, one manager.

See [security.md](security.md) for the platform threat model and
[status/TEST_READY.md](status/TEST_READY.md) for what was actually measured.
