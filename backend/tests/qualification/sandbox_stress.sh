#!/usr/bin/env bash
# Kernel-backed stress qualification of the Bubblewrap + cgroups v2 sandbox.
#
# Proves, with the kernel actually enforcing (not a fixture), that a command
# launched through bwrap-runner.sh:
#   - cannot exceed the 4 GiB memory ceiling (OOM-killed at the ceiling),
#   - cannot exceed the 128-task process ceiling (fork fails at the ceiling),
#   - cannot use more than ~2 CPUs (cpu.max 200000/100000 throttling),
#   - cannot outlive the 15 s deadline (+ 5 s kill grace),
#   - cannot reach the network, including a LIVE listener on the host loopback,
#   - cannot write outside /workspace, see host /etc, or mount filesystems,
#   - sees only its own PID namespace.
#
# The runner has two enforcement paths: systemd-run --user (preferred) and a
# raw cgroups v2 delegation fallback. Both legs are exercised; the fallback leg
# is forced by shadowing systemd-run with a stub that fails. If the session
# running this script has no writable delegated subtree, the fallback leg must
# FAIL CLOSED (exit 126, command never runs) and its enforcement scenarios are
# reported SKIP with the reason stated.
#
# Usage:   backend/tests/qualification/sandbox_stress.sh
# Exit:    0 = every assertion passed or skipped with an environment reason
#          1 = at least one assertion failed
#          2 = fatal harness error
set -u -o pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RUNNER="${RUNNER:-$HERE/../../config/sandbox/bwrap-runner.sh}"
[ -x "$RUNNER" ] || { echo "FATAL: runner not executable: $RUNNER" >&2; exit 2; }
PY_HOST="$(command -v python3)" || { echo "FATAL: python3 required on host" >&2; exit 2; }

FAILURES=0
SKIPS=0
pass() { printf 'PASS  %-34s %s\n' "$1" "$2"; }
fail() { printf 'FAIL  %-34s %s\n' "$1" "$2"; FAILURES=$((FAILURES+1)); }
skip() { printf 'SKIP  %-34s %s\n' "$1" "$2"; SKIPS=$((SKIPS+1)); }

now_ms() {
  # EPOCHREALTIME is seconds.microseconds (bash >= 5); date +%s%3N is not
  # portable and produced garbage on some hosts during qualification.
  local t=${EPOCHREALTIME}
  echo $(( ${t%.*} * 1000 + 10#${t#*.} / 1000 ))
}

# run_in_sandbox <stubdir|""> <workspace> <outdir> <cmd...>
# Sets RC and WALL_MS; command stdout/stderr land in <outdir>/{out,err}.
RC=0; WALL_MS=0
run_in_sandbox() {
  local stub="$1" ws="$2" outdir="$3"; shift 3
  local t0 t1
  mkdir -p "$outdir"
  t0=$(now_ms)
  if [ -n "$stub" ]; then
    PATH="$stub:$PATH" "$RUNNER" "$ws" "$@" >"$outdir/out" 2>"$outdir/err"
  else
    "$RUNNER" "$ws" "$@" >"$outdir/out" 2>"$outdir/err"
  fi
  RC=$?
  t1=$(now_ms)
  WALL_MS=$((t1 - t0))
}

write_scenarios() {
  local ws="$1"
  cat >"$ws/mem_under.py" <<'EOF'
x = bytearray(2 * 1024**3)              # 2 GiB < 4 GiB ceiling
for i in range(0, len(x), 4096):
    x[i] = 1
print("MEM_UNDER_OK", flush=True)
EOF
  cat >"$ws/mem_over.py" <<'EOF'
chunk = 256 * 1024**2
buf = []
for n in range(1, 25):                  # 24 x 256 MiB = 6 GiB > 4 GiB ceiling
    b = bytearray(chunk)
    for i in range(0, chunk, 4096):
        b[i] = 1
    buf.append(b)
    print(f"CHUNK_{n}", flush=True)
print("SURVIVED", flush=True)
EOF
  # Only bash builtins after the spawn loop: once pids.max is reached the
  # shell cannot fork wc/grep/sleep, so external tools would report nothing.
  cat >"$ws/pids.sh" <<'EOF'
#!/bin/bash
for i in {1..400}; do /usr/bin/sleep 8 & done 2>/workspace/fork_err.txt
for k in {1..400000}; do :; done
j=($(jobs -p))
echo "RUNNING=${#j[@]}"
kill $(jobs -p) 2>/dev/null
wait 2>/dev/null
EOF
  cat >"$ws/cpu.sh" <<'EOF'
#!/bin/bash
TIMEFORMAT='%R %U %S'
spin() { /usr/bin/python3 -c 'import time
end = time.time() + 8
while time.time() < end:
    pass'; }
{ time ( spin & spin & spin & spin & wait ); } 2>/workspace/timing.txt
echo "TIMING=$(cat /workspace/timing.txt)"
EOF
  cat >"$ws/net_client.py" <<'EOF'
import socket
for host, port, tag in (("127.0.0.1", PORT_PLACEHOLDER, "NET"), ("1.1.1.1", 443, "NET_EXT")):
    s = socket.socket()
    s.settimeout(2)
    code = s.connect_ex((host, port))
    print(f"{tag}_OK" if code == 0 else f"{tag}_BLOCKED_{code}", flush=True)
    s.close()
EOF
  cat >"$ws/fs.sh" <<'EOF'
#!/bin/bash
echo control > /workspace/ctl.txt && echo WS_WRITE_OK
if touch /usr/bin/pwn 2>/dev/null; then echo USR_WRITE_BAD; else echo USR_RO_OK; fi
if [ -e /etc/shadow ]; then echo SHADOW_VISIBLE_BAD; else echo NO_SHADOW_OK; fi
if [ -x /usr/bin/mount ]; then
  mkdir -p /tmp/m
  if /usr/bin/mount -t tmpfs none /tmp/m 2>/dev/null; then echo MOUNT_BAD; else echo MOUNT_DENIED_OK; fi
else
  echo MOUNT_BINARY_ABSENT
fi
EOF
  cat >"$ws/pidns.sh" <<'EOF'
#!/bin/bash
c=0; for d in /proc/[0-9]*; do c=$((c+1)); done; echo "PROCS=$c"
EOF
  chmod +x "$ws"/*.sh
}

# --- scenario assertions (reused for both legs) -----------------------------
# $1 = leg prefix, $2 = stubdir ("" for default leg), $3 = workspace
run_enforcement_scenarios() {
  local p="$1" stub="$2" ws="$3" od
  od=$(mktemp -d)

  # 1. memory below ceiling succeeds -----------------------------------------
  run_in_sandbox "$stub" "$ws" "$od/mem_under" /usr/bin/python3 /workspace/mem_under.py
  if [ "$RC" -eq 0 ] && grep -q MEM_UNDER_OK "$od/mem_under/out"; then
    pass "$p-mem-under-ceiling" "2 GiB allocation completed (rc=0, wall=${WALL_MS}ms)"
  else
    fail "$p-mem-under-ceiling" "rc=$RC expected 0; err: $(tail -2 "$od/mem_under/err")"
  fi

  # 2. memory above ceiling is OOM-killed ------------------------------------
  run_in_sandbox "$stub" "$ws" "$od/mem_over" /usr/bin/python3 /workspace/mem_over.py
  local last_chunk
  last_chunk=$(grep -o 'CHUNK_[0-9]*' "$od/mem_over/out" | tail -1 || true)
  if grep -q SURVIVED "$od/mem_over/out"; then
    fail "$p-mem-over-ceiling-killed" "process allocated 6 GiB and SURVIVED - ceiling not enforced"
  elif [ "$RC" -eq 0 ]; then
    fail "$p-mem-over-ceiling-killed" "rc=0 without SURVIVED - unexpected: $(tail -2 "$od/mem_over/err")"
  elif ! grep -q 'CHUNK_[3-9]' "$od/mem_over/out"; then
    fail "$p-mem-over-ceiling-killed" "allocation died before 768 MiB ($last_chunk) - ceiling too aggressive or unrelated failure"
  else
    pass "$p-mem-over-ceiling-killed" "OOM-killed at $last_chunk of 24 (rc=$RC), SURVIVED absent, wall=${WALL_MS}ms"
  fi

  # 3. task ceiling ------------------------------------------------------------
  run_in_sandbox "$stub" "$ws" "$od/pids" /usr/bin/bash /workspace/pids.sh
  local running forkers
  running=$(sed -n 's/^RUNNING=//p' "$od/pids/out")
  # Fork failures are counted on the host: inside the exhausted sandbox the
  # shell can no longer fork grep/wc to count them itself.
  forkers=$(grep -c -i 'fork' "$ws/fork_err.txt" 2>/dev/null || echo 0)
  forkers=${forkers//[^0-9]/}
  [ -n "$forkers" ] || forkers=0
  if [ -z "$running" ] || [ -z "$forkers" ]; then
    fail "$p-pids-ceiling" "no RUNNING/FORK_ERRORS markers (rc=$RC): $(tail -2 "$od/pids/err")"
  elif [ "$running" -le 128 ] && [ "$running" -ge 64 ] && [ "$forkers" -ge 1 ]; then
    pass "$p-pids-ceiling" "of 400 spawn attempts, $running ran concurrently, $forkers fork failures (ceiling 128)"
  else
    fail "$p-pids-ceiling" "RUNNING=$running FORK_ERRORS=$forkers - expected RUNNING in [64,128] with fork failures"
  fi

  # 4. CPU quota throttling ----------------------------------------------------
  run_in_sandbox "$stub" "$ws" "$od/cpu" /usr/bin/bash /workspace/cpu.sh
  local tline treal tuser ratio
  tline=$(sed -n 's/^TIMING=//p' "$od/cpu/out")
  treal=$(echo "$tline" | awk '{print $1}')
  tuser=$(echo "$tline" | awk '{print $2}')
  if [ -z "$treal" ] || [ -z "$tuser" ]; then
    fail "$p-cpu-quota" "no TIMING marker (rc=$RC): $(tail -2 "$od/cpu/err")"
  else
    ratio=$(awk -v u="$tuser" -v r="$treal" 'BEGIN{printf "%.2f", u/r}')
    if awk -v r="$ratio" 'BEGIN{exit !(r >= 1.4 && r <= 2.6)}'; then
      pass "$p-cpu-quota" "4 spinners x 8 s: user/real ratio $ratio (2-CPU quota; unthrottled host would be ~4)"
    else
      fail "$p-cpu-quota" "user/real ratio $ratio outside [1.4, 2.6] (real=${treal}s user=${tuser}s) - quota not enforced as 2 CPUs"
    fi
  fi

  # 5. deadline ----------------------------------------------------------------
  run_in_sandbox "$stub" "$ws" "$od/deadline" /usr/bin/python3 -c 'import time; time.sleep(60); print("SURVIVED")'
  if grep -q SURVIVED "$od/deadline/out"; then
    fail "$p-deadline" "60 s sleep SURVIVED the 15 s deadline"
  elif [ "$WALL_MS" -ge 11000 ] && [ "$WALL_MS" -le 23000 ] && [ "$RC" -ne 0 ]; then
    pass "$p-deadline" "killed after ${WALL_MS}ms (deadline 15 s + 5 s grace, rc=$RC)"
  else
    fail "$p-deadline" "wall=${WALL_MS}ms rc=$RC - expected wall in [11 s, 23 s], rc != 0"
  fi

  # 6. network denial (live host listener + external) ---------------------------
  run_in_sandbox "$stub" "$ws" "$od/net" /usr/bin/python3 /workspace/net_client.py
  if grep -q '^NET_OK' "$od/net/out"; then
    fail "$p-network-denied" "sandbox reached a LIVE host loopback listener: $(cat "$od/net/out")"
  elif grep -q '^NET_BLOCKED' "$od/net/out"; then
    local ext
    ext=$(grep '^NET_EXT' "$od/net/out" || true)
    pass "$p-network-denied" "live host listener unreachable ($(grep '^NET_BLOCKED' "$od/net/out")); external: $ext"
  else
    fail "$p-network-denied" "no NET markers (rc=$RC): $(tail -2 "$od/net/err")"
  fi

  # 7. filesystem confinement ---------------------------------------------------
  run_in_sandbox "$stub" "$ws" "$od/fs" /usr/bin/bash /workspace/fs.sh
  local fsok=1 mnote
  grep -q WS_WRITE_OK "$od/fs/out"  || fsok=0
  grep -q USR_RO_OK "$od/fs/out"    || fsok=0
  grep -q NO_SHADOW_OK "$od/fs/out" || fsok=0
  if grep -q MOUNT_BAD "$od/fs/out" || grep -q USR_WRITE_BAD "$od/fs/out" || grep -q SHADOW_VISIBLE_BAD "$od/fs/out"; then
    fail "$p-fs-confinement" "escape marker present: $(tr '\n' ' ' < "$od/fs/out")"
  elif [ "$fsok" -eq 1 ]; then
    mnote=$(grep -o 'MOUNT_[A-Z_]*' "$od/fs/out" | head -1)
    pass "$p-fs-confinement" "workspace writable; /usr read-only; host /etc absent; mount: $mnote"
  else
    fail "$p-fs-confinement" "missing markers: $(tr '\n' ' ' < "$od/fs/out")"
  fi

  # 8. PID namespace isolation ---------------------------------------------------
  run_in_sandbox "$stub" "$ws" "$od/pidns" /usr/bin/bash /workspace/pidns.sh
  local procs
  procs=$(sed -n 's/^PROCS=//p' "$od/pidns/out")
  if [ -n "$procs" ] && [ "$procs" -lt 64 ]; then
    pass "$p-pid-namespace" "only $procs processes visible in /proc (host has hundreds)"
  else
    fail "$p-pid-namespace" "PROCS=$procs - expected < 64 (rc=$RC)"
  fi
}

# --- harness ------------------------------------------------------------------
WS=$(mktemp -d)
STUB=$(mktemp -d)
LISTENER_PID=""
trap 'rm -rf "$WS" "$STUB"; [ -n "$LISTENER_PID" ] && kill "$LISTENER_PID" 2>/dev/null; true' EXIT
printf '#!/bin/sh\nexit 1\n' >"$STUB/systemd-run"; chmod +x "$STUB/systemd-run"
write_scenarios "$WS"

# Host loopback listener: proves the network test is not vacuous.
NETPORT=""
for port in 16379 16380 16381; do
  "$PY_HOST" - "$port" <<'EOF' &
import socket, sys, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(1); time.sleep(300)
EOF
  LPID=$!
  sleep 0.4
  if "$PY_HOST" - "$port" <<'EOF'
import socket, sys
s = socket.socket(); s.settimeout(1)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
EOF
  then
    LISTENER_PID=$LPID; NETPORT=$port; break
  else
    kill $LPID 2>/dev/null; wait $LPID 2>/dev/null
  fi
done
if [ -z "$NETPORT" ]; then
  echo "FATAL: could not start a host loopback listener for the network scenario" >&2
  exit 2
fi
sed -i "s/PORT_PLACEHOLDER/$NETPORT/" "$WS/net_client.py"

echo "== sandbox stress qualification =="
echo "runner: $RUNNER"
echo "host listener: 127.0.0.1:$NETPORT (pid $LISTENER_PID)"

# --- LEG A: default path (systemd-run --user preferred) -----------------------
if systemd-run --user --scope -q true 2>/dev/null; then
  echo "leg A: systemd-run --user path"
else
  echo "leg A: raw cgroups v2 delegation path (systemd-run unavailable)"
fi
run_enforcement_scenarios "legA" "" "$WS"

# --- LEG B: forced raw-cgroup fallback -----------------------------------------
echo "leg B: raw cgroups v2 fallback path (systemd-run shadowed)"
probe=$(mktemp -d)
run_in_sandbox "$STUB" "$WS" "$probe" /usr/bin/python3 -c 'print("FALLBACK_RAN")'
if [ "$RC" -eq 126 ] && ! grep -q FALLBACK_RAN "$probe/out"; then
  pass "legB-fail-closed" "no writable delegated subtree in this session: runner aborted 126 BEFORE executing the command (marker absent)"
  for s in mem-under-ceiling mem-over-ceiling-killed pids-ceiling cpu-quota deadline network-denied fs-confinement pid-namespace; do
    skip "legB-$s" "fallback enforcement unmeasured: this session has no delegated writable subtree; run under the platform account delegated subtree"
  done
elif [ "$RC" -eq 0 ] && grep -q FALLBACK_RAN "$probe/out"; then
  echo "leg B: delegated subtree available - running enforcement scenarios"
  run_enforcement_scenarios "legB" "$STUB" "$WS"
else
  fail "legB-fail-closed" "unexpected rc=$RC; out: $(cat "$probe/out"); err: $(tail -2 "$probe/err")"
fi
rm -rf "$probe"

echo "== summary: $FAILURES failed, $SKIPS skipped =="
[ "$FAILURES" -eq 0 ]
