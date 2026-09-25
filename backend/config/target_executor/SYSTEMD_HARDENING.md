# Systemd hardening note — target adapter / executor

The executor (`/usr/local/libexec/sysadmin-target-exec`) is the only component
that ever runs with privilege. The adapter that *calls* it must run as an
unprivileged, dedicated account so that an adapter compromise cannot touch host
state directly. This note describes how to harden the adapter's systemd unit;
the executor itself runs for one short invocation under `sudo` and needs no
systemd unit.

## Adapter unit hardening (drop-in)

Run the target-adapter-facing service (the platform worker that hosts
`target_adapter`) as `sysadmin-agent` with, at minimum:

```ini
[Service]
User=sysadmin-agent
Group=sysadmin-agent
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/sysadmin-target-exec/staging
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
```

Rationale:

- `User=sysadmin-agent` — the adapter is unprivileged; it can only mutate host
  state by invoking the executor through the tightly scoped sudoers entry.
- `NoNewPrivileges=yes` + empty `CapabilityBoundingSet`/`AmbientCapabilities` —
  the adapter process can never gain capabilities; the only escalation is the
  intentional `sudo -n` trampoline to the executor.
- `ProtectSystem=strict` — the adapter cannot write anywhere except the
  explicitly listed `ReadWritePaths`. It MUST be able to write the staging dir
  (`/var/lib/sysadmin-target-exec/staging`) so it can stage files for the
  executor; it MUST NOT be able to write any allow-listed destination.
- `PrivateTmp=yes` — the adapter's `/tmp` is private, so a staged file can never
  accidentally land in a shared, attacker-observable location.

## Directory ownership

| Path | Owner:group | Mode | Purpose |
|---|---|---|---|
| `/usr/local/libexec/sysadmin-target-exec` | root:root | 0755 | The privileged executor binary |
| `/etc/sysadmin-target-exec/allowlist.json` | root:root | 0644 | The root-owned allowlist the executor enforces |
| `/var/lib/sysadmin-target-exec/staging` | root:sysadmin-agent | 0770 | Staging area writable only by root and the adapter account |

`install-executor.sh` creates these (staging dir with group `sysadmin-agent`
when that group exists). The staging dir is the only host path the adapter can
write; the executor validates that every staged file lives directly inside it,
is a regular non-symlink file, is not group/world writable, is not root-owned,
and matches the approval-bound sha256 before it is installed.

## Residual risks

- The executor is invoked with `sudo -n`, which uses a passwordless sudoers
  entry. A compromise of the `sysadmin-agent` account is therefore *as powerful
  as the executor's allowlist*: it can restart/reload/status the allow-listed
  units and install files under the allow-listed destinations. It cannot add
  new allowlist entries (the allowlist is root-owned, not group/world writable)
  or run anything other than the executor.
- `sudo -n` will hang or fail if the sudoers entry is missing or a password is
  required; the adapter surfaces this as a failed execution, not a silent
  fallback.
