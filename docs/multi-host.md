# Multi-host deployment (one machine or three)

Status: **implemented and unit-tested; never run on real machines.** The role
plumbing (`install.sh --role`, role-aware `platform.sh`, config rendering,
firewall rulesets, role-aware secret handling) is in place and covered by
`backend/tests/tier1_unit/test_multihost_roles.py` and
`test_multihost_render.py`, but the topology has not been brought up on three
hosts. The full design lives in
[plans/MULTI_HOST_DEPLOYMENT.md](plans/MULTI_HOST_DEPLOYMENT.md).

The same codebase runs either as **one machine** (the default, everything on
loopback) or as **three machines**:

| Machine | Role | Runs |
|---|---|---|
| **W — web delivery** | `web` | Traefik, ForwardAuth, agent platform (`agent_runtime` + `agent_tools`), approval gate, target adapter, sandbox, harness gateway, workspaces |
| **I — inference** | `inference` | LiteLLM proxy, `inference_engine`, vLLM (loopback) |
| **D — data** | `data` | Valkey, VictoriaLogs, SeaweedFS, backup/restore |

A single host is the `all` role: every service, everything loopback, exactly
today's behaviour. `all` is what you get with no recorded role.

## 1. Choosing the topology at install time

```bash
# One machine (default): every service, all binds loopback
./install.sh --role all

# Three machines — run one command per host, in this order (D -> I -> W)
./install.sh --role data      --lan-bind-ip <D-IP>
./install.sh --role inference --lan-bind-ip <I-IP> --peer-data <D-IP>
./install.sh --role web       --lan-bind-ip <W-IP> --peer-inference <I-IP> --peer-data <D-IP>
```

The interactive wizard asks the same question first:

```bash
./install.sh --tui                      # menu: all / web / inference / data
./install.sh --tui --role web --lan-bind-ip <W-IP> \
    --peer-inference <I-IP> --peer-data <D-IP>   # same answers, no prompts
```

Three rules the installer enforces (it exits `2` before changing anything):

- a role other than `all` needs a real `--lan-bind-ip`; a split host bound to
  loopback cannot be reached by its peers;
- `web` needs `--peer-inference` and `--peer-data`; `inference` needs
  `--peer-data`;
- before anything is written, the installer TCP-checks every peer port the role
  depends on. A failure aborts with **nothing applied** — no
  `deployment.env`, no rendered config, no keys.

Preview a plan without touching the machine:

```bash
./install.sh --dry-run --role web --lan-bind-ip <W-IP> \
    --peer-inference <I-IP> --peer-data <D-IP>
# prints the resolved role, addresses, the services each role runs, and the
# exact non-dry-run command; writes nothing, installs nothing, starts nothing
```

### The choice is recorded, and reversible

`install.sh` writes `backend/config/roles/deployment.env` (Git-ignored) for
**every** role, including `all`. That file is the only thing that makes a host
stop being single-host.

- **Omitting `--role` reuses the recorded role.** This is what makes
  `./update.sh` safe: it calls `./install.sh` with no flags, so a self-update
  never re-roles a machine, and it re-renders the role-dependent files (a git
  update restores the checked-in single-host `traefik/dynamic.yml` and
  `valkey/valkey.conf`, which the role render then corrects).
- **`--role all` switches a split host back to one machine.** Peers are reset
  to loopback in the record, and the renderer restores the checked-in
  `dynamic.yml` / `valkey.conf` byte-for-byte. Leaving a host on
  `--role web` after the other two machines are gone is therefore not a
  one-way door.
- The renderer also owns the non-loopback entries it wrote: changing
  `--lan-bind-ip` replaces the previous address instead of accumulating binds.

## 2. Prerequisite — no containers, no Docker

The runtime is the same zero-Docker stack as single-host. Nothing here adds
containers.

## 3. Staged bring-up (D → I → W)

Bring the tiers up in dependency order so every client starts against a
reachable store and the front door never proxies to a half-started tier.

1. **D (data)** — install then start Valkey, VictoriaLogs, SeaweedFS:
   ```bash
   ./install.sh --role data --lan-bind-ip <D-LAN-IP>
   ./platform.sh status   # valkey :6379, victorialogs :9428, seaweedfs :8333
   ```
   Data reaches no peers, so there is no pre-apply connectivity check.

2. **I (inference)** — install then start LiteLLM + inference engine:
   ```bash
   ./install.sh --role inference --lan-bind-ip <I-LAN-IP> --peer-data <D-LAN-IP>
   ./platform.sh status   # litellm :4000, inference :8000
   ```
   The installer TCP-checks D:6379 and D:9428 and fails closed (no partial
   apply) if D is unreachable. **Copy the key files from W before starting** —
   LiteLLM's in-process auth reads them (see §4).

3. **W (web)** — install then start the application tier:
   ```bash
   ./install.sh --role web --lan-bind-ip <W-LAN-IP> \
       --peer-inference <I-LAN-IP> --peer-data <D-LAN-IP>
   ./platform.sh status   # traefik, auth_gateway, agent_tools + peers
   ```
   The installer TCP-checks I:4000 and D:6379/8333/9428 before applying.

Apply the firewall rulesets (§6) before any tier is reachable from anything
else, and start the tiers in order after the rules are in place.

```bash
./platform.sh roles    # which services each role starts (marks this host's role)
./platform.sh status   # this role's services, plus the peers it reaches
```

## 4. Key-copy and revocation (W **and** I)

LiteLLM's authentication is **in-process** (`custom_auth` imports
`backend/services/auth_gateway/litellm_auth.py`), not an HTTP callback. So the
I host carries the full per-user key set and the quota/audit code, and needs
network access to D.

Provision keys on W only (`install.sh` role `all` or `web`), then copy exactly
these files over a **secure operator channel** (`scp`/`rsync` over SSH, verify
`0600`/`0700` permissions after copy):

| Key file | W | I | D |
|---|:-:|:-:|:-:|
| `master.key` | ✓ | ✓ | — |
| `sysadmin-*.key`, `emergency-p1.key` | ✓ | ✓ | — |
| `login-credentials.json`, `initial-passwords.txt` | ✓ | — | — |
| `valkey-password.key` | ✓ | ✓ | ✓ |

`install.sh --role inference|data` does **not** provision keys; it prints the
copy list and stops there. `platform.sh` loads only the secrets the role needs:
a `data` host starts with `valkey-password.key` alone (it has no LiteLLM key by
design), while `web` and `inference` fail closed without `master.key`.

**Revocation is two-step.** Rotating or revoking a key on W alone is
insufficient while I holds a copy: re-provision on W **and** re-copy to I (and
re-copy `valkey-password.key` to D when that password rotates). Until a shared
key store exists this is a manual runbook step.

## 5. What the role changes (config + env)

`deployment.env` (`backend/config/roles/deployment.env`, Git-ignored) is the
only thing that makes a host stop being `all`:

- `platform.sh` starts/stops/reports **only this role's services** and forces
  loopback binds for `all`. Otherwise it binds `LAN_BIND_IP` for VictoriaLogs,
  SeaweedFS and LiteLLM.
- It exports the peer addresses as `VALKEY_URL`, `VALKEY_HOST`, `VALKEY_PORT`,
  `VICTORIALOGS_URL`, `LITELLM_URL` and `SYSADMIN_LITELLM_URL`, plus
  `SYSADMIN_VALKEY_HOST/PORT`, `SYSADMIN_SEAWEEDFS_HOST` and
  `SYSADMIN_INFERENCE_LOCAL` for the harness admin console.
  `VALKEY_HOST`/`VALKEY_PORT` matter as much as `VALKEY_URL`: ForwardAuth builds
  its P1 elevation client from them, not from the URL.
- `backend/config/roles/render_config.py` rewrites the role-dependent files:
  `valkey/valkey.conf` (Valkey `bind` gains the LAN address on `data`, and loses
  it again on `all`) and `traefik/dynamic.yml` (LiteLLM / SeaweedFS /
  VictoriaLogs upstreams point at peers on `web`, and revert to loopback on
  `all`). Upstreams are matched by service name, so an operator-chosen port is
  still rewritten correctly. For `all` the renderer reproduces the checked-in
  files byte-for-byte.
- `install.sh` accepts `--role` / `--peer-*` / `--lan-bind-ip` / `--dry-run`,
  installs only the role's binaries (W: Traefik; D: Valkey/VictoriaLogs/
  SeaweedFS; I: none) and runs the pre-apply connectivity check via
  `backend/config/roles/connectivity_check.py`.
- The TUI (`install.sh --tui`) asks the same role/peer questions first and then
  only the questions that role needs: the inference questions on
  `all`/`inference`, the workspace and quota questions on `all`/`web`, and each
  port question only on the roles that serve it.
- `./update.sh` checks the secrets **this** role holds after an update, so a
  data host (no `master.key`) is not failed by a single-host postflight.

Interactively:
```bash
./install.sh --tui
```

## 6. Firewall rulesets

`backend/config/firewall/{web,inference,data}.nft` ship the §4 matrix from the
design doc. They are **not applied automatically** — review the `define` block
(interface name + peer IPs), then load explicitly, e.g. `nft -f
backend/config/firewall/web.nft`.

| Host | Inbound | Outbound |
|---|---|---|
| W | 8443 (+8080 redirect) from user networks | I:4000, D:6379/8333/9428 |
| I | 4000 from W only | D:6379/9428 |
| D | 6379 from W+I; 8333/8888/9333/9428 from W only | (backup export, site policy) |

Only W is reachable from user networks. SSH management is out of scope and
follows site policy.

## 7. Verification checklist

Run on real machines before considering this qualified:

- [ ] D up: `valkey`, `victorialogs`, `seaweedfs` reachable on LAN IPs, and
      `platform.sh status` on D shows its three services without `master.key`.
- [ ] I up: `litellm` answers on I:4000; quota/audit reach D (no 503 when
      Valkey is up).
- [ ] W up: Traefik front door answers on W:8443; login + agent flow end-to-end;
      a P1 elevation succeeds (it needs W's `VALKEY_HOST` pointing at D).
- [ ] `./platform.sh test` (or the full `make test`) with `VALKEY_URL` /
      `VICTORIALOGS_URL` pointed at D passes on W.
- [ ] The harness admin console on W reports Valkey/SeaweedFS up (peer hosts)
      and does not report the remote inference engine as down.
- [ ] Firewall: from a user network, only W:8443 answers; W:4000/6379/9428
      and I/D ports are refused.
- [ ] Key revocation tested on **both** W and I (two-step, see §4).
- [ ] Backup/restore runs on D and covers Valkey/VictoriaLogs/SeaweedFS state.
- [ ] `./install.sh --role all` on one host returns it to a working
      single-host install (peers reset, configs reverted).

Record results in [status/TEST_READY.md](status/TEST_READY.md).

## 8. Honest limitations

- **Not verified on real machines.** The topology has never been run; §7 is
  the acceptance gate. What *is* measured is the unit-level behaviour: role
  validation, peer env exports, role-scoped secret loading, reversible
  rendering and the fail-closed install paths
  (`test_multihost_roles.py`, `test_multihost_render.py`).
- **Plaintext inter-machine links.** Bearer tokens and the Valkey password
  cross an isolated, default-deny private segment **unencrypted**. TLS on these
  hops is hardening item PR-H2 and must land before the machines share any
  non-isolated network.
- **Secrets exist on two machines** (W and I both hold user keys); revocation
  is two-step until a shared key store is designed.
- **Single points of failure everywhere** — one instance of each service; HA is
  a deferred non-goal.
- **Workspaces live on W** (server-assigned 0700, ephemeral); they do not move
  to D.
- The port-3080 host note in [status/TEST_READY.md](status/TEST_READY.md) still
  applies on W.
- The admin console's `inferenceMode` label still probes the inference engine
  on loopback and therefore shows `unknown` on a web host; the *services*
  panel (which is what the checklist covers) is peer-aware.

## 9. GPU-fleet topology (roles `platform` / `inference`, Phase B)

The fleet split runs the state + admin tier on one **platform** host and the
GPU tier on N **inference** (GPU) nodes. It replaces the PR-H1 three-machine
split (W/I/D, which stays supported for compatibility) when the GPU tier must
be added to and removed from without friction.

Roles:

| Host | Services | Notes |
|---|---|---|
| P (platform) | valkey, victorialogs, audit_outbox, seaweedfs, auth_gateway, agent_tools, litellm (loopback :4000), litellm_sync, traefik, harness_gateway | data+admin tier; LiteLLM is served here, so it binds loopback |
| GPU node (inference) | inference engine (:8000, LAN), audit_outbox, node_agent (:8001) | **secretless**: no master.key, no valkey-password.key |

Install order: platform first, then each GPU node:

```bash
# Platform host
./install.sh --role platform --lan-bind-ip 10.0.0.20 \
    --peer-inference-hosts 10.0.0.21,10.0.0.22

# Each GPU node (driver install is a separate explicit step)
./install.sh --role inference --lan-bind-ip 10.0.0.21 \
    --platform-url https://10.0.0.20:3080 --node-name gpu-01
./install.sh --role inference --lan-bind-ip 10.0.0.21 --nvidia
```

Key facts:

- A GPU node needs only `--platform-url`: it registers and heartbeats to the
  platform's fleet API (`:3080`), and its audit outbox replays to the
  platform's VictoriaLogs (`:9428`). `--peer-data` is accepted but defaults to
  the platform URL's host.
- `--peer-inference-hosts` on the platform is a **bootstrap** list only; the
  fleet registry (fleet worker) is the source of truth once nodes register.
- Secrets are provisioned on `all`/`web`/`platform` only; the GPU node never
  holds keys. Its fleet identity (`NODE_NAME`) is issued by the CA
  (`backend/services/resilience/fleet-ca.sh issue-node <node-name>`), run
  during install when the script is present.
- Firewall: `backend/config/firewall/platform.nft` (default-deny: user LAN may
  reach only :8443/:8080; the fleet LAN may reach :3080/:9428) and
  `backend/config/firewall/inference.nft` (only the platform may reach
  :8000/:8001; the node reaches out to the platform on :3080/:9428). Not
  applied automatically — load explicitly with `nft -f`.
- Flow matrix: LiteLLM on P calls `http://<gpu-node>:8000` per the desired
  state; node_agent on the GPU node calls `https://platform:3080` for
  register/heartbeat/desired-state and drains on instruction. See the full
  matrix in the architecture doc (§5).

Limitations inherited from §8 apply: plaintext inter-machine links until
PR-H2 lands (the platform-to-node hops are the same class), single instance
of each control-plane service, and the topology itself is not yet verified on
real machines (acceptance = §7 adapted: P first, then GPU nodes).
