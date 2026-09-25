# Documentation index

This directory documents the implementation in this repository.

Primary entry points:

- [`../README.md`](../README.md) — project overview and quick start
- [`../AGENTS.md`](../AGENTS.md) — contributor/agent operating guide, guardrails, verification matrix
- [`status/TEST_READY.md`](status/TEST_READY.md) — latest measured verification evidence and limits

> **Status reminder:** this is a prototype and is not yet qualified for production target mutations.

## Document map

| File | Scope |
|---|---|
| `architecture.md` | System context, trust boundaries, and request flows |
| `services.md` | Service-by-service implementation reference |
| `security.md` | Identity, sandbox, approval/audit controls, threat framing |
| `http-api.md` | API endpoint reference |
| `tools.md` | Bounded tool behavior and command policy |
| `operations.md` | Installation, lifecycle, runtime ops, troubleshooting |
| `configuration.md` | Config files, env vars, ports, paths |
| `model-management.md` | Local model registration/download/runtime lifecycle |
| `backup-restore.md` | Backup, restore, disaster-recovery drill behavior |
| `testing.md` | Test tiers and how to execute/interpret checks |
| `development.md` | Repository layout and change guidance |
| `harness-integration.md` | DeepSeek Harness gateway/profile/plugin integration |
| `glossary.md` | Terms and abbreviations |
| `status/TEST_READY.md` | Verification record and host constraints |
| `status/AUDIT_CENSUS.md` | Audited paths/events and known coverage gaps |
| `status/BENCHMARK_REPORT.md` | Benchmark and qualification reporting |
| `plans/DEVELOPMENT_PLAN.md` | Implementation baseline/backlog |
| `plans/PRODUCTION_READINESS.md` | Production-readiness work decomposition |
| `plans/MULTI_HOST_DEPLOYMENT.md` | Multi-host topology design |
| `specs/README.md` | Source specification bundle and traceability |

## Suggested reading paths

- **Operator**: `operations.md` → `tools.md` → `security.md`
- **Developer**: `development.md` → `architecture.md` → `services.md`
- **Security reviewer**: `architecture.md` → `security.md` → `status/AUDIT_CENSUS.md`
- **Release/qualification**: `testing.md` → `status/TEST_READY.md` → `plans/PRODUCTION_READINESS.md`

## Documentation conventions

- Claims should reflect measured evidence; otherwise mark them as unverified.
- Prefer stable behavior descriptions over volatile run counts in static docs.
- Keep loopback ports/paths aligned with `backend/platform.sh` and service configs.
