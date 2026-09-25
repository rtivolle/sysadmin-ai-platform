#!/usr/bin/env bash
# ==============================================================================
# Sysadmin AI Platform - Self-Update ("platform modules update")
#
# Updates the platform's own modules — backend services, the DeepSeek Harness
# package, Python dependencies and (optionally) the pinned static binaries — to
# a newer revision, then restarts exactly the services that were running.
#
# Two update sources are supported:
#   * git (default): fetch the tracking branch and fast-forward only.
#   * --source DIR : overlay files from another checkout via a tar stream, with
#                    generated and secret directories excluded.
#
# What an update NEVER touches:
#   backend/config/keys/*  secrets are never regenerated or overwritten
#   backend/data/*         Valkey/SeaweedFS/VictoriaLogs state and workspaces
#   backend/logs, backend/run, backend/.venv, backend/.vllm-venv,
#   backend/bin (unless --binaries removes only the downloaded binaries so
#                install.sh re-downloads them), harness node_modules
#
# Fail closed: the script refuses a dirty working tree, a non-fast-forward
# upstream, a missing upstream, a checkout without provisioned keys, or an
# unattended confirmation. It never resets, rebases or rewrites history.
#
# Every outcome is appended as one JSON line to backend/logs/update.log.
#
# Exit codes: 0 success / up to date, 1 updates available (--check only),
#             2 failure or refusal.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${SCRIPT_DIR}"
BACKEND_DIR="${ROOT_DIR}/backend"
KEYS_DIR="${BACKEND_DIR}/config/keys"
RUN_DIR="${BACKEND_DIR}/run"
LOGS_DIR="${BACKEND_DIR}/logs"
INSTALL_SH="${ROOT_DIR}/install.sh"
PLATFORM_SH="${BACKEND_DIR}/platform.sh"
HARNESS_INSTALLER="${ROOT_DIR}/packages/harness-integration/install-harness.sh"
UPDATE_LOG="${LOGS_DIR}/update.log"

# Canonical start order (kept in sync with platform.sh; harness_gateway is
# opt-in there but must be restarted too when it was running).
SERVICE_START_ORDER="valkey victorialogs audit_outbox seaweedfs inference auth_gateway agent_tools litellm traefik harness_gateway"

# The static binaries install.sh downloads itself (Valkey entries in
# backend/bin are symlinks to PATH/Linuxbrew/distro binaries and are re-linked
# by install.sh when missing, so they are not removed here).
DOWNLOADED_BINARIES="traefik victoria-logs-prod weed"

CHECK_ONLY=0
ASSUME_YES=0
DRY_RUN=0
SKIP_RESTART=0
FORCE=0
WITH_BINARIES=0
SKIP_VLLM=0
SOURCE_DIR=""
REF=""

usage() {
  cat <<EOF
Usage: ./update.sh [options]

Update the platform modules to a newer revision (git fast-forward, or a
directory overlay with --source), refresh dependencies via install.sh and
restart exactly the services that were running.

  --check           Fetch and report only; exit 1 when updates are available.
  --source DIR      Overlay a checkout directory instead of using git.
  --ref REV         Update to a specific revision instead of the tracking branch.
  --yes, -y         Skip the confirmation prompt (automation).
  --dry-run         Run every check and print the plan without applying anything.
  --skip-restart    Apply code and dependencies but leave services untouched.
  --binaries        Re-download pinned static binaries (stops services first).
  --skip-vllm       Pass --skip-vllm through to install.sh (faster updates).
  --force           Proceed despite a dirty working tree (git mode).
  --help, -h        Show this help.

Exit codes: 0 up to date/success, 1 updates available (--check), 2 failure.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-restart) SKIP_RESTART=1; shift ;;
    --force) FORCE=1; shift ;;
    --binaries) WITH_BINARIES=1; shift ;;
    --skip-vllm) SKIP_VLLM=1; shift ;;
    --source) SOURCE_DIR="${2:?--source requires a directory}"; shift 2 ;;
    --ref) REF="${2:?--ref requires a revision}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

say()  { echo "  $*"; }
warn() { echo "  [!] $*" >&2; }

now() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# Append one JSON line to the local update audit log. Never prints secrets.
log_event() {
  mkdir -p "${LOGS_DIR}" 2>/dev/null || true
  printf '%s\n' "$1" >> "${UPDATE_LOG}" 2>/dev/null || true
}

# Refuse the operation, record it and exit 2 (fail closed).
fail() {
  local stage="${1:-preflight}"
  shift
  local msg
  msg="$(printf '%s' "$*" | tr '"' "'")"
  echo "  [!] ${msg}" >&2
  log_event "{\"ts\":\"$(now)\",\"result\":\"error\",\"stage\":\"${stage}\",\"error\":\"${msg}\"}"
  exit 2
}

# Safety net for any unexpected abort: record it before the shell exits.
on_err() {
  local rc=$?
  log_event "{\"ts\":\"$(now)\",\"result\":\"error\",\"stage\":\"abort\",\"rc\":${rc}}"
}
trap on_err ERR

# Which services are running right now (their pid files are alive)?
running_services() {
  local name pid pid_file
  for name in $SERVICE_START_ORDER; do
    pid_file="${RUN_DIR}/${name}.pid"
    [ -f "$pid_file" ] || continue
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    [ -n "$pid" ] || continue
    if kill -0 "$pid" 2>/dev/null; then
      echo "$name"
    fi
  done
}

# ---------------------------------------------------------------------------
# 1. Preflight: this is a platform checkout, with secrets, on a safe tree.
# ---------------------------------------------------------------------------
[ -f "$INSTALL_SH" ] || fail preflight "install.sh not found; update.sh must run from the repository root."
[ -f "$PLATFORM_SH" ] || fail preflight "backend/platform.sh not found; this is not a platform checkout."

if [ "$SOURCE_DIR" != "" ]; then
  [ -d "$SOURCE_DIR" ] || fail preflight "--source is not a directory: ${SOURCE_DIR}"
  [ -f "${SOURCE_DIR}/install.sh" ] || fail preflight "--source does not look like a platform checkout (no install.sh)."
  [ -f "${SOURCE_DIR}/backend/platform.sh" ] || fail preflight "--source does not look like a platform checkout (no backend/platform.sh)."
  [ "$CHECK_ONLY" = 0 ] || fail preflight "--check is not supported with --source; compare the trees directly instead."
fi

for key in master valkey-password; do
  [ -s "${KEYS_DIR}/${key}.key" ] || fail preflight "missing ${KEYS_DIR}/${key}.key — run ./install.sh before updating."
done

GIT_REPO=0
if git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  GIT_REPO=1
fi
if [ "$SOURCE_DIR" = "" ] && [ "$GIT_REPO" != 1 ]; then
  fail preflight "not a git repository and no --source DIR given; nothing to update from."
fi

if [ "$GIT_REPO" = 1 ] && [ -n "$(git -C "$ROOT_DIR" status --porcelain)" ]; then
  if [ "$FORCE" != 1 ]; then
    fail dirty "working tree has uncommitted changes; refusing to update over them. Commit or stash first, or re-run with --force."
  fi
  warn "working tree is dirty; proceeding because --force was given."
fi

# ---------------------------------------------------------------------------
# 2. Resolve the update target (git: upstream tracking branch or --ref).
# ---------------------------------------------------------------------------
LOCAL_HEAD=""
TARGET_REF=""
TARGET_HASH=""
UPSTREAM=""
REMOTE=""
if [ "$GIT_REPO" = 1 ] && [ "$SOURCE_DIR" = "" ]; then
  UPSTREAM="$(git -C "$ROOT_DIR" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
  [ -n "$UPSTREAM" ] || fail preflight "no upstream tracking branch. Set one (git branch --set-upstream-to=origin/<branch>) or use --source DIR."
  REMOTE="${UPSTREAM%%/*}"
  LOCAL_HEAD="$(git -C "$ROOT_DIR" rev-parse HEAD)"
  say "Fetching upstream (${REMOTE})..."
  if ! git -C "$ROOT_DIR" fetch --quiet --tags "$REMOTE"; then
    fail fetch "git fetch from '${REMOTE}' failed; cannot check for updates."
  fi
  TARGET_REF="${REF:-$UPSTREAM}"
  TARGET_HASH="$(git -C "$ROOT_DIR" rev-parse "$TARGET_REF" 2>/dev/null || true)"
  [ -n "$TARGET_HASH" ] || fail fetch "cannot resolve revision '${TARGET_REF}' (did you fetch it?)."
fi

UP_TO_DATE=0
if [ "$GIT_REPO" = 1 ] && [ "$SOURCE_DIR" = "" ]; then
  if [ "$LOCAL_HEAD" = "$TARGET_HASH" ]; then
    UP_TO_DATE=1
  elif ! git -C "$ROOT_DIR" merge-base --is-ancestor "$LOCAL_HEAD" "$TARGET_HASH"; then
    fail history "cannot fast-forward ${LOCAL_HEAD:0:12} to ${TARGET_HASH:0:12}: history has diverged or local commits exist. This script never rebases or resets; resolve manually."
  fi
fi

# ---------------------------------------------------------------------------
# 3. --check: report only.
# ---------------------------------------------------------------------------
if [ "$CHECK_ONLY" = 1 ]; then
  if [ "$UP_TO_DATE" = 1 ]; then
    echo "Up to date at ${LOCAL_HEAD:0:12}."
    exit 0
  fi
  count="$(git -C "$ROOT_DIR" rev-list --count "${LOCAL_HEAD}..${TARGET_HASH}")"
  echo "${count} update(s) available: ${LOCAL_HEAD:0:12} -> ${TARGET_HASH:0:12}"
  exit 1
fi

# ---------------------------------------------------------------------------
# 4. Plan, dry-run, up-to-date short-circuit.
# ---------------------------------------------------------------------------
if [ "$SOURCE_DIR" = "" ]; then
  count="$(git -C "$ROOT_DIR" rev-list --count "${LOCAL_HEAD}..${TARGET_HASH}")"
  say "Update: ${LOCAL_HEAD:0:12} -> ${TARGET_HASH:0:12} (${count} commit(s), fast-forward only)."
  if [ "$count" -gt 0 ]; then
    # `head` closing the pipe early must not abort the script (pipefail + SIGPIPE).
    git -C "$ROOT_DIR" log --oneline "${LOCAL_HEAD}..${TARGET_HASH}" 2>/dev/null | sed 's/^/      /' | head -n 20 || true
    if [ "$count" -gt 20 ]; then
      say "      ... and $((count - 20)) more commit(s)."
    fi
  fi
else
  say "Update: overlay files from ${SOURCE_DIR} (adds/replaces files; never deletes files removed upstream)."
fi

if [ "$WITH_BINARIES" = 1 ]; then
  say "Binaries: will re-download ${DOWNLOADED_BINARIES} (running services are stopped first)."
fi

RUNNING_SERVICES="$(running_services)"

if [ "$DRY_RUN" = 1 ]; then
  echo "Dry run: nothing will be changed."
  if [ "$UP_TO_DATE" = 1 ] && [ "$WITH_BINARIES" = 0 ]; then
    echo "  Already up to date; no action needed."
    exit 0
  fi
  echo "  would run: ./install.sh${SKIP_VLLM:+ --skip-vllm}"
  [ -f "$HARNESS_INSTALLER" ] && echo "  would run: ${HARNESS_INSTALLER}"
  if [ "$SKIP_RESTART" = 1 ]; then
    echo "  services: untouched (--skip-restart)"
  elif [ -n "$RUNNING_SERVICES" ]; then
    echo "  would restart: ${RUNNING_SERVICES}"
  else
    echo "  services: nothing running, nothing to restart"
  fi
  exit 0
fi

if [ "$UP_TO_DATE" = 1 ] && [ "$WITH_BINARIES" = 0 ]; then
  echo "Already up to date at ${LOCAL_HEAD:0:12}; nothing to do."
  exit 0
fi

# ---------------------------------------------------------------------------
# 5. Confirmation (fail closed without a terminal and no --yes).
# ---------------------------------------------------------------------------
if [ "$ASSUME_YES" != 1 ]; then
  prompt="Apply this update now?"
  if [ -n "$RUNNING_SERVICES" ] && [ "$SKIP_RESTART" != 1 ]; then
    prompt="${prompt} Running services (${RUNNING_SERVICES}) will be restarted."
  fi
  printf '%s [y/N] ' "$prompt"
  if ! read -r answer; then
    fail confirm "no interactive confirmation and --yes not given; aborting."
  fi
  case "$answer" in
    y|Y|yes|YES) ;;
    *) echo "Aborted by operator."; exit 0 ;;
  esac
fi

# ---------------------------------------------------------------------------
# 6. Apply: stop-for-binaries, code update, dependencies, harness modules.
# ---------------------------------------------------------------------------
if [ "$WITH_BINARIES" = 1 ] && [ -n "$RUNNING_SERVICES" ]; then
  say "Stopping running services so the pinned binaries can be replaced..."
  for name in $(printf '%s\n' "$RUNNING_SERVICES" | tac | tr '\n' ' '); do
    "$PLATFORM_SH" service "$name" stop || warn "failed to stop ${name}"
  done
  for bin in $DOWNLOADED_BINARIES; do
    rm -f "${BACKEND_DIR}/bin/${bin}"
    say "Removed ${bin} for re-download."
  done
fi

if [ "$SOURCE_DIR" = "" ]; then
  say "Fast-forwarding to ${TARGET_HASH:0:12}..."
  if ! git -C "$ROOT_DIR" merge --ff-only "$TARGET_REF"; then
    fail merge "fast-forward merge failed; the working tree was left unchanged."
  fi
else
  say "Overlaying files from ${SOURCE_DIR}..."
  # A tar stream with excluded subtrees: secrets, state, generated artifacts
  # and harness dependency closures are never copied from the source tree.
  ( cd "$SOURCE_DIR" && tar \
      --exclude='./.git' \
      --exclude='./.pytest_cache' \
      --exclude='./backend/bin' \
      --exclude='./backend/logs' \
      --exclude='./backend/run' \
      --exclude='./backend/.venv' \
      --exclude='./backend/.vllm-venv' \
      --exclude='./backend/data' \
      --exclude='./backend/config/keys' \
      --exclude='./packages/harness-integration/gateway/node_modules' \
      --exclude='./packages/harness-integration/dsh-plugin-sysadmin/node_modules' \
      -cf - . ) | ( cd "$ROOT_DIR" && tar -xf - )
  warn "overlay adds/replaces files but does not delete files removed upstream; review afterwards."
fi

# Record the applied revision for the success log line.
APPLIED_TO="${TARGET_HASH}"
if [ "$GIT_REPO" = 1 ]; then
  APPLIED_TO="$(git -C "$ROOT_DIR" rev-parse HEAD)"
fi

say "Refreshing dependencies and configuration (./install.sh)..."
if [ "$SKIP_VLLM" = 1 ]; then
  if ! "$INSTALL_SH" --skip-vllm; then
    fail install "install.sh failed after the update was applied; services are NOT restarted. See ${UPDATE_LOG}."
  fi
else
  if ! "$INSTALL_SH"; then
    fail install "install.sh failed after the update was applied; services are NOT restarted. See ${UPDATE_LOG}."
  fi
fi

if [ -f "$HARNESS_INSTALLER" ]; then
  say "Refreshing harness profile modules (${HARNESS_INSTALLER})..."
  "$HARNESS_INSTALLER" || warn "harness profile refresh failed; the gateway still re-stages the plugin on next start."
fi

# Post-update invariants: secrets must still exist and be non-empty.
for key in master valkey-password; do
  [ -s "${KEYS_DIR}/${key}.key" ] || fail postflight "post-update check: ${KEYS_DIR}/${key}.key is missing or empty — restore from backup immediately."
done

# ---------------------------------------------------------------------------
# 7. Restart exactly the services that were running before the update.
# ---------------------------------------------------------------------------
RESTARTED=""
if [ "$SKIP_RESTART" != 1 ] && [ -n "$RUNNING_SERVICES" ]; then
  for name in $SERVICE_START_ORDER; do
    case " $RUNNING_SERVICES " in
      *" $name "*)
        say "Restarting ${name}..."
        if "$PLATFORM_SH" service "$name" restart; then
          RESTARTED="${RESTARTED}${RESTARTED:+ }${name}"
        else
          warn "restart of ${name} failed; see ${LOGS_DIR}/${name}.log"
        fi
        ;;
    esac
  done
fi

# ---------------------------------------------------------------------------
# 8. Success: audit log + summary.
# ---------------------------------------------------------------------------
restarted_json="[]"
if [ -n "$RESTARTED" ]; then
  restarted_json="["
  for name in $RESTARTED; do
    restarted_json="${restarted_json}\"${name}\","
  done
  restarted_json="${restarted_json%,}]"
fi
binaries_json=false
[ "$WITH_BINARIES" = 1 ] && binaries_json=true

log_event "{\"ts\":\"$(now)\",\"source\":\"${SOURCE_DIR:-git}\",\"from\":\"${LOCAL_HEAD:-overlay}\",\"to\":\"${APPLIED_TO}\",\"result\":\"updated\",\"restarted\":${restarted_json},\"binaries\":${binaries_json}}"

echo "============================================================"
echo " Platform modules updated to ${APPLIED_TO:0:12}."
if [ -n "$RESTARTED" ]; then
  echo " Restarted: ${RESTARTED}"
elif [ "$SKIP_RESTART" = 1 ]; then
  echo " Services untouched (--skip-restart); restart them to load the new code."
else
  echo " No services were running; start them with ./platform.sh start."
fi
echo " Update log: ${UPDATE_LOG}"
echo "============================================================"
exit 0
