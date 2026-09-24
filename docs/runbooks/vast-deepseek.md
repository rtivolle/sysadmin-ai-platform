# Native DeepSeek deployment: lessons from the H200 test

This is an operator recipe, not an unattended rental script. Obtain authorization
for the actual hourly rate and shutdown policy. Never infer permission to leave
an instance running from an earlier short-lived test, or destroy one after the
operator explicitly takes responsibility for shutdown. No credentials belong in
archives, logs, command arguments, or source control.

## Preflight before downloading

1. Verify the source manifest includes `backend/platform.sh` and all installer
   dependencies. Exclude keys, virtual environments, model data, logs and Git.
2. Survey every GPU, driver, VRAM, available host RAM, disk and sandbox support.
   A working CUDA allocation does not prove Bubblewrap/cgroups work.
3. Install vLLM in its isolated environment. Let its wheel resolve torch;
   selecting torch from the kernel driver's CUDA level alone caused an ABI
   mismatch in this test. Import torchvision, torchaudio and the model registry
   as well as allocating on every GPU. `install.sh` now does this preflight.
4. For DeepSeek JIT, install the matching CUDA toolkit/compiler, cuRAND development
   headers and `ninja-build`. On the measured Ubuntu host, CUDA 13.2 used
   `cuda-nvcc-13-2`, `cuda-cudart-dev-13-2`, `cuda-cccl-13-2`, and
   `libcurand-dev-13-2` from NVIDIA's signed repository. Set `CUDA_HOME` and PATH.
   Driver 570 required `cuda-compat-13-2` and its directory in LD_LIBRARY_PATH;
   this does not replace the host kernel driver. Recheck compatibility for other
   hosts; do not blindly upgrade a rented host's kernel driver.
5. Run inside the vLLM environment:
   `python backend/scripts/verify_vllm_runtime.py --jit --architecture DeepseekV41ForCausalLM`.
   This is a prerequisite check, not evidence of successful inference. The
   measured environment also needed CPU torchaudio 2.11.0 because its CUDA wheel
   rejected torch CUDA 13.2. Do not universally pin this workaround for other
   versions; inspect the actual import failure.
6. Pin the Hub revision, check all checkpoint shards and their aggregate size.
   Do not describe a simulator or an import test as a working full model.

## Serving and DSH

Use `backend/config/vllm/deepseek-v41-h200.example.yaml` as an explicit operator
config. The registry's TP, context and memory values override YAML and must agree.
The measured model needed Engram CPU offload; allow sufficient host RAM.

Set these in the environment used to launch the harness gateway, then restart it:

```sh
export SYSADMIN_DEFAULT_MODEL=deepseek-v41-flash
export SYSADMIN_MODEL_DISPLAY_NAME='DeepSeek V4.1 Flash (4 H200)'
export SYSADMIN_MODEL_CONTEXT_WINDOW=32768
```

DSH's model menu is configured, not automatically discovered from `/v1/models`.
The profile now uses these settings and explicitly labels the default simulators.
New conversations must use the same context capacity as the running model.
32K is a measured deployment setting, not a safe universal default for all GPUs.
The quota gateway separately caps conservative serialized-input bytes plus output
reservation at 32768; it is not a tokenizer. This limit remains enforced. A long
text can therefore receive 413 before exhausting model context. Do not silently
raise quota reservations or weaken admission to work around context errors.

Expose only the needed HTTPS paths, keeping authentication and spoofed-header
stripping. For IP HTTPS, configure Traefik's default TLS store certificate as
well as the certificate list; clients using an IP may send no SNI. A self-signed
certificate with the correct IP SAN must still be explicitly trusted by clients.
Use `NODE_EXTRA_CA_CERTS=/absolute/path/admin.crt dsh --profile <profile>`; never
use global TLS verification disablement. Only distribute the public certificate.
The deployment SSH private key is not automatically present on another computer;
add that computer's public key without replacing existing authorized keys.

## Acceptance tests

Verify authenticated `/v1/models`, unauthenticated denial, and actual output from
the exact managed model. Wait for LiteLLM readiness after its model-list restart;
a transient 502 is not a model failure. Test both non-streaming and streaming
through the public gateway. Consume SSE through `[DONE]`, check content/usage,
and exercise a prompt longer than 8192 tokens. Also test upstream 400 propagation
and downstream stream cancellation. The proxy must retain its httpx client and
response for the whole stream and close both afterward.

The first small inference test missed two real failures: consumed-stream errors
in the proxy, and the DSH system prompt exceeding an 8192-token serving limit.
Passing a two-token completion is not an end-to-end DSH qualification. Verify the
actual harness conversation and tools separately; sandbox operation on this
rental was not qualified. Leave the instance in the operator-authorized state.
