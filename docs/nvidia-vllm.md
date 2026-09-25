# Native NVIDIA and vLLM setup

The driver helper supports Ubuntu hosts with NVIDIA PCI hardware. Other Linux
distributions can use the read-only plan as reference, but automatic installation
refuses them. No containers or container toolkit are involved.

## Driver retrieval and installation

```bash
# Read-only plan, including detection of cards with no working driver:
./install.sh --nvidia
# Explicit opt-in: retrieve and install through the host's signed APT repositories:
./install.sh --nvidia --apply
# Optional branch override (choose one listed by ubuntu-drivers list --gpgpu):
./install.sh --nvidia --apply --driver 570-server
```

Automatic selection uses `ubuntu-drivers install --gpgpu`. The helper installs
kernel headers, DKMS, build tools, PCI inspection, Secure Boot inspection, and
the NVIDIA utilities matching the installed driver package (`nvidia-smi`).
APT/driver failures stop the helper; package transactions are not rolled back.
The TUI offers the same plan and a separate installation confirmation. Unattended
TUI configuration never installs a driver automatically.

Reboot after driver changes; complete MOK enrollment if Secure Boot requests it.
The helper does not disable Secure Boot or reboot the machine. Then run
`nvidia-smi`. Driver package installation alone does not establish GPU readiness.
This follows the [Ubuntu server driver procedure](https://ubuntu.com/server/docs/how-to/graphics/install-nvidia-drivers/).

## vLLM installation

```bash
./install.sh                     # installs vLLM for all/inference roles
./install.sh --skip-vllm         # remote inference or dependency-only setup
# For repeatable installs, select the version qualified for your hardware:
./install.sh --vllm-version VERSION
```

`VERSION` is a placeholder for an actual release, not a literal argument.
The installer preserves an existing isolated `.vllm-venv`, uses Python 3.12 when
creating it, and selects the PyTorch backend with `uv --torch-backend=auto`.
Without a pin, dependency resolution selects the available compatible release.
A PyTorch CUDA allocation/copy probe must pass before installation reports
success; failure exits nonzero and points to driver setup. A successful probe
does not prove that a particular model will fit or start.

Prebuilt wheels supply their CUDA userspace dependencies. A system CUDA
development toolkit is optional for custom compilation; when required, first
configure NVIDIA's signed APT repository for your supported Ubuntu release,
then use `./install.sh --nvidia --apply --cuda-toolkit 12-8` (example version).
This installs `cuda-toolkit-12-8`, without the broad CUDA driver metapackage.
No repository is added automatically. NVSwitch Fabric Manager/NSCQ and DCGM
are specialized host components and are not installed by this helper; select
driver-matched versions when the hardware requires them.
See the [vLLM GPU installation requirements](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/)
and [NVIDIA Ubuntu driver guide](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/ubuntu.html).

## Full serving configuration

Edit `backend/config/vllm/serve.yaml` before starting a managed model, or set
`VLLM_CONFIG` to another operator-owned YAML file in the platform's environment.
Set `VLLM_CONFIG=''` to use vLLM defaults. Missing or malformed configured files
fail startup. The HTTP API cannot choose this path.

The file uses the native [vLLM configuration format](https://docs.vllm.ai/en/latest/configuration/serve_args/),
so version-supported options are available without changing the platform:
precision, quantization, tensor/pipeline parallelism, context length, memory
budget, batching, KV cache, prefix caching, chunked prefill, compilation,
speculative decoding, multimodal limits, reasoning and tool-call parsers.
Unknown or incompatible settings are rejected by the installed vLLM process.
Inspect its supported options with `backend/.vllm-venv/bin/vllm serve --help`.

The supplied starting profile uses one GPU, 85% GPU memory, 8192 context tokens,
32 concurrent sequences, automatic dtype/KV dtype, prefix caching and chunked
prefill. Adjust these to the model and available VRAM; they are not a measured
performance recommendation. Remote model code is disabled by default.

The existing registration settings (`quantization`, `max_model_len`,
`tensor_parallel_size`, `gpu_memory_utilization`) override file defaults.
Host, port, model, served name and Unix socket settings are reserved and rejected
in the file. The platform assigns them and binds the server to loopback.
Restart the model for changes to take effect. To select physical GPUs, set
`CUDA_VISIBLE_DEVICES` in the platform process environment; children inherit it.
Configuration can reference local files or code, so only trusted host operators
should edit it.

Verify through the admin model lifecycle: register, download, start, wait for
healthy status, then perform inference. Package installation and unit tests
are not substitutes for that GPU-host qualification.
