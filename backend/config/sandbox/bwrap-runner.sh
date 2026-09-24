#!/usr/bin/env bash
# Hardened Bubblewrap Runner for Sysadmin AI Platform Agent Commands
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <workspace_dir> <command> [args...]" >&2
  exit 1
fi

USER_WORKSPACE="$1"
shift
CMD=("$@")

if [ ! -d "$USER_WORKSPACE" ] || [ -L "$USER_WORKSPACE" ]; then
  echo "Error: workspace must be an existing directory, not a symlink" >&2
  exit 126
fi

BWRAP_BIN="${BWRAP_BIN:-/usr/bin/bwrap}"
if [ ! -x "$BWRAP_BIN" ]; then
  echo "Error: bwrap executable not found at $BWRAP_BIN" >&2
  exit 127
fi

TIMEOUT_BIN="$(command -v timeout || true)"
if [ -z "$TIMEOUT_BIN" ]; then
  echo "Error: timeout executable is required" >&2
  exit 127
fi

# If systemd-run --user is available and functional, use it directly to enforce
# exact cgroup resource ceilings (4 GiB memory, 128 pids, 200% CPU quota).
# MemorySwapMax=0 is required: memory.max alone lets the kernel swap pages out
# at the ceiling instead of OOM-killing, so total memory (RSS + swap) would
# exceed the 4 GiB envelope on any swap-enabled host.
if command -v systemd-run >/dev/null 2>&1 && systemd-run --user --scope -q true 2>/dev/null; then
  exec systemd-run --user --scope -q \
    -p MemoryMax=4G \
    -p MemorySwapMax=0 \
    -p TasksMax=128 \
    -p CPUQuota=200% \
    "$TIMEOUT_BIN" --kill-after=5s 15s "$BWRAP_BIN" \
    --unshare-all \
    --unshare-net \
    --die-with-parent \
    --ro-bind /usr /usr \
    --symlink usr/bin /bin \
    --symlink usr/sbin /sbin \
    --symlink usr/lib /lib \
    --symlink usr/lib64 /lib64 \
    --ro-bind-try /etc/resolv.conf /etc/resolv.conf \
    --ro-bind-try /etc/ssl /etc/ssl \
    --proc /proc \
    --dev /dev \
    --tmpfs /tmp \
    --bind "$USER_WORKSPACE" /workspace \
    --chdir /workspace \
    --cap-drop ALL \
    -- "${CMD[@]}"
fi

# The service must be delegated a writable cgroups v2 subtree. If any limit
# cannot be installed and read back, abort before starting an unbounded command.
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
CGROUP="$CGROUP_PARENT/sysadmin-agent-$$-$RANDOM"
if ! mkdir "$CGROUP" 2>/dev/null; then
  echo "Error: no delegated cgroup available for sandbox" >&2
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
  echo "Error: no delegated cgroup available for sandbox" >&2
  echo "Error: cannot enforce sandbox cgroup limits" >&2
  exit 126
fi

# memory.max alone does not bound total memory on hosts with swap: at the
# ceiling the kernel swaps anonymous pages out instead of OOM-killing, so the
# process grows past the 4 GiB envelope (measured: RSS pinned at 4 GiB while
# swap usage climbed without bound). Forbid swap so the ceiling is a real
# total-memory bound. A kernel without swap accounting has no memory.swap.max
# file and nothing to enforce.
if [ -f "$CGROUP/memory.swap.max" ]; then
  if ! echo 0 > "$CGROUP/memory.swap.max" 2>/dev/null ||
     [ "$(cat "$CGROUP/memory.swap.max" 2>/dev/null)" != "0" ]; then
    echo "Error: cannot enforce sandbox swap limit" >&2
    exit 126
  fi
fi

# Join the limited cgroup before executing Bubblewrap; all its descendants inherit it.
# The parent remains outside so it can reap the process and remove the cgroup.
set +e
(
  echo "$BASHPID" > "$CGROUP/cgroup.procs" || exit 126
  exec "$TIMEOUT_BIN" --kill-after=5s 15s "$BWRAP_BIN" \
  --unshare-all \
  --unshare-net \
  --die-with-parent \
  --ro-bind /usr /usr \
  --symlink usr/bin /bin \
  --symlink usr/sbin /sbin \
  --symlink usr/lib /lib \
  --symlink usr/lib64 /lib64 \
  --ro-bind-try /etc/resolv.conf /etc/resolv.conf \
  --ro-bind-try /etc/ssl /etc/ssl \
  --proc /proc \
  --dev /dev \
  --tmpfs /tmp \
  --bind "$USER_WORKSPACE" /workspace \
  --chdir /workspace \
  --cap-drop ALL \
  -- "${CMD[@]}"
)
status=$?
exit "$status"
