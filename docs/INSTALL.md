# Installation

This guide covers installing the platform on one machine or on a fleet split:
one **platform** host (data + admin tier) plus N **inference** GPU nodes. The
installer is idempotent-ish and fail-closed: a failed pre-flight check aborts
**before** anything is written. The platform is a **prototype**; the install
process is tested on the development host, and the multi-node topologies have
never been run on real machines (see [multi-host.md](multi-host.md) §8 and the
checklist there).

## 1. Prerequisites

### Operating system

- **Ubuntu 22.04/24.04 LTS** (recommended) or **Debian**. The installer uses
  `apt` on Ubuntu/Debian and `apk` on Alpine.
- The **NVIDIA driver helper is Ubuntu-only**: automatic installation refuses
  other distributions. On other distros, run `./install.sh --nvidia` as a
  read-only plan and install the drivers manually.
- GPU nodes need an Ubuntu host with NVIDIA PCI hardware.

### Software

- **Python 3.12** — the platform venv is created by `install.sh`; the vLLM venv
  is isolated at `backend/.vllm-venv` (Python 3.12, created by the installer).
- **Build tools**: `gcc`, `make`, kernel headers + DKMS (required for NVIDIA
  drivers), `git`, `curl`.
- **Bubblewrap** (`bwrap`) and **timeout** — required for the sandbox. The
  sandbox also needs a **delegated writable cgroups v2** subtree for the
  service account; without it execution fails closed (exit `126`). Install-only
  hosts that never run the agent (data tier, GPU nodes) do not need this.
- **NVIDIA drivers** (inference role only) — installed by `install.sh --nvidia`
  (see §6) or pre-installed by the operator. Verify with `nvidia-smi`.
- **Node 22+** — only if you use the DeepSeek Harness package
  (`packages/harness-integration/`); no build step.

### Network

No special ports on a single host (everything binds `127.0.0.1`). For a fleet
split, these LAN ports must be reachable (firewall rulesets ship in
`backend/config/firewall/` and are **not** applied automatically — review the
`define` block, then `nft -f`):

| Port | Direction | Purpose |
|---|---|---|
| `8443` (+`8080` redirect) | user networks → platform | Traefik TLS front door |
| `3080` | fleet LAN → platform | agent platform + fleet API (`agent_tools`) |
| `9428` | fleet LAN → platform | VictoriaLogs (audit outbox replay) |
| `8000` | platform → GPU nodes | inference traffic (LiteLLM → engines) |
| `8001` | platform → GPU nodes | node-agent control (desired state, drain, mTLS) |

`4000` (LiteLLM), `6379` (Valkey), `8333/8888/9333` (SeaweedFS) stay
loopback-only on the platform host.

> **Phase A note.** `agent_tools` (including the fleet API on `:3080`)
> currently binds loopback even on the `platform` role; fleet admin commands
> (approve / drain / decommission) run **from the platform host itself**
> against `127.0.0.1:3080`, or through the Traefik front door `:8443`.
> GPU nodes are not called on `:3080` — the platform **polls** their
> node-agent (`:8001`, mTLS) instead. The nft rulesets above are the
> documented intent; the code catches up as the fleet work lands.

**Port 3080 must be free** on the platform host. It is also the DeepSeek
Harness web default, so a host running the harness UI there cannot start the
agent platform.

## 2. Roles

`install.sh --role` selects what a host runs. The fleet split uses the
**platform/inference** pair:

| Role | Purpose | Flag example |
|---|---|---|
| `all` | One machine: every service, all binds loopback (default) | `./install.sh` |
| `platform` | Data + admin tier: state, agent platform, LiteLLM, fleet control. Needs `--lan-bind-ip` | `./install.sh --role platform --lan-bind-ip 10.0.0.20` |
| `inference` | GPU node: inference engine + node-agent, **secretless**. Needs `--lan-bind-ip` and `--platform-url` | `./install.sh --role inference --lan-bind-ip 10.0.0.21 --platform-url https://10.0.0.20:3080` |
| `web`, `data` | Legacy three-machine split (PR-H1): application tier / state tier. Still supported for compatibility; for new installs prefer `platform`. | `./install.sh --role web --lan-bind-ip <W-IP> --peer-inference <I-IP> --peer-data <D-IP>` |

The chosen role is recorded in `backend/config/roles/deployment.env`
(Git-ignored). Omitting `--role` reuses the recorded role, so `./update.sh`
never re-roles a machine. `--role all` switches a split host back to
all-in-one. Preview any role plan without touching the machine with
`--dry-run`.

## 3. What each role installs

| Role | Services started by `platform.sh` | Installed binaries | Secrets provisioned |
|---|---|---|---|
| `all` | Traefik, ForwardAuth, agent platform, approval gate, target adapter, sandbox, LiteLLM (:4000), inference engine (:8000), Valkey, VictoriaLogs, SeaweedFS, model servers | all of the above | yes (random local credentials in `backend/config/keys/`) |
| `platform` | Valkey, VictoriaLogs, audit outbox, SeaweedFS, auth gateway, agent platform (:3080 + fleet API), LiteLLM (loopback :4000), `litellm_sync` daemon, Traefik, harness gateway | state binaries (Valkey/VictoriaLogs/SeaweedFS) | yes |
| `inference` | inference engine (:8000, LAN), audit outbox (replays to platform), node-agent (:8001) | none extra; vLLM in `backend/.vllm-venv` | **no** — the GPU node is secretless by design. Fleet identity is a certificate issued by the fleet CA (`backend/services/resilience/fleet-ca.sh issue-node <node-name>`) during install |
| `web` / `data` (legacy) | `web`: Traefik, ForwardAuth, agent platform, approval gate, target adapter, sandbox; `data`: Valkey, VictoriaLogs, SeaweedFS | `web`: Traefik; `data`: Valkey/VictoriaLogs/SeaweedFS | `web`: yes; `data`: no (needs the Valkey password copied) |

`./platform.sh roles` shows which services each role starts on the current
host; `./platform.sh status` reports this role's services plus the peers it
reaches.

## 4. Single-node install (copy-paste)

```bash
git clone <this-repo> && cd sysadmin-ai-platform

# Optional: preview the plan (writes nothing, installs nothing)
./install.sh --dry-run

# 1. Install dependencies, native binaries and random local credentials
./install.sh

# 2. Start the platform services
./platform.sh start
./platform.sh status

# 3. Chat with the agent CLI
./sysadmin-chat
```

The CLI defaults to `SYSADMIN_USER=sysadmin-01`. Administrator commands use
`SYSADMIN_USER=sysadmin-admin` and the master key in
`backend/config/keys/master.key`. Stop everything with `./platform.sh stop`.

Run `./install.sh --tui` for the interactive configuration wizard, or
`./install.sh --survey` for hardware inventory only. For repeatable vLLM
installs pin the version: `./install.sh --vllm-version <VERSION>`
(`--skip-vllm` for remote inference or dependency-only setup).

## 5. Multi-node install — platform first, then GPU nodes

Bring up the tiers in dependency order so every client starts against a
reachable store. Apply the firewall rulesets
(`backend/config/firewall/platform.nft`, `backend/config/firewall/inference.nft`)
**before** the tiers are reachable from anything else.

### 5a. Platform host (first)

```bash
./install.sh --role platform --lan-bind-ip 10.0.0.20 \
    --peer-inference-hosts 10.0.0.21,10.0.0.22   # optional bootstrap list;
                                                # the fleet registry is the source of truth once nodes register
```

`--peer-inference-hosts` is the bootstrap list the platform polls; adding or
removing a GPU node later requires **no platform-side config edit** — nodes
self-register at boot and appear as `pending` until a human approves them.

**PostgreSQL control store (required for the fleet control loop).** The
`litellm_sync` daemon (schedule → push desired state → regenerate LiteLLM
config) stays inactive without it. The installer never provisions a database;
do it explicitly with `backend/config/postgres/postgres.sh` (loopback-only
cluster on `:5433`; the script never installs packages — missing binaries exit
`3` with the exact install command):

```bash
backend/config/postgres/postgres.sh check       # are the binaries present?
backend/config/postgres/postgres.sh provision   # initdb + role + database; password to 0600
backend/config/postgres/postgres.sh start
# Set SYSADMIN_DATABASE_URL to the local-cluster DSN when starting the
# platform (see docs/status/CONTROL_STORE.md for the DSN recipe), then:
./platform.sh start
./platform.sh status   # valkey, victorialogs, agent platform, litellm, traefik…
```

Without `SYSADMIN_DATABASE_URL`, `litellm_sync` prints
`staying inactive` and the platform serves only what is statically
configured — GPU nodes poll in, but no desired state is pushed.

### 5b. Each GPU node (after the platform is up)

```bash
# 1. Drivers first on Ubuntu (see §6 for details), then:
./install.sh --role inference --lan-bind-ip 10.0.0.21 \
    --platform-url https://10.0.0.20:3080 \
    --node-name gpu-01
```

The installer TCP-checks the platform before applying; an unreachable
platform aborts with nothing applied. `--peer-data` is accepted but defaults
to the platform URL's host.

The node self-registers at boot as **pending**. A human must approve it before
it can serve traffic. Run the fleet admin commands **on the platform host**
(`agent_tools` binds loopback in Phase A; see §1):

```bash
KEY=$(cat backend/config/keys/sysadmin-admin.key)
curl -s -X POST -H "Authorization: Bearer $KEY" \
  http://127.0.0.1:3080/api/v1/fleet/nodes/gpu-01/approve
```

Node lifecycle: `pending` → `approved` → `active` → `stale` (3 missed
heartbeats, auto-excluded from LiteLLM routing) → `drained` → `retired`.
Stale nodes return to `active` automatically when they heartbeat again.

**Removing a node** — no platform config edit, ever (run on the platform host):

```bash
# 1. Drain: the node refuses new models and stops running ones
curl -s -X POST -H "Authorization: Bearer $KEY" \
  http://127.0.0.1:3080/api/v1/fleet/nodes/gpu-01/drain
# 2. Decommission: status retired; revoke its fleet certificate
curl -s -X POST -H "Authorization: Bearer $KEY" \
  http://127.0.0.1:3080/api/v1/fleet/nodes/gpu-01/decommission
# 3. Power the machine off
```

For the legacy three-machine split (`web`/`inference`/`data`) and the
key-copy runbook it requires, see [multi-host.md](multi-host.md) §§1–8.

## 6. GPU / NVIDIA setup

The driver helper supports **Ubuntu hosts with NVIDIA PCI hardware**; automatic
installation refuses other distributions, but `./install.sh --nvidia` (no
`--apply`) is a safe read-only plan on any host.

```bash
# 1. Read-only plan, including detection of cards with no working driver
./install.sh --nvidia

# 2. Explicit opt-in: install through the host's signed APT repositories
./install.sh --nvidia --apply

# 3. Optional branch override (choose one listed by ubuntu-drivers list --gpgpu)
./install.sh --nvidia --apply --driver 570-server
```

On a GPU node the helper extends the role install:

```bash
./install.sh --role inference --lan-bind-ip 10.0.0.21 --nvidia
```

Then: **reboot** after driver changes (complete MOK enrollment if Secure Boot
requests it — the helper does not disable Secure Boot and does not reboot),
and verify with `nvidia-smi`. A successful driver install does not prove a
model will fit or start; a PyTorch CUDA probe must pass before vLLM install
reports success.

Optional CUDA toolkit (only for custom compilation; prebuilt wheels supply
their CUDA userspace already):

```bash
./install.sh --nvidia --apply --cuda-toolkit 12-8   # example version
```

No repository is added automatically — configure NVIDIA's signed APT repository
first. See [nvidia-vllm.md](nvidia-vllm.md) for the full vLLM serving
configuration (`backend/config/vllm/serve.yaml`), `CUDA_VISIBLE_DEVICES`
selection, and GPU qualification notes.

## 7. Post-install verification

On the installed host(s):

```bash
./platform.sh status    # every service this role runs: pid, port, health
```

Targeted checks:

```bash
# Authenticated API smoke test (single node or platform host)
KEY=$(cat backend/config/keys/sysadmin-01.key)
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8080/api/tools/list

# Fleet view (platform host): registered nodes and health summary
ADMIN=$(cat backend/config/keys/sysadmin-admin.key)
curl -s -H "Authorization: Bearer $ADMIN" http://127.0.0.1:3080/api/v1/fleet/nodes
curl -s -H "Authorization: Bearer $ADMIN" http://127.0.0.1:3080/api/v1/fleet/health

# First model on the fleet (admin model lifecycle, platform host)
# register → download → start → wait for healthy → inference
# see docs/model-management.md and docs/gpu-fleet.md ("Adding a node")
```

Then `./sysadmin-chat` for a first agent conversation. Deeper verification
(`make test`, `make test-live`, `make benchmark`, the tiered pytest suites) is
documented in [testing.md](testing.md); the recorded results and host notes
live in [status/TEST_READY.md](status/TEST_READY.md).

## 8. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `platform.sh start` fails: `address already in use` on **3080** | port occupied (often the DeepSeek Harness web UI) | Free the port or move the other service; 3080 must belong to the agent platform. See the host note in [status/TEST_READY.md](status/TEST_READY.md). |
| Sandbox exits `126` / "no delegated cgroup" | cgroups v2 not delegated, or `bwrap`/`timeout` missing | Delegate a writable cgroups v2 subtree to the service account; install `bubblewrap`. The runner fails closed by design — re-run Tier 2 tests to confirm. |
| `install.sh --nvidia --apply` refuses on this host | not Ubuntu, or no NVIDIA PCI hardware | Use the read-only plan (`./install.sh --nvidia`) as reference and install drivers manually; then `nvidia-smi` must succeed. |
| `nvidia-smi` works but vLLM install fails the CUDA probe | driver/runtime mismatch (Secure Boot MOK, stale kernel module) | Reboot and complete MOK enrollment; re-run `nvidia-smi`; check `backend/logs/` for the probe error. |
| `./install.sh --role inference` aborts before applying | `--platform-url` unreachable | Bring the platform host up first (§5a); the installer TCP-checks the platform and fails closed with nothing applied. Verify the URL scheme, host and port. |
| `401` from CLI/API | wrong user key | Check `backend/config/keys/<user>.key` and `SYSADMIN_USER` (default `sysadmin-01`; admin `sysadmin-admin` + `backend/config/keys/master.key`). |
| `429` from chat | quota (concurrency/RPM/daily budget) | Wait, or use an approved P1 elevation for genuine incidents. |
| `503` "Shared quota state unavailable" | Valkey down or unreachable | Check `valkey.log`; the platform fails closed. On a fleet node, check the LAN path to the platform's Valkey. |
| Agent returns `502` | LiteLLM / inference engine unavailable | Check `litellm.log` and `inference.log`; on the platform host verify the GPU nodes are `active` in the fleet health view (§7). |
| No audit events | VictoriaLogs down | Events queue in `backend/data/victorialogs/outbox.jsonl`; the outbox worker replays on recovery. |
| PostgreSQL control store refuses to start | binaries missing | `backend/config/postgres/postgres.sh` never installs packages: missing binaries exit `3` with the exact install command. The store is optional (`SYSADMIN_CONTROL_STORE=postgres`); Valkey/file stores are the default. |

Logs: `backend/logs/{traefik,litellm,agent_tools,auth_gateway,inference,seaweedfs,valkey,victorialogs,audit_outbox}.log`, per-model vLLM logs in
`backend/logs/models/<model>.log`, pending audit replay in
`backend/data/victorialogs/outbox.jsonl`.

## 9. Runbooks

- [runbooks/](runbooks/) — alert remediation recipes (`service_down.md`,
  `quota_reject_spike.md`, `outbox_backlog.md`, `disk_usage.md`,
  `gpu_memory.md`, `stale_leases.md`, `backup_age.md`) and the multi-GPU
  Vast/H200 recipe ([runbooks/vast-deepseek.md](runbooks/vast-deepseek.md)).
- [operations.md](operations.md) — day-to-day operation: start/stop, CLI,
  dashboards, the full troubleshooting table.
- [multi-host.md](multi-host.md) — role internals, staged bring-up, key copy,
  firewall matrices, verification checklist.
- [gpu-fleet.md](gpu-fleet.md) — fleet control loop, node lifecycle, mTLS auth
  model, desired-state policies.
- [nvidia-vllm.md](nvidia-vllm.md) — driver/CUDA/vLLM details.
- [update.md](update.md) — self-update (`update.sh`) without re-roling hosts.
