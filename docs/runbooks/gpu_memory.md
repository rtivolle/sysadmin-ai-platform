# Alert runbook: `gpu_memory`

## Meaning

A GPU's VRAM usage is above 90 % (`observability_gpu_memory_used_percent{gpu=…}
> 90` for 5 minutes). This rule only evaluates when `nvidia-smi` reports GPUs;
on a host without an NVIDIA GPU it never fires.

## Impact

An over-committed GPU throttles local model serving (vLLM/llama.cpp), causes
OOM aborts of inference workers, and degrades chat latency.

## Diagnosis

```bash
nvidia-smi                                   # per-GPU memory, util, processes
./platform.sh logs models                    # local model server logs
./platform.sh survey                         # device inventory and model-store usage
```

## Remediation

- Stop unused local model servers via the model-manager API
  (`POST /api/v1/models/{name}/stop`) or `./platform.sh dashboard`.
- Reduce context length or GPU layers for the running model.
- If multiple models contend for one GPU, run them serially or on separate GPUs.

## Escalation

Escalate to **owner** (currently `owner-pending`) if the workload genuinely
needs more VRAM than the host provides — this is a capacity decision, not a
restart.
