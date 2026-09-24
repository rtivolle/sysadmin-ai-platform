# Operations guide

This guide covers installing, running and troubleshooting the platform on a
single Linux host.

## 1. Host requirements

- Linux with a recent kernel, Python 3 and Bash.
- **Bubblewrap** (`bwrap`) — required; the installer aborts without it.
- **GNU `timeout`** — required by the sandbox runner.
- **cgroups v2** with a writable subtree delegated to the service account.
  Without delegation, sandbox execution fails closed.
- Optional: `ripgrep` (`/usr/bin/rg`) for faster log search; `systemd-run`;
  NVIDIA drivers and an upstream vLLM endpoint for real inference.

The installer downloads native static binaries for Traefik, VictoriaLogs,
SeaweedFS and Valkey (Valkey may be symlinked from Linuxbrew or `PATH`).

## 2. Install

```bash
./install.sh          # directories, binaries, Python venv, keys, logins
./install.sh --tui    # interactive configuration wizard (then rerun ./install.sh)
./install.sh --survey # hardware survey only
```

What `install.sh` does:

1. Creates `backend/{bin,config,data,logs,run}` and one `0700` workspace per
   user (`sysadmin-01`…`10`, `emergency-p1-oncall`).
2. Verifies Bubblewrap.
3. Installs native binaries into `backend/bin/`.
4. Creates `backend/.venv` and installs
   `litellm[proxy] fastapi uvicorn httpx pyyaml redis pydantic`.
5. Fixes script permissions and creates the root `platform.sh` symlink.
6. Provisions keys (`config/keys/provision-keys.sh`) and login credentials
   (`config/keys/provision-logins.py`).

### Credentials produced

| File | Contents |
|---|---|
| `backend/config/keys/sysadmin-01.key`…`10.key` | Per-user bearer + LiteLLM virtual key. |
| `backend/config/keys/emergency-p1.key` | P1 on-call bearer key. |
| `backend/config/keys/master.key` | Administrator key. |
| `backend/config/keys/valkey-password.key` | Valkey password. |
| `backend/config/keys/login-credentials.json` | PBKDF2 hashes. |
| `backend/config/keys/initial-passwords.txt` | One-time plaintext passwords. |

All are mode `0600` and Git-ignored. Reruns preserve existing random keys and
replace legacy deterministic keys. To rotate login passwords:

```bash
backend/.venv/bin/python3 backend/config/keys/provision-logins.py --rotate
```

Move the initial passwords to a password manager and delete the plaintext file
from the host after delivery.

## 3. Start, inspect and stop

```bash
./platform.sh start            # start the 8 services + audit outbox worker
./platform.sh status           # PID, port and RSS per service
./platform.sh logs [service]   # tail one log, or all
./platform.sh stop             # graceful stop (SIGTERM, then SIGKILL)
./platform.sh restart
./platform.sh test             # end-to-end verification (starts services if needed)
./platform.sh dashboard        # live TUI dashboard
./platform.sh survey           # hardware survey
./platform.sh chat             # interactive terminal assistant
```

The hardware survey covers PCI accelerators and their kernel drivers, driver
and toolkit availability, installed software versions, and model-store capacity
and directory sizes in addition to host/GPU and sandbox readiness.

`start` requires `master.key` and `valkey-password.key`; it injects
`LITELLM_MASTER_KEY` and `VALKEY_URL` into the services and writes a rendered
Valkey config with the password substituted into `backend/run/valkey.conf`.

Service startup order: Valkey → VictoriaLogs → audit outbox → SeaweedFS →
inference → auth gateway → agent tools → LiteLLM → Traefik.

State lives in `backend/run/*.pid`; logs in `backend/logs/*.log`.

## 4. Terminal assistant

```bash
./sysadmin-chat                       # SYSADMIN_USER defaults to sysadmin-01
SYSADMIN_USER=sysadmin-admin ./sysadmin-chat
GATEWAY_URL=http://127.0.0.1:8080 ./sysadmin-chat
```

The CLI talks to Traefik at `GATEWAY_URL` using the bearer key for
`SYSADMIN_USER`. ReAct reasoning streams live (Server-Sent Events) by default;
toggle it with `/stream off`. `Ctrl+C` during a query cancels the in-flight
request without exiting; a second `Ctrl+C` (or `/exit`) quits.

| Command | Action |
|---|---|
| `/users` | List provisioned identities. |
| `/user ID` / `/whoami` | Switch identity / show current identity and role. |
| `/sessions` | List this user's sessions. |
| `/session ID` / `/session rm ID` | Continue / delete a session. |
| `/new` | Start a fresh session. |
| `/tools` | List registered tools. |
| `/status` / `/logs [service]` | Run `./platform.sh status` / tail logs. |
| `/health` | Check gateway and service health. |
| `/approvals` | List pending approvals (admin). |
| `/approve ID` / `/reject ID` | Decide an approval (admin). |
| `/resume ID` | Execute the approved command once (requester). |
| `/p1 status\|elevate <incident>\|revoke` | P1 elevation controls. |
| `/help` / `/exit` | Help / quit. |

Any other input is sent to the ReAct agent. When a command needs approval the
CLI prints the approval id; an administrator reviews it with
`/approvals` + `/approve ID`, then the requester runs `/resume ID`.

The same operations are available as non-interactive subcommands for scripting:

```bash
./sysadmin-chat chat "search for connect errors in nginx logs" --stream
./sysadmin-chat chat "summarize the outage" --json --user sysadmin-admin
./sysadmin-chat tools
./sysadmin-chat users
./sysadmin-chat sessions --delete <id>
./sysadmin-chat approvals
./sysadmin-chat decide <id> --approve --user sysadmin-admin
./sysadmin-chat health
```

### Approval workflow across identities

```text
sysadmin-01: "restart nginx"            -> approval id appr-xxxx (5 min TTL)
sysadmin-admin: /approvals ; /approve appr-xxxx
sysadmin-01: /resume appr-xxxx          -> executes exactly once
```

## 5. Web surfaces

- **Harness UI** — start the Node gateway
  (`cd packages/harness-integration/gateway && node server.js`, port 3085) and
  open it; it authenticates against the auth gateway and proxies to a per-user
  harness instance.
- **Agent platform API** — `:3080`, normally through Traefik `:8080`.
- **Traefik dashboard** — disabled by default.

## 6. Health checks

```bash
curl -s http://127.0.0.1:3081/health   # auth gateway
curl -s http://127.0.0.1:3080/health   # agent platform
curl -s http://127.0.0.1:8000/health   # inference engine
curl -s http://127.0.0.1:9428/health   # VictoriaLogs
curl -s http://127.0.0.1:4000/health/readiness
```

A quick authenticated smoke test:

```bash
KEY=$(cat backend/config/keys/sysadmin-01.key)
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8080/api/tools/list
```

## 7. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| Sandbox exits `126` "no delegated cgroup" | cgroups v2 not delegated | Delegate a writable cgroup v2 subtree to the service account; re-test Tier 2. |
| Commands run without limits | host prerequisites missing | The runner is designed to fail closed; check the log for a `126` rather than accepting execution. |
| `401` from the CLI | wrong/missing key or user | Check `config/keys/<user>.key`; verify `SYSADMIN_USER`. |
| `429` from chat | concurrency/RPM/daily limit | Wait, or use an approved P1 elevation for genuine incidents. |
| `503` "Shared quota state unavailable" | Valkey down or unreachable | Check `valkey.log`; the platform fails closed by design. |
| Agent returns `502` | LiteLLM/inference unavailable | Check `litellm.log` and `inference.log`; verify `UPSTREAM_VLLM_URL` if set. |
| No audit events | VictoriaLogs down | Events queue in `backend/data/victorialogs/outbox.jsonl`; the worker replays on recovery. |
| Approval says "invalid, expired, mismatched or already used" | binding mismatch or reuse | Request a new approval; check the binds (user/session/command/workspace). |
| `ls`, `cat` require approval unexpectedly | shell metacharacters present | The allow-list excludes any shell syntax; remove it or approve. |

Useful log locations:

```text
backend/logs/{traefik,litellm,agent_tools,auth_gateway,inference,
              seaweedfs,valkey,victorialogs,audit_outbox}.log
backend/data/victorialogs/outbox.jsonl          # pending audit replay
backend/run/                                     # PID files and rendered config
```

## 8. Shutdown and data

`./platform.sh stop` stops services but keeps data. Runtime state is under
`backend/data/` (Valkey, VictoriaLogs, SeaweedFS, workspaces) and is Git-ignored.
For backup/restore see [backup-restore.md](backup-restore.md).
