# Custom DeepSeek Harness integration

This package wires a **custom DeepSeek Harness (`dsh`) profile** into the sysadmin
AI platform so the agent runtime is the real harness instead of a hand-written
reAct loop, and so **multiple sysadmins can log in** and have their data flow
through the platform backend.

It is the first slice of [`docs/plans/DEVELOPMENT_PLAN.md`](../../docs/plans/DEVELOPMENT_PLAN.md)'s
"per-user Harness runtime + home + session store" architecture and of
specification document [`03 - Service Agent & Outils`](../../docs/specs/README.md)
(DeepSeek Harness & Plugins).

## Why a gateway: the harness is single-tenant

`@deepseek-ai/dsh`'s web surface authenticates **one** browser with a per-launch
token (`http://127.0.0.1:<port>/?token=…`). It has no user accounts, no login, and
one credential store, one session store and one workspace per process.

Multi-user is therefore built as **one harness process per authenticated
sysadmin**, with the process environment as the isolation boundary:

```
                       POST /api/gateway/login
  browser  ────────────────────────────────────►  harness gateway  ──►  auth gateway :3081
     │                                            (multi-user front)      (PBKDF2 + Valkey sessions)
     │  session cookie (opaque, HttpOnly)
     ▼
  gateway routes by identity ──► dsh #1  (sysadmin-01)  DSH_HOME=homes/sysadmin-01, key=sysadmin-01.key, port 3180
                             ──► dsh #2  (sysadmin-02)  DSH_HOME=homes/sysadmin-02, key=sysadmin-02.key, port 3181
                             ──► …
```

Each `dsh` instance gets its own `DSH_HOME`, workspace (`cwd`, the sandbox root),
loopback port, and `SYSADMIN_LITELLM_KEY`. No credential is ever written into a
shared configuration file.

## Data flow into the backend

| Data | Path | Contract |
|---|---|---|
| Model / token traffic | profile `llm-pi-ai` `litellm` route → **LiteLLM :4000** | `apiKeyEnv: SYSADMIN_LITELLM_KEY` = the user's virtual key, so concurrency, RPM/TPM and daily-token quotas are charged to the right identity |
| Tool execution | plugin tool `sysadmin_backend_tool` → **agent platform :3080** `POST /api/tools/execute` | bearer token = `backend/config/keys/<user>.key`; the backend's bounded tools, approval gate and workspace confinement stay authoritative |
| Audit | plugin `tools/result` + policy decisions → **VictoriaLogs :9428** `POST /insert/jsonline` | same event schema as `backend/services/agent_tools/audit.py`; durable local outbox on collector outage |
| Identity | gateway → **auth gateway :3081** `POST /api/v1/auth/login` | gateway stores only an opaque session id, never the password |

Command safety is enforced **twice with the same rules**: the plugin's
`tools/pre-execute` guard classifies shell commands with a port of the backend's
`backend/services/approval_gate/filter.py` (`BLOCKED` / `ALLOW` /
`APPROVAL_REQUIRED`), and a parity test asserts the two engines agree
action-for-action.

## Layout

```
packages/harness-integration/
├── profile/                     # the `sysadmin` dsh profile
│   ├── package.json             # dsh.profile.bundles: dsh-base, dsh-web-app, dsh-plugin-sysadmin
│   └── cordis.patch.yml         # deployment layer: LiteLLM route, default model, bind
├── dsh-plugin-sysadmin/         # the custom harness bundle
│   ├── package.json             # dsh.bundle.patch
│   ├── cordis.patch.yml         # inserts the sysadmin-harness row (env-driven config)
│   ├── index.js                 # policy guard, audit observer, backend tool bridge
│   └── lib/{policy,audit,backend}.js
├── gateway/                     # multi-user login gateway
│   ├── server.js                # login/logout, session cookie, HTTP + WebSocket proxy
│   ├── instance-manager.js      # per-user dsh process lifecycle + profile provisioning
│   ├── session-store.js         # opaque gateway sessions
│   └── config.js
├── scripts/verify-harness.mjs   # real-dsh verification (compose, boot, banner, 401)
├── tests/                       # node:test suite (policy parity, audit, gateway)
└── install-harness.sh           # stage the profile into $DSH_HOME/profiles/sysadmin
```

## Install and run

```bash
# 1. Backend services (auth gateway, agent platform, LiteLLM, VictoriaLogs, Valkey)
./platform.sh start

# 2. Stage the harness profile into $DSH_HOME (~/.dsh)
packages/harness-integration/install-harness.sh

# 3. Verify against the real dsh binary
node packages/harness-integration/scripts/verify-harness.mjs

# 4. Start the multi-user gateway
node packages/harness-integration/gateway/server.js     # http://127.0.0.1:3085
```

Then browse to the gateway, sign in as `sysadmin-01` with the password from
`backend/config/keys/initial-passwords.txt`, and the gateway starts that user's
harness instance.

Environment knobs (all optional): `SYSADMIN_GATEWAY_PORT` (3085),
`SYSADMIN_AUTH_URL` (3081), `SYSADMIN_BACKEND_URL` (3080), `SYSADMIN_LITELLM_URL`
(4000), `SYSADMIN_HARNESS_STATE`, `SYSADMIN_INSTANCE_PORT_START/END`
(3180–3280), `DSH_BIN`, `DSH_HOME`.

## Tests

```bash
node --test packages/harness-integration/tests/        # 19 tests
```

The suite covers command-policy classification and **Python parity**, audit
schema + outbox fallback, session/cookie handling, per-user provisioning, and the
full login → identity-scoped instance → proxy → logout path.

## Status and limits

- Verified here: profile composition (`--dump-config`), plugin load on a real
  `dsh` boot, web surface binding and unauthenticated refusal, plus the 19-test
  suite and Python policy parity.
- The gateway starts real `dsh` processes; that path is exercised by
  `scripts/verify-harness.mjs`, not by the unit suite (which injects a stub
  instance manager).
- Sandbox enforcement (`bwrap` + cgroups v2) is inherited from the profile and
  host; it is **not** qualified by this package. See
  [`../../docs/status/TEST_READY.md`](../../docs/status/TEST_READY.md).
- On the GPU host the `litellm` route should point at the real vLLM-backed
  LiteLLM deployment; locally the backend's inference engine is a simulator.
