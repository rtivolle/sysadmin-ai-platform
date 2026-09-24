# Security model

This document describes the controls that are implemented, the trust boundaries
they create, and the qualification work that remains. It is written to be
honest: a control that is not tested on the target host is labelled as such.

## 1. Security objectives

1. **Sovereignty** — no runtime egress to third-party clouds; model traffic,
   storage and audit are local.
2. **Confinement** — agent-generated shell commands cannot read or write the host
   outside their workspace, cannot reach the network, and cannot exhaust host
   resources.
3. **Human authorisation** — mutating operations require an administrator's
   approval bound to the exact request.
4. **Identity integrity** — identity comes only from verified credentials, never
   from client-supplied headers or model arguments.
5. **Traceability** — every tool call and completion is audited durably.

## 2. Identity and access

### Credentials

- **Bearer keys** — one random key per account in
  `backend/config/keys/<user>.key` (mode `0600`, Git-ignored). `master.key`
  identifies `sysadmin-admin`.
- **Login passwords** — random PBKDF2-SHA256 hashes (600,000 iterations, 32-byte
  salt) in `login-credentials.json`. Plaintext is written once to
  `initial-passwords.txt` for delivery and must be removed from the host.
- **Session cookies** — 256-bit opaque ids stored in Valkey for 24 h.

### Header spoofing

Traefik's `strip-client-headers` middleware clears `X-User`,
`X-Forwarded-User`, `X-Forwarded-Role`, `X-User-Role` and
`X-User-Workspace` before forwarding. The auth gateway ignores those headers
entirely and re-derives identity. The agent platform's
`authenticate_request` also validates credentials directly, so direct backend
access cannot impersonate an administrator by setting a header.

### Roles

| Role | Assigned to | Capabilities |
|---|---|---|
| `admin` | `sysadmin-admin` (master key) | List/decide approvals, any status query, LiteLLM proxy-admin. |
| `p1-operator` | a user with active P1 elevation | Elevated quotas; still bound by sandbox, approval and audit. |
| `sysadmin` | `sysadmin-01`…`10`, `emergency-p1-oncall` | Tools, chat, own sessions/approvals. |

Only an `admin` may decide an approval, and a requester cannot decide their own.

### P1 emergency elevation

- Requires a named on-call identity **and** a mandatory `incident_id`
  (3–32 alphanumeric/hyphen characters).
- TTL ≤ 3600 s; enforced by Valkey key expiry and checked on read.
- Elevated limits: 6 in-flight, 200 RPM, 500k TPM, 10M daily tokens.
- `revoke_p1_elevation` deletes the elevation key and every issued token, so a
  revocation invalidates all outstanding P1 tokens for the user.
- P1 **never** bypasses Bubblewrap, the command filter, human approval or audit.
  If the backend cannot prioritise running work, P1 is admission priority, not
  GPU preemption.
- Fail closed: when `VALKEY_URL` is set and the shared store is unavailable,
  elevation operations raise `ConnectionError` (surfaced as `503`).

## 3. Sandbox and resource confinement

**Runner:** `backend/config/sandbox/bwrap-runner.sh`, invoked by
`execute_sandboxed_command` as
`bwrap-runner.sh <workspace> /bin/sh -c <command>`.

Controls:

- **Workspace validation** — the workspace must exist, be a directory and not a
  symlink, or the runner exits `126`. The workspace is bound read-write at
  `/workspace` and is the working directory.
- **Namespaces** — `--unshare-all`, `--unshare-net`, `--die-with-parent`;
  host `/usr` is mounted read-only, `/proc`, `/dev` and a tmpfs `/tmp` are
  provided, and all capabilities are dropped (`--cap-drop ALL`).
- **Resource envelope** — either `systemd-run --user --scope` with
  `MemoryMax=4G`, `TasksMax=128`, `CPUQuota=200%` when available, or a
  manually created cgroup v2 subtree with `memory.max=4294967296`,
  `pids.max=128` and `cpu.max=200000 100000`.
- **Deadline** — `timeout --kill-after=5s 15s` around Bubblewrap.
- **Fail closed** — if the cgroup limits cannot be installed **and read back**
  exactly, or cgroups v2 is unavailable, the runner aborts before executing the
  command. It never falls back to unbounded host execution.
- **Cleanup** — on exit the cgroup is killed via `cgroup.kill` and removed.

### What the sandbox does not do

Bubblewrap is a sandboxing tool, not a complete security policy. The runner
relies on the operator's host configuration (unprivileged user namespaces,
delegated cgroups) and does not install a seccomp filter. The exact host
hardening must be validated during qualification.

### Path confinement (backend tools)

Read tools resolve and canonicalise paths, reject a forbidden list
(`/etc/shadow`, `/etc/gshadow`, `/etc/sudoers*`, `/proc/kcore`, `/dev/mem`),
reject `.ssh` and `backend/config/keys`, and require the result to stay within
category roots (`logs`, `runbooks`, `configs`, `workspaces`, `/tmp`).
Workspace access is additionally scoped to the authenticated user, so
cross-user reads fail.

## 4. Approval gate

- State machine:
  `proposed → pending → approved | rejected | expired → executing → succeeded | failed`.
- Binding: user, session, workspace, target, action, exact command, content hash
  and base hash; 300-second expiry.
- Atomic compare-and-swap in Valkey ensures an approval is claimed **once**.
  Claiming an already-executing, consumed or expired token fails.
- Deciding requires the `admin` role and a reviewer different from the
  requester.
- When `VALKEY_URL` is configured, an unavailable store fails closed. The
  in-memory implementation is for development only.

## 5. Command policy

The same classification is applied in Python and JavaScript:

| Verdict | Meaning | Examples |
|---|---|---|
| `BLOCKED` | Refused unconditionally. | `rm -rf`, `mkfs`, `dd if=/dev/sd*`, writes to block devices, firewall flushes, reboot/shutdown, fork bombs. |
| `ALLOW` | Runs without approval. | A simple read-only command with no shell metacharacters and a whitelisted argv[0]: `cat date df echo free head id ls pwd tail uname wc whoami`. |
| `APPROVAL_REQUIRED` | Needs an administrator. | Any mutating command, or any command containing `; | & < > ` $ ( ) { } \` or newlines. |

Regex lists are warnings, **not** a security boundary; the sandbox and approval
binding are the boundary. Instructions embedded in logs or runbooks are treated
as untrusted data.

## 6. Target adapter

- Four actions only; nine whitelisted services; a fixed set of configuration
  roots; sensitive paths rejected.
- Staged files must be regular files directly inside the caller's workspace,
  opened with `O_NOFOLLOW`, capped at 1 MiB.
- Deployment verifies the proposed hash, detects out-of-band base-hash changes,
  backs up before replacing, verifies after replacing and rolls back on failure.
- Reviewer authority and executor identity are derived from authenticated
  identity; a caller cannot act as another user.

> **Open qualification.** The adapter's privileged execution boundary has not
> been qualified, and running `systemctl` inside the sandbox does not restart a
> host service. A production deployment needs a separate least-privilege
> execution path and a clean staging target validation.

## 7. Audit

- Structured events carry `event_id`, UTC timestamp, service, user/session,
  action, tool, parameters (bounded), command, approval id, human-approved flag,
  exit code, duration, priority, incident id and token counts.
- Delivery is at-least-once: VictoriaLogs first, then a `fsync`-ed local outbox
  replayed by a supervised worker. A crash can replay an accepted event;
  `event_id` supports deduplication.
- Malformed outbox records are quarantined so they cannot halt replay.
- Audit failure is recorded but does not block a tool turn in the harness.

> **Retention/integrity.** VictoriaLogs is configured with a 90-day retention
> period. That is a storage setting, not proof of immutability or tamper
> resistance. The design calls for an independent integrity archive; it is not
> implemented here.

## 8. Threat model summary

| Threat | Control | Status |
|---|---|---|
| Privilege escalation from agent shell | Bubblewrap namespaces, cap-drop, no network, read-only host | Implemented; host qualification pending |
| Resource exhaustion (fork bomb, OOM) | cgroup `pids.max`/`memory.max`/`cpu.max`, fail closed | Implemented; kernel stress run pending |
| Destructive command | Python + JS command filter | Implemented and unit-tested |
| Unauthorised mutation | Human approval bound to exact request, single-use CAS | Implemented and tested |
| Identity spoofing | Header stripping + credential re-validation at each layer | Implemented and tested |
| Cross-user data access | Server-owned `0700` workspaces, per-user workspace checks | Implemented and tested |
| Quota bypass | Independent LiteLLM + auth-gateway enforcement, fail closed | Implemented; lease runtime verification pending |
| Replay of an approval | Status CAS, single-use consumption | Implemented and tested |
| Prompt injection authorising an action | Actions require out-of-band approval; content treated as data | Design property; adversarial evaluation pending |
| Silent audit loss | Durable outbox, replay worker, `event_id` | Implemented; query-completeness check pending |

## 9. Known gaps

These are carried over from [`status/TEST_READY.md`](status/TEST_READY.md) and
the code, and must be closed before any production target change:

1. Privileged execution boundary and staging target deployment not qualified.
2. Multi-worker Valkey integration run outstanding. Lease acquisition,
   renewal, release and daily-token reservation/settlement have been verified
   against a live Valkey; see [`status/TEST_READY.md`](status/TEST_READY.md).
3. Inference is simulated unless `UPSTREAM_VLLM_URL` is configured; Dynamo and
   the full Harness are not deployed by this codebase.
4. Tier 2 sandbox/namespace/cgroup tests require a host with delegated writable
   cgroups v2 and unprivileged namespaces; they skip where those are missing and
   pass on the current development host.
5. Real owner-scored 30-task model evaluation not performed.
6. Kernel-backed sandbox stress run not performed.
7. Audit query completeness and full restore drill against clean staging remain.
8. Measured RTO under four hours not established (the drill checks logic).
9. The login cookie is issued with `secure=False` for the loopback HTTP
   prototype; enable TLS and mark it secure before exposing the portal.
10. Traefik CORS uses a wildcard origin list (`*`); restrict it to known portals
    before production.
11. Retention is a VictoriaLogs setting and does not by itself provide
    tamper-evident archival.
