#!/usr/bin/env bash
# Hardened Bubblewrap & cgroups v2 Runner for DeepSeek Harness (DSH) Instances
# Enforces unprivileged execution, resource envelope, and filesystem confinement.
# Exits with 126 on any security validation or confinement failure (fail-closed).
set -euo pipefail

USER_ID=""
USER_DSH_HOME=""
USER_WORKSPACE=""
NETNS_NAME=""
MOCK_MODE=0

# Parse options
while [ "$#" -gt 0 ]; do
  case "$1" in
    --user)
      if [ "$#" -lt 2 ]; then
        echo "Error: option $1 requires an argument" >&2
        exit 126
      fi
      USER_ID="$2"
      shift 2
      ;;
    --home)
      if [ "$#" -lt 2 ]; then
        echo "Error: option $1 requires an argument" >&2
        exit 126
      fi
      USER_DSH_HOME="$2"
      shift 2
      ;;
    --workspace)
      if [ "$#" -lt 2 ]; then
        echo "Error: option $1 requires an argument" >&2
        exit 126
      fi
      USER_WORKSPACE="$2"
      shift 2
      ;;
    --netns)
      if [ "$#" -lt 2 ]; then
        echo "Error: option $1 requires an argument" >&2
        exit 126
      fi
      NETNS_NAME="$2"
      shift 2
      ;;
    --mock)
      MOCK_MODE=1
      shift
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "Error: unrecognized option $1" >&2
      exit 126
      ;;
    *)
      # Positional fallback: <userId> <dshHome> <workspace> [netns] -- <cmd...>
      if [ -z "$USER_ID" ] && [ "$#" -ge 3 ]; then
        USER_ID="$1"
        USER_DSH_HOME="$2"
        USER_WORKSPACE="$3"
        shift 3
        if [ "$#" -gt 0 ] && [ "$1" != "--" ]; then
          NETNS_NAME="$1"
          shift
        fi
        if [ "$#" -gt 0 ] && [ "$1" = "--" ]; then
          shift
        fi
        break
      else
        break
      fi
      ;;
  esac
done

CMD=("$@")

# 1. Validate User ID
if [ -z "$USER_ID" ]; then
  echo "Error: user ID is required (--user <userId>)" >&2
  exit 126
fi
if [[ ! "$USER_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  echo "Error: invalid user ID: $USER_ID" >&2
  exit 126
fi

# 2. Validate Directories: must exist, be directory, not a symlink, mode 0700
validate_dir_0700() {
  local target="$1"
  local name="$2"

  if [ -z "$target" ]; then
    echo "Error: $name path is required" >&2
    exit 126
  fi

  if [ ! -e "$target" ]; then
    echo "Error: $name does not exist: $target" >&2
    exit 126
  fi

  if [ -L "$target" ]; then
    echo "Error: $name must not be a symlink: $target" >&2
    exit 126
  fi

  if [ ! -d "$target" ]; then
    echo "Error: $name is not a directory: $target" >&2
    exit 126
  fi

  local mode=""
  if stat -c '%a' "$target" >/dev/null 2>&1; then
    mode="$(stat -c '%a' "$target" 2>/dev/null)"
  elif stat -f '%Lp' "$target" >/dev/null 2>&1; then
    mode="$(stat -f '%Lp' "$target" 2>/dev/null)"
  fi

  if [ -n "$mode" ]; then
    case "$mode" in
      700|0700)
        ;;
      *)
        echo "Error: $name must have permissions 0700, got $mode: $target" >&2
        exit 126
        ;;
    esac
  fi
}

validate_dir_0700 "$USER_WORKSPACE" "workspace"
validate_dir_0700 "$USER_DSH_HOME" "DSH_HOME"

# 3. Validate Command
if [ "${#CMD[@]}" -eq 0 ]; then
  echo "Error: no command specified to run" >&2
  exit 126
fi

# 4. Construct Bubblewrap Arguments
BWRAP_ARGS=(
  --die-with-parent
  --unshare-pid
  --unshare-ipc
  --unshare-uts
  --unshare-cgroup-try
  --cap-drop ALL
  --proc /proc
  --dev /dev
  --tmpfs /tmp
  --ro-bind /usr /usr
  --symlink usr/bin /bin
  --symlink usr/sbin /sbin
  --symlink usr/lib /lib
)

if [ -d /usr/lib64 ]; then
  BWRAP_ARGS+=(--symlink usr/lib64 /lib64)
fi

# Read-only configuration files
for cfg in /etc/resolv.conf /etc/ssl /etc/pki /etc/hosts /etc/localtime /etc/passwd; do
  if [ -e "$cfg" ]; then
    BWRAP_ARGS+=(--ro-bind-try "$cfg" "$cfg")
  fi
done

if [ -d /usr/local ]; then
  BWRAP_ARGS+=(--ro-bind-try /usr/local /usr/local)
fi

# Strictly bounded read-write mounts
BWRAP_ARGS+=(
  --bind "$USER_DSH_HOME" "$USER_DSH_HOME"
  --bind "$USER_WORKSPACE" "$USER_WORKSPACE"
  --chdir "$USER_WORKSPACE"
)

# Optional audit outbox file mount if specified
if [ -n "${SYSADMIN_AUDIT_OUTBOX:-}" ]; then
  if [ ! -e "$SYSADMIN_AUDIT_OUTBOX" ]; then
    mkdir -p "$(dirname "$SYSADMIN_AUDIT_OUTBOX")" 2>/dev/null || true
    touch "$SYSADMIN_AUDIT_OUTBOX" 2>/dev/null || true
    chmod 0600 "$SYSADMIN_AUDIT_OUTBOX" 2>/dev/null || true
  fi
  if [ -e "$SYSADMIN_AUDIT_OUTBOX" ]; then
    BWRAP_ARGS+=(--bind-try "$SYSADMIN_AUDIT_OUTBOX" "$SYSADMIN_AUDIT_OUTBOX")
  fi
fi

# Network namespace isolation handling:
# When running within a dedicated netns, do not unshare net.
# If no netns is provided, complete airgap via unshare-net.
if [ -n "$NETNS_NAME" ] && [ "$NETNS_NAME" != "none" ]; then
  :
else
  BWRAP_ARGS+=(--unshare-net)
fi

# 5. Check Mock / Non-privileged mode
IS_DARWIN=0
if [ "$(uname -s)" = "Darwin" ]; then
  IS_DARWIN=1
fi

BWRAP_BIN="${BWRAP_BIN:-/usr/bin/bwrap}"
HAS_BWRAP=0
if [ -x "$BWRAP_BIN" ] || command -v bwrap >/dev/null 2>&1; then
  HAS_BWRAP=1
fi

if [ "$MOCK_MODE" -eq 1 ] || [ "${SYSADMIN_SANDBOX_MOCK:-0}" = "1" ] || [ "$IS_DARWIN" -eq 1 ]; then
  ENVELOPE_FILE="${DSH_SANDBOX_ENVELOPE_OUT:-${SYSADMIN_SANDBOX_ENVELOPE_FILE:-/tmp/dsh-sandbox-envelope-${USER_ID}.json}}"

  # Format bwrapArgs as JSON array
  ARGS_JSON="["
  FIRST=1
  for arg in "${BWRAP_ARGS[@]}"; do
    if [ "$FIRST" -eq 1 ]; then
      FIRST=0
    else
      ARGS_JSON+=","
    fi
    ESCAPED_ARG="${arg//\\/\\\\}"
    ESCAPED_ARG="${ESCAPED_ARG//\"/\\\"}"
    ARGS_JSON+="\"$ESCAPED_ARG\""
  done
  ARGS_JSON+="]"

  # Format command as JSON array
  CMD_JSON="["
  FIRST=1
  for arg in "${CMD[@]}"; do
    if [ "$FIRST" -eq 1 ]; then
      FIRST=0
    else
      CMD_JSON+=","
    fi
    ESCAPED_ARG="${arg//\\/\\\\}"
    ESCAPED_ARG="${ESCAPED_ARG//\"/\\\"}"
    CMD_JSON+="\"$ESCAPED_ARG\""
  done
  CMD_JSON+="]"

  cat <<EOF > "$ENVELOPE_FILE"
{
  "userId": "$USER_ID",
  "home": "$USER_DSH_HOME",
  "workspace": "$USER_WORKSPACE",
  "netns": "$NETNS_NAME",
  "mock": true,
  "cgroupLimits": {
    "MemoryMax": "4G",
    "MemorySwapMax": "0",
    "TasksMax": 128,
    "CPUQuota": "200%"
  },
  "bwrapArgs": $ARGS_JSON,
  "command": $CMD_JSON
}
EOF

  if [ -n "${MOCK_FAIL_CODE:-}" ]; then
    echo "Simulated mock failure with exit code $MOCK_FAIL_CODE" >&2
    exit "$MOCK_FAIL_CODE"
  fi

  cd "$USER_WORKSPACE"
  exec "${CMD[@]}"
fi

# 6. Real Linux Sandbox Execution
if [ ! -x "$BWRAP_BIN" ]; then
  if command -v bwrap >/dev/null 2>&1; then
    BWRAP_BIN="$(command -v bwrap)"
  else
    echo "Error: bwrap executable not found at $BWRAP_BIN" >&2
    exit 126
  fi
fi

# Path A: systemd-run --user --scope
if command -v systemd-run >/dev/null 2>&1 && systemd-run --user --scope -q true 2>/dev/null; then
  if [ -n "$NETNS_NAME" ] && [ "$NETNS_NAME" != "none" ]; then
    exec systemd-run --user --scope -q \
      -p MemoryMax=4G \
      -p MemorySwapMax=0 \
      -p TasksMax=128 \
      -p CPUQuota=200% \
      ip netns exec "$NETNS_NAME" \
      "$BWRAP_BIN" "${BWRAP_ARGS[@]}" -- "${CMD[@]}"
  else
    exec systemd-run --user --scope -q \
      -p MemoryMax=4G \
      -p MemorySwapMax=0 \
      -p TasksMax=128 \
      -p CPUQuota=200% \
      "$BWRAP_BIN" "${BWRAP_ARGS[@]}" -- "${CMD[@]}"
  fi
fi

# Path B: Manual cgroup v2 subtree
CGROUP_MOUNT=/sys/fs/cgroup
if [ ! -f "$CGROUP_MOUNT/cgroup.controllers" ]; then
  echo "Error: cgroups v2 is required" >&2
  exit 126
fi

CGROUP_PATH=""
while IFS=: read -r hierarchy controllers path; do
  if [ "$hierarchy" = 0 ] && [ -z "$controllers" ]; then
    CGROUP_PATH="$path"
    break
  fi
done < /proc/self/cgroup

if [ -z "$CGROUP_PATH" ]; then
  echo "Error: cannot locate current cgroup" >&2
  exit 126
fi

CGROUP_PARENT="$CGROUP_MOUNT$CGROUP_PATH"
CGROUP="$CGROUP_PARENT/sysadmin-dsh-$$-$RANDOM"
if ! mkdir "$CGROUP" 2>/dev/null; then
  echo "Error: no delegated cgroup available for dsh sandbox" >&2
  exit 126
fi

cleanup() {
  if [ -f "$CGROUP/cgroup.kill" ]; then
    echo 1 > "$CGROUP/cgroup.kill" 2>/dev/null || true
  fi
  rmdir "$CGROUP" 2>/dev/null || true
}
trap cleanup EXIT

if ! { echo 4294967296 > "$CGROUP/memory.max" 2>/dev/null &&
       echo 128 > "$CGROUP/pids.max" 2>/dev/null &&
       echo '200000 100000' > "$CGROUP/cpu.max" 2>/dev/null; } ||
   [ "$(cat "$CGROUP/memory.max" 2>/dev/null)" != 4294967296 ] ||
   [ "$(cat "$CGROUP/pids.max" 2>/dev/null)" != 128 ] ||
   [ "$(cat "$CGROUP/cpu.max" 2>/dev/null)" != '200000 100000' ]; then
  echo "Error: cannot enforce dsh sandbox cgroup limits" >&2
  exit 126
fi

if [ -f "$CGROUP/memory.swap.max" ]; then
  if ! echo 0 > "$CGROUP/memory.swap.max" 2>/dev/null ||
     [ "$(cat "$CGROUP/memory.swap.max" 2>/dev/null)" != "0" ]; then
    echo "Error: cannot enforce dsh sandbox swap limit" >&2
    exit 126
  fi
fi

set +e
(
  echo "$BASHPID" > "$CGROUP/cgroup.procs" || exit 126
  if [ -n "$NETNS_NAME" ] && [ "$NETNS_NAME" != "none" ]; then
    exec ip netns exec "$NETNS_NAME" "$BWRAP_BIN" "${BWRAP_ARGS[@]}" -- "${CMD[@]}"
  else
    exec "$BWRAP_BIN" "${BWRAP_ARGS[@]}" -- "${CMD[@]}"
  fi
)
status=$?
exit "$status"
