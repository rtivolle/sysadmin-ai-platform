# Runbook: GPU fleet (inference tier)

Enterprise tier-1 operations for the platform ↔ GPU split
(spec `ARCHITECTURE.md` §7–§8, §10–§11). Targets: **RPO ≤ 15 min**,
**RTO ≤ 2 h**.

Conventions: `platform` = the data+admin node, `gpu-0N` = inference nodes.
All mTLS material lives under `backend/config/keys/fleet/` (Git-ignored,
umask 077). Destructive steps are dry-run-first everywhere.

---

## 1. Add a GPU node

1. **Provision the OS + drivers** (another machine, same checkout):
   ```bash
   ./install.sh --role inference --unattended
   ```
   (TUI will ask only for the platform address.)
2. **Issue the node identity** (on the **platform** node):
   ```bash
   ./backend/services/resilience/fleet-ca.sh init-ca        # once ever
   ./backend/services/resilience/fleet-ca.sh issue-node gpu-02
   ```
   Idempotent: re-running `issue-node` reuses the existing cert unless `--force`.
3. **Deliver** `fleet/ca.crt`, `fleet/certs/gpu-02.crt`, `fleet/private/gpu-02.key`
   (0600) to the new node — never by git, never by email. Secure channel only
   (SSH `scp` from the platform node is fine).
4. **Start the node**: `./platform.sh start` → node-agent auto-registers → status
   `pending` in the fleet admin view.
5. **Approve** the node (human, UI or `POST /fleet/nodes/gpu-02/approve`): the
   scheduler computes the desired-state.
6. **Converge**: the node-agent pulls weights (HF direct), starts engines, reports
   `ready` on the next heartbeat.
7. **Go live**: `litellm_sync` regenerates the `model_list` → LiteLLM reload →
   smoke test:
   ```bash
   curl -s http://127.0.0.1:4000/v1/chat/completions -H "Authorization: Bearer $KEY" ...
   ```
8. Confirm the fleet alerts (`fleet_node_down`, `fleet_node_stale`) are green.

## 2. Drain a node (maintenance / update / removal)

```bash
curl -sk --cert fleet/certs/<admin>.crt --key fleet/private/<admin>.key \
  -X POST https://<platform>:3080/fleet/nodes/gpu-02/drain
```

Effect: the node leaves the `model_list` (no **new** traffic); `inference_engine`
finishes in-flight requests (grace period 120 s, configurable); node-agent
confirms `drained`. Poll until confirmed:
```bash
curl -sk ... https://<platform>:3080/fleet/nodes/gpu-02 | jq .status
```
A `drained` node that becomes healthy is re-integrated automatically on the next
healthy heartbeat — no manual `uncordon`.

`update.sh` on an `inference` node drains it automatically before patching
(§7 below); the manual command above is for planned maintenance.

## 3. Decommission a node

1. Drain it (§2) and wait for `drained`.
2. Decommission (removes it from the registry and the `model_list`):
   ```bash
   curl -sk --cert ... --key ... -X POST https://<platform>:3080/fleet/nodes/gpu-02/decommission
   ```
3. **Revoke its certificate** (on the platform node):
   ```bash
   ./backend/services/resilience/fleet-ca.sh revoke-node gpu-02
   ```
   This regenerates `fleet/ca.crl`. Distribute the new CRL to the remaining
   nodes (they must reject the revoked cert).
4. Power the node off. Its weights are re-downloadable; nothing durable is lost.

## 4. Rotate a node certificate {#cert-rotation}

Triggered by `fleet_cert_expiring` (< 30 days) or on demand:

```bash
# on the platform node
./backend/services/resilience/fleet-ca.sh issue-node gpu-02 --force
# deliver the new certs/gpu-02.crt (+ ca.crt if renewed) to the node (0600 for the key)
# on the node: restart node-agent and inference_engine so they pick up the new cert
./platform.sh service node_agent restart   # service name when implemented
./platform.sh service inference restart
# verify
./backend/services/resilience/fleet-ca.sh list
```

Rotation is per-node and needs no fleet-wide outage. Keep the old key until the
node has confirmed healthy heartbeats with the new cert.

**CA renewal** (every ~10 years): generate a new CA, re-issue all certs,
distribute `ca.crt` fleet-wide during a maintenance window. Documented here so
it is not discovered the week it expires.

## 5. Postgres PITR restore {#pitr-restore}

Prerequisites: `pg_basebackup`/`psql` present, PITR destination configured
(`SYSADMIN_PG_PITR_DEST`, default `backend/data/pg_pitr/`), WAL archiving active
(`wal-archive.conf` included from `postgresql.conf` — see `wal-snippet`).

```bash
# 0. Inspect what is available (never destructive)
python3 -m backend.services.resilience.pg_pitr_backup verify
# 1. Dry-run the plan first (default)
python3 -m backend.services.resilience.pg_pitr_backup restore --target-time "2026-09-30 09:45"
# 2. Execute only with explicit confirmation
python3 -m backend.services.resilience.pg_pitr_backup restore --target-time "2026-09-30 09:45" --yes
# 3. Follow the printed plan: stop postgres, the mechanical steps are staged in
#    backend/data/postgres_restored/, start postgres on it, wait for
#    'recovery complete', smoke-test the app, then promote / swap.
```

Target-time formats: `now`, ISO 8601, `YYYY-MM-DD HH:MM:SS` (UTC),
`<N>s|m|h|d ago` (e.g. `30m ago`). The restore stages into a **new** data dir
and refuses to overwrite an existing one — the original cluster is untouched
until you swap it (rollback plan built in).

## 6. Semi-annual DR drill {#dr-drill}

(spec §8.2, PR-B4) — every 6 months, on a scratch host or VM, never on prod:

1. `verify` the newest base backup (file checks + ephemeral restore).
2. Full `restore --target-time now` from the off-host PITR destination.
3. Bring up `platform.sh` on the restored data, run `make test-live`.
4. Record RTO measured vs the 2 h target in `docs/status/TEST_READY.md`.
5. File the drill report; fix whatever was not actually restorable — an
   untested restore is not a restore.

## 7. Ordered patching (update.sh) {#patching}

`update.sh` is role-aware via `backend/config/roles/deployment.env`:

- **role `inference`**: drains the node before touching code (tries the local
  node-agent `:8001/drain`, then the platform fleet API when `PLATFORM_URL`
  and the node client cert are available; warns and continues with a direct
  service stop when neither is reachable), applies the update, restarts the
  services, then waits for the node-agent health check. The platform
  re-integrates the node automatically on the next healthy heartbeat.
- **role `platform`** (or `all`): prints a pre-update backup reminder and
  checks the newest platform backup age — warns loudly when older than 26 h —
  then proceeds (the operator already confirmed at the prompt / passed `--yes`).
- GPU nodes are always patched **before** the platform node; never the reverse
  in one unattended run.

Operational rule: patch GPU nodes one at a time, verify each is back in the
`model_list` before moving to the next. Platform patching happens in an
announced maintenance window with a fresh backup (see §5).

## 8. Alerting

Fleet rules live in `backend/config/observability/alerts-fleet.yml`, same schema
as `backend/config/observability/alerts.json` (see the header comment for the
one-liner that merges them into the file `alerts.py` loads).

The collector must emit these per-node metrics (loopback probes on platform
aggregate the fleet registry + per-node scrapes; the hard-coded `127.0.0.1`
probes in `collector.py` are the known gap, spec P1.11):

| Metric | Source |
|---|---|
| `observability_fleet_node_up{node}` | platform probe of node `:8001/healthz` (1/0) |
| `observability_fleet_node_heartbeat_age_seconds{node}` | fleet registry `last_heartbeat` |
| `observability_fleet_model_healthy{node,model}` | node `/healthz` per-model ping |
| `observability_fleet_outbox_lag_seconds{node}` | oldest un-replayed outbox event per node |
| `observability_fleet_vram_used_percent{node,gpu}` | node `nvidia-smi` scrape |
| `observability_fleet_cert_expires_in_days{node}` | platform scans `fleet/certs/*.crt` |

Alert → runbook mapping: `fleet_node_down` → § node-down (check power, network,
`platform.sh logs node_agent` on the node; if dead > 5 min, the capacity is
simply gone — the platform is unaffected); `fleet_audit_outbox_lag` → check
`:9428` reachability from the node, then the outbox replay worker;
`fleet_vram_high` → reduce `max_num_seqs` / `gpu_memory_utilization` for that
node's desired-state or shed a replica via the scheduler.
