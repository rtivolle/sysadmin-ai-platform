# Project documentation

This directory documents the **implemented** on-premises sysadmin AI platform that
lives in this repository. It complements — and does not replace — the source
requirement specifications at the repository root:

- `README.md` — short orientation and quick start.
- `DEVELOPMENT_PLAN.md` — the proposed implementation baseline and backlog.
- `TEST_READY.md` — the latest recorded verification results and limits.
- `00 - Plan Directeur …` … `05 - Services Stockage & Audit …` — the six French
  design specifications (also delivered as `.docx`). These are the authoritative
  statement of intent; this documentation describes what the code *actually does*.

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
| [services.md](services.md) | The Python backend services: agent runtime, tools, auth gateway, quota, approval gate, target adapter, resilience, inference. |
| [harness-integration.md](harness-integration.md) | The DeepSeek Harness package: multi-user gateway, per-user instance manager, backend-wiring plugin and profile. |
| [security.md](security.md) | Identity, sandbox, approval gate, audit, threat model and known gaps. |
| [http-api.md](http-api.md) | Every HTTP endpoint exposed by the platform. |
| [tools.md](tools.md) | Reference for the four bounded sysadmin tools. |
| [operations.md](operations.md) | Install, start, stop, CLI usage, logs, dashboards and troubleshooting. |
| [configuration.md](configuration.md) | Configuration files, environment variables, ports and paths. |
| [backup-restore.md](backup-restore.md) | Backup, clean-staging restore and the disaster-recovery drill. |
| [testing.md](testing.md) | Test tiers, how to run them, recorded results and environment limits. |
| [development.md](development.md) | Repository layout, conventions and extension points. |
| [glossary.md](glossary.md) | Terms and abbreviations used across the project. |

## Reading paths

- **Operator / sysadmin** — start with [operations.md](operations.md), then
  [tools.md](tools.md), then [security.md](security.md).
- **Reviewer / security** — [architecture.md](architecture.md),
  [security.md](security.md), [testing.md](testing.md).
- **Developer** — [development.md](development.md), [architecture.md](architecture.md),
  [services.md](services.md), [harness-integration.md](harness-integration.md).
- **Auditor** — [security.md](security.md), [backup-restore.md](backup-restore.md),
  [configuration.md](configuration.md).

## Documentation conventions

- Paths are relative to the repository root unless stated otherwise.
- Ports are loopback-only unless stated otherwise (the whole stack binds to
  `127.0.0.1`).
- "Fail closed" means the platform refuses the operation rather than proceeding
  without a required control.
- Evidence that has not been measured is described as *unverified* or
  *proposed*, never as verified.
