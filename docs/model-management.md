# Local model management

The **model manager** turns a HuggingFace link into a locally served,
selectable model: register a repo, download its snapshot, start a local vLLM
OpenAI-compatible server, and have the model appear through the inference engine
and LiteLLM. It is admin-only and lives in `backend/services/model_manager/`.

> **Status.** The registry, downloader, vLLM supervisor and LiteLLM sync are
> implemented and unit-tested with injected process/HTTP seams. A real vLLM
> start and a real HuggingFace download were **not** run on this development
> host (vLLM is not installed and the account has no verified egress); they are
> "not measured" here. Treat the local-serving path as unqualified until it runs
> on the GPU host.

## 1. Prerequisites

- **vLLM** on the GPU host, pinned to a build that matches the driver/CUDA and
  PyTorch versions. It is intentionally **not** installed by `install.sh` — see
  the [vLLM installation guide](https://docs.vllm.ai/en/stable/getting_started/installation/).
  Set `VLLM_BIN` if the binary is not on `PATH`.
- **A HuggingFace token** for gated/private repos, in
  `backend/config/keys/hf-token.key` (0600, Git-ignored) or `HF_TOKEN`. Public
  repos work without one.
- **Disk space** in the model store (`backend/data/models/`, Git-ignored).
- For air-gapped/mirrored hubs set `HF_ENDPOINT` and/or `HF_HUB_OFFLINE`.

## 2. Lifecycle

```text
register  ->  download      ->  start (vLLM)  ->  running  ->  selectable
POST /models  POST /models/{n}/download  POST /models/{n}/start
                 |                |                |
             registered  ->  downloaded  ->   starting  ->  running
                                                              |
                                             stop / delete  v
```

1. **Register** a HuggingFace repo (or URL). The served name defaults to the
   repo's last path segment; `org/model` becomes `model`.
2. **Download** the snapshot with `huggingface_hub.snapshot_download`. Resume is
   handled by the Hub cache, so re-running a download reuses completed files.
   A disk-space floor (1 GiB by default) is checked first.
3. **Start** a local vLLM server on a free port in `MODEL_PORT_START..END`
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
| POST | `/api/v1/models` | Register `{hf_repo\|reference, name?, revision?, quantization?, max_model_len?, tensor_parallel_size?, gpu_memory_utilization?}`. |
| POST | `/api/v1/models/{name}/download` | Start a background snapshot download (`202`). |
| POST | `/api/v1/models/{name}/start` | Start the vLLM server (`202`; poll the model). |
| POST | `/api/v1/models/{name}/stop` | Stop the vLLM server and refresh LiteLLM. |
| POST | `/api/v1/models/{name}/restart` | Stop then start. |
| GET | `/api/v1/models/{name}/logs?tail=` | Tail the vLLM log (1 KiB–256 KiB). |
| DELETE | `/api/v1/models/{name}?delete_files=0\|1` | Unregister (and optionally delete files); refused while running. |

Mutations emit `model_register`, `model_download`, `model_start`, `model_stop`
and `model_delete` audit events (see
[status/AUDIT_CENSUS.md](status/AUDIT_CENSUS.md)).

## 4. How a model becomes "available"

- The **inference engine** (`:8000`) lists running registry models in
  `GET /v1/models` and routes a completion whose `model` matches a running
  model to that model's own vLLM port. A registered-but-stopped model returns
  `503`, not a simulated answer.
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
| `backend/logs/models/<name>.log` | vLLM stdout/stderr (rotated manually for now). |
| `backend/config/keys/hf-token.key` | Optional HuggingFace token (0600). |
| `MODELS_DIR` / `MODELS_REGISTRY` | Override the store / registry path. |
| `HF_TOKEN` / `HF_ENDPOINT` / `HF_HOME` / `HF_HUB_OFFLINE` | Hub auth, mirror, cache, offline. |
| `VLLM_BIN` | vLLM executable (default `vllm` on `PATH`). |
| `MODEL_PORT_START` / `MODEL_PORT_END` | vLLM port range (default 8100–8199). |
| `MODEL_LOG_DIR` | vLLM log directory. |
| `LITELLM_CONFIG` | LiteLLM config path to manage. |
| `MODEL_INFERENCE_API_BASE` | Base URL used in generated LiteLLM entries (default `http://127.0.0.1:8000/v1`). |

## 6. Security and honest limitations

- **Admin-only, credential-derived.** Every mutation re-derives the caller's
  identity from credentials; a header cannot elevate. Model names and repo ids
  are validated (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`) and paths are confined to
  the models directory, so a request cannot traverse the filesystem.
- **Supply chain.** Downloading a model pulls third-party weights and starting
  vLLM executes them. Only an administrator should register and start models,
  and only from trusted repositories. The registry records `hf_repo` and
  `revision` so a served model can be traced to its source.
- **Sovereignty.** A download is intentional egress. Set `HF_ENDPOINT` to an
  internal mirror and `HF_HUB_OFFLINE=1` for air-gapped runs.
- **Not qualified.** No GPU-fit guarantee (a 12 GB card may not hold a given
  model at the chosen context), no per-model GPU isolation, and no live vLLM
  or download qualification in this repository yet. The registry is a local
  file, not a shared/cluster store — one host, one manager.

See [security.md](security.md) for the platform threat model and
[status/TEST_READY.md](status/TEST_READY.md) for what was actually measured.
