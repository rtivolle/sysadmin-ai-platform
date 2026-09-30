# Project documentation

This directory documents the **implemented** on-premises sysadmin AI platform that
lives in this repository. It complements — and does not replace — the source
requirement specifications:

- [`../README.md`](../README.md) — short orientation and quick start.
- [`../AGENTS.md`](../AGENTS.md) — repository map, golden commands and guardrails.
- [plans/DEVELOPMENT_PLAN.md](plans/DEVELOPMENT_PLAN.md) — the proposed implementation baseline and backlog.
- [plans/PRODUCTION_READINESS.md](plans/PRODUCTION_READINESS.md) — the prioritized production-readiness roadmap: one-agent work items with acceptance criteria, dependencies and tracking.
- [plans/MULTI_HOST_DEPLOYMENT.md](plans/MULTI_HOST_DEPLOYMENT.md) — design for the three-machine topology (web delivery / inference / data); unimplemented, implemented by PR-H1.
- [status/TEST_READY.md](status/TEST_READY.md) — the latest recorded verification results and limits.
- [status/AUDIT_CENSUS.md](status/AUDIT_CENSUS.md) — the event-by-event audit completeness census: every audited path, its event, and the paths still unaudited.
- [status/BENCHMARK_REPORT.md](status/BENCHMARK_REPORT.md) — the M4 qualification report and its re-verification addendum.
- [specs/](specs/README.md) — the six French design specifications (also delivered as `.docx`). These are the authoritative statement of intent; this documentation describes what the code *actually does*.

> **Status.** This is a prototype. It is **not qualified for production target
> changes**. The scoped target adapter can propose and execute allow-listed
> service actions and staged configuration deployments, but its privileged
> execution boundary and staging deployment have not been qualified, and a
> `systemctl` command inside the Bubblewrap sandbox does not restart a host
> service. See [security.md](security.md) and [testing.md](testing.md) for the
> full list of open qualification work.

## Contents

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | System context, layers, trust boundaries and end-to-end request flows. |
| [services.md](services.md) | The Python backend services: agent runtime, tools, auth gateway, quota, approval gate, control store, target adapter, target executor, model manager, observability, resilience, inference. |
| [harness-integration.md](harness-integration.md) | The DeepSeek Harness package: multi-user gateway, per-user instance manager, backend-wiring plugin and profile. |
| [security.md](security.md) | Identity, sandbox, approval gate, audit, threat model and known gaps. |
| [http-api.md](http-api.md) | Every HTTP endpoint exposed by the platform. |
| [tools.md](tools.md) | Reference for the four bounded sysadmin tools. |
| [operations.md](operations.md) | Install, start, stop, CLI usage, logs, dashboards and troubleshooting. |
| [INSTALL.md](INSTALL.md) | Installation guide: prerequisites, machine roles, single-node and fleet-split installs, GPU/NVIDIA setup, post-install verification, troubleshooting. |
| [update.md](update.md) | Platform self-update (`update.sh`): fast-forward/overlay module updates, options, invariants and rollback. |
| [multi-host.md](multi-host.md) | One machine or a three-machine split (web/inference/data): choosing the role, staged bring-up, key copy, firewall, verification checklist. |
| [configuration.md](configuration.md) | Configuration files, environment variables, ports and paths. |
| [model-management.md](model-management.md) | Register, download and locally serve HuggingFace models through vLLM and llama.cpp. |
| [model-promotion.md](model-promotion.md) | Versioned model rollout: staging → canary → prod, canary traffic, rollback, and the node-agent canary duties (to qualify in lab). |
| [gpu-fleet.md](gpu-fleet.md) | GPU fleet management: node-agent, fleet registry, desired-state scheduler, placement control loop and dynamic LiteLLM routing. |
| [nvidia-vllm.md](nvidia-vllm.md) | Ubuntu NVIDIA driver detection, optional CUDA toolkit, and native vLLM configuration. |
| [observability.md](observability.md) | Out-of-process metrics collector, alerting engine, Prometheus exposition, and VictoriaLogs summary. |
| [sovereignty.md](sovereignty.md) | Sovereign / blocked-egress operation: air-gap verification, mirror manifests, and egress census. |
| [runbooks/](runbooks/) | Operator runbooks: alert remediation recipes and the multi-GPU Vast/H200 recipe ([runbooks/vast-deepseek.md](runbooks/vast-deepseek.md)). |
| [decisions/README.md](decisions/README.md) | Architectural Decision Records (ADR-0001 through ADR-0014). |
| [backup-restore.md](backup-restore.md) | Backup, clean-staging restore and the disaster-recovery drill. |
| [testing.md](testing.md) | Test tiers, how to run them, recorded results and environment limits. |
| [development.md](development.md) | Repository layout, conventions and extension points. |
| [glossary.md](glossary.md) | Terms and abbreviations used across the project. |
| [specs/](specs/README.md) | The six source specifications and the code that implements each. |
| [plans/DEVELOPMENT_PLAN.md](plans/DEVELOPMENT_PLAN.md) | Implementation baseline, backlog and corrections. |
| [plans/MILA_ROADMAP.md](plans/MILA_ROADMAP.md) | Gap analysis and prioritized roadmap for Mila-scale inference-fleet and data management. |
| [plans/PRODUCTION_READINESS.md](plans/PRODUCTION_READINESS.md) | Prioritized roadmap from prototype to production: work items, acceptance criteria, status tracking. |
| [plans/MULTI_HOST_DEPLOYMENT.md](plans/MULTI_HOST_DEPLOYMENT.md) | Three-machine topology design (web/inference/data): placement, firewall matrices, secret distribution, setup prompts. |
| [status/TEST_READY.md](status/TEST_READY.md) | Current verification results, environment limits and host notes. |
| [status/AUDIT_CENSUS.md](status/AUDIT_CENSUS.md) | Event-by-event audit completeness census, canonical schema and open gaps. |
| [status/CONTROL_STORE.md](status/CONTROL_STORE.md) | PostgreSQL control store status: schema, durable quota ledger, key store, and migration. |
| [status/BENCHMARK_REPORT.md](status/BENCHMARK_REPORT.md) | M4 soak qualification and the later re-verification addendum. |

## Reading paths

- **Operator / sysadmin** — start with [INSTALL.md](INSTALL.md), then
  [operations.md](operations.md), [update.md](update.md), [multi-host.md](multi-host.md),
  [observability.md](observability.md), [tools.md](tools.md), then [security.md](security.md).
- **Reviewer / security** — [architecture.md](architecture.md),
  [security.md](security.md), [sovereignty.md](sovereignty.md), [testing.md](testing.md),
  [decisions/README.md](decisions/README.md).
- **Developer** — [development.md](development.md), [architecture.md](architecture.md),
  [services.md](services.md), [harness-integration.md](harness-integration.md),
  [model-management.md](model-management.md).
- **Auditor** — [security.md](security.md), [status/AUDIT_CENSUS.md](status/AUDIT_CENSUS.md),
  [backup-restore.md](backup-restore.md), [configuration.md](configuration.md),
  [status/CONTROL_STORE.md](status/CONTROL_STORE.md).
- **Agent / automated contributor** — [`../AGENTS.md`](../AGENTS.md) first, then
  [development.md](development.md) and [testing.md](testing.md).

## Documentation conventions

- Paths are relative to the repository root unless stated otherwise.
- Ports are loopback-only unless stated otherwise (the whole stack binds to
  `127.0.0.1`).
- "Fail closed" means the platform refuses the operation rather than proceeding
  without a required control.
- Evidence that has not been measured is described as *unverified* or
  *proposed*, never as verified.
