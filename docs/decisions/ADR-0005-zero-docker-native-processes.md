# ADR-0005: Zero-Docker native-process topology instead of Docker Compose

- **Date:** 2026-09-24
- **Status:** Proposed — owner acceptance pending (de-facto baseline; AGENTS.md forbids Docker)
- **Source of divergence:** `docs/specs/02/03/04/05` (each ships a `docker-compose.*.yml`); `docs/plans/DEVELOPMENT_PLAN.md` §4 ("Use Docker Compose for shared services and systemd for per-user runtimes").

## Context

Every source spec deploys its services as Docker containers (`docker-compose.dynamo.yml`,
`docker-compose.gateway.yml`, `docker-compose.aux.yml`, plus a `dsh` systemd unit).
The plan §4 likewise says "Use Docker Compose for shared services".

The implementation is deliberately **zero-Docker**. `backend/platform.sh`
supervises native processes for Valkey (`valkey-server`), VictoriaLogs
(`victoria-logs-prod`), SeaweedFS (`weed server -s3`), LiteLLM, Traefik, the
auth gateway, the agent platform, the inference engine, and an audit outbox
worker, using PID files in `backend/run/` and logs in `backend/logs/`. All
binaries bind loopback. `AGENTS.md` states there is no Docker anywhere in the
runtime and forbids introducing containers. SeaweedFS and VictoriaLogs are kept
but run as native Go binaries rather than containers; Valkey replaces the
containerized Redis of spec 02.

## Decision

Run the whole stack as native processes supervised by `platform.sh`, on one
host, loopback-only, with no Docker or any other container runtime. Keep
SeaweedFS (S3/filer), VictoriaLogs (audit), and Valkey (shared state) as native
binaries.

## Consequences

- **Positive:** No daemon to secure (no `/var/run/docker.sock` mount, which the
  plan explicitly wanted to avoid); no image supply chain; simpler backup
  (filesystem + `BGSAVE`); lower per-service footprint.
- **Negative:** Operator convenience of Compose (declarative restart policies,
  healthchecks, network isolation) is hand-rolled in `platform.sh`; the plan's
  "systemd for per-user runtimes" is only partially realized — systemd-run is
  used inside the sandbox, while services are `nohup`-supervised shell children,
  not systemd units.

## Evidence

- **Live service bring-up is tested:** `backend/tests/test_platform.py::test_valkey`,
  `::test_victorialogs`, `::test_seaweedfs`, `::test_inference_engine`,
  `::test_auth_gateway`, `::test_agent_tools_and_security`, `::test_traefik_gateway`
  assert each native service is reachable on its loopback port when started via
  `platform.sh`.
- **Backup/restore covers the native data dirs:**
  `backend/tests/qualification/restore_drill.py` backs up/restores Valkey,
  VictoriaLogs, SeaweedFS and `config/keys` with byte-for-byte verification;
  `backend/tests/tier4_recovery/test_m3_dr_drill.py::test_backup_manager_creates_valid_archive_and_manifest`.
- **No Docker in the repo:** grep for `docker`/`compose` in `backend/` returns
  only runbook/fixture text describing host PostgreSQL, not platform Docker.

## Corrective work

- None strictly required (Docker is intentionally absent). Record the owner's
  acceptance that the platform will not use containers, so future work does not
  reintroduce them.
