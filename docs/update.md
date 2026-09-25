# Platform self-update (`update.sh`)

The platform can update its own modules — the backend services, the DeepSeek
Harness package, Python dependencies and, optionally, the pinned static
binaries — to a newer revision and restart exactly the services that were
running. The entry point is `./update.sh` at the repository root (golden
commands: `make update`, `make update-check`).

## 1. What an update does

```text
preflight checks  ->  fetch & compare  ->  fast-forward (git) / overlay (--source)
                  ->  ./install.sh     ->  harness profile refresh
                  ->  restart the services that were running
```

1. **Preflight (fail closed).** Refuses unless:
   - the working directory is a platform checkout (`install.sh` and
     `backend/platform.sh` exist),
   - the secrets exist (`backend/config/keys/master.key`,
     `backend/config/keys/valkey-password.key`),
   - in git mode, the working tree is clean (no uncommitted changes) — unless
     `--force` is given,
   - in git mode, a tracking branch is configured and the update target is a
     fast-forward of `HEAD`. The script never rebases, resets or rewrites
     history; a diverged history is a refusal, not a merge.
2. **Code update.** Git mode: `git fetch` the tracking remote, then
   `git merge --ff-only`. Overlay mode (`--source DIR`): copy files from
   another checkout over a tar stream with the generated and secret subtrees
   excluded; it adds/replaces files and never deletes files removed upstream.
3. **Dependencies and configuration.** Re-runs `./install.sh` (pass
   `--skip-vllm` to skip the slow isolated vLLM environment refresh). Existing
   keys are kept by `provision-keys.sh`; existing binaries are kept.
4. **Harness modules.** Re-runs `packages/harness-integration/install-harness.sh`
   when present, refreshing the `sysadmin` profile plugin in `$DSH_HOME`.
5. **Restart.** Restarts exactly the services whose PID files were alive before
   the update, in dependency order (`valkey` first, `harness_gateway` last).
   Services that were stopped stay stopped. Use `--skip-restart` to leave the
   stack untouched (the running processes keep the old code until restarted).

Every outcome — success, refusal or failure — is appended as one JSON line to
`backend/logs/update.log` (`ts`, `source`, `from`, `to`, `result`, `restarted`,
`binaries`, and `stage`/`error` for failures).

## 2. What an update never touches

| Path | Why |
|---|---|
| `backend/config/keys/*` | Secrets are never regenerated, copied or overwritten; the overlay path excludes this subtree entirely. |
| `backend/data/*` | Valkey/SeaweedFS/VictoriaLogs state and the 0700 workspaces. |
| `backend/logs`, `backend/run`, `backend/.venv`, `backend/.vllm-venv`, `.pytest_cache` | Generated directories. |
| `backend/bin` | Pinned binaries are kept as installed, unless `--binaries` removes only the downloaded binaries (`traefik`, `victoria-logs-prod`, `weed`) so `install.sh` re-downloads them. Valkey symlinks are re-created by `install.sh`. |
| harness `node_modules` | The per-profile dependency closure under `$DSH_HOME` and gateway `node_modules` are excluded from overlays. |

A post-update invariant check re-verifies the secrets are still present and
non-empty before services are restarted.

## 3. Modes and options

```text
./update.sh [--check] [--yes] [--dry-run] [--skip-restart]
            [--source DIR] [--ref REV] [--binaries] [--skip-vllm] [--force]
```

| Option | Effect |
|---|---|
| *(none)* | Apply the update interactively. With no terminal and no `--yes`, the script aborts (fail closed). |
| `--check` | Fetch and report only. Exit `0` up to date, `1` updates available, `2` refusal/error. Suitable for cron. |
| `--yes`, `-y` | Skip the confirmation prompt (automation). |
| `--dry-run` | Run every check and print the plan (including `install.sh` and the restart list) without changing anything. |
| `--skip-restart` | Apply code and dependencies but leave running services untouched. |
| `--source DIR` | Update from a directory checkout instead of git (non-git deployments). `--check` is not supported with `--source`. |
| `--ref REV` | Fast-forward to a specific revision (branch/tag) instead of the tracking branch. |
| `--binaries` | Stop the running services, remove the downloaded static binaries and let `install.sh` re-download the currently pinned versions, then restart. |
| `--skip-vllm` | Passed through to `install.sh`; skips the vLLM environment refresh (much faster when vLLM is unchanged). |
| `--force` | Proceed despite a dirty working tree (git mode). Non-overlapping local changes survive the fast-forward; overlapping ones make the merge fail and nothing is applied. |

Exit codes: `0` success / up to date, `1` updates available (`--check` only),
`2` failure or refusal.

## 4. Examples

```bash
make update-check                 # exit 1 when updates are available
make update                       # interactive full update
./update.sh --yes                 # unattended (cron-friendly)
./update.sh --yes --skip-vllm     # fast update when vLLM did not change
./update.sh --binaries --yes      # also refresh pinned static binaries
./update.sh --source /srv/platform-staging   # non-git host, staged checkout
./update.sh --dry-run             # show the plan without applying it
```

## 5. Multi-host notes

`update.sh` is role-aware through the same machinery as `install.sh` and
`platform.sh`: it re-runs `install.sh` (which reads
`backend/config/roles/deployment.env`) and restarts only the services the
host's role owns. Update the three machines in the order recommended for
`install.sh` (data first, then inference, then web); each host updates only its
own modules. See [multi-host.md](multi-host.md).

## 6. Rollback

An update never rewrites history, so the previous revision stays reachable in
git and a rollback is an ordinary operator action:

```bash
git -C . log --oneline -3
git -C . merge --ff-only <previous-revision>   # if it is a descendant
```

`backend/logs/update.log` records the exact `from`/`to` hashes of every applied
update. The script itself offers no rollback command because one would require
rewriting the checkout, which the platform guardrails forbid for automation.

## 7. Verification

The fail-closed contract is covered by
`backend/tests/tier1_unit/test_update_script.py` (10 tests, local bare-repo
fixtures, no network): `--check` exit codes, fast-forward application with
secrets preserved byte-for-byte, dirty-tree and diverged-history refusals,
missing-key refusal, fail-closed confirmation without a terminal, and overlay
exclusion of `keys`/`data`/`logs`/`run`/`bin`. Run with:

```bash
backend/.venv/bin/python3 -m pytest backend/tests/tier1_unit/test_update_script.py -q
```
