#!/usr/bin/env bash
# ==============================================================================
# Sysadmin AI Platform - Backend Services Lifecycle Manager
# Ultra-Low Overhead: Native Go/C Binaries & Lightweight Services (Zero Docker)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${SCRIPT_DIR}"
if [ -d "${SCRIPT_DIR}/backend" ]; then
  ROOT_DIR="${SCRIPT_DIR}/backend"
fi

BIN_DIR="${ROOT_DIR}/bin"
CONFIG_DIR="${ROOT_DIR}/config"
DATA_DIR="${ROOT_DIR}/data"
LOGS_DIR="${ROOT_DIR}/logs"
RUN_DIR="${ROOT_DIR}/run"
SERVICES_DIR="${ROOT_DIR}/services"
VENV_PYTHON="${ROOT_DIR}/.venv/bin/python3"
VENV_LITELLM="${ROOT_DIR}/.venv/bin/litellm"
VLLM_VENV_DIR="${VLLM_VENV_DIR:-${ROOT_DIR}/.vllm-venv}"
HARNESS_DIR="${ROOT_DIR}/../packages/harness-integration"

# The agent platform defaults to 3080. On a host where the DeepSeek Harness web
# surface already owns that port, export SYSADMIN_AGENT_PORT=<free port> before
# `platform.sh start` and point the Traefik agent-service route at the same
# port (see docs/status/TEST_READY.md).
AGENT_PORT="${SYSADMIN_AGENT_PORT:-3080}"
HARNESS_STATE_DIR="${DATA_DIR}/harness"

mkdir -p "$LOGS_DIR" "$RUN_DIR" "$DATA_DIR/valkey" "$DATA_DIR/seaweedfs" "$DATA_DIR/victorialogs"

# ---- Multi-host deployment role (PR-H1) -------------------------------------
# backend/config/roles/deployment.env (git-ignored) is the only way this host
# stops being the single-host `all` role. Absent file => `all` => exactly
# today's behaviour. See docs/multi-host.md.
DEPLOYMENT_ENV="${CONFIG_DIR}/roles/deployment.env"
# Environment may pre-set ROLE (tests/CI); deployment.env wins when present.
ROLE="${ROLE:-all}"
if [ -f "$DEPLOYMENT_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  # shellcheck source=/dev/null
  . "$DEPLOYMENT_ENV"
  set +a
fi
ROLE="${ROLE:-all}"

# Outward-facing bind address: loopback for single-host `all`, the machine's
# LAN address otherwise. Traefik keeps binding every interface (front door) and
# is restricted by backend/config/firewall/web.nft instead.
LAN_BIND_IP="${LAN_BIND_IP:-127.0.0.1}"
if [ "$ROLE" = "all" ]; then
  LAN_BIND_IP="127.0.0.1"
fi

# Peer addresses used to build VALKEY_URL / VICTORIALOGS_URL / LITELLM_URL.
PEER_INFERENCE_HOST="${PEER_INFERENCE_HOST:-127.0.0.1}"
PEER_INFERENCE_PORT="${PEER_INFERENCE_PORT:-4000}"
PEER_DATA_HOST="${PEER_DATA_HOST:-127.0.0.1}"
PEER_DATA_VALKEY_PORT="${PEER_DATA_VALKEY_PORT:-6379}"
PEER_DATA_LOGS_PORT="${PEER_DATA_LOGS_PORT:-9428}"
PEER_DATA_SEAWEEDFS_PORT="${PEER_DATA_SEAWEEDFS_PORT:-8333}"

# Canonical start order (dependencies first); role filtering selects a subset.
# harness_gateway is intentionally absent: it is opt-in via `platform.sh harness`.
SERVICE_START_ORDER="valkey victorialogs audit_outbox seaweedfs inference auth_gateway agent_tools litellm traefik"

# Services this host's role owns (shown in status, stopped on stop).
role_services() {
  case "$ROLE" in
    all)        echo "valkey victorialogs audit_outbox seaweedfs inference auth_gateway agent_tools litellm traefik harness_gateway" ;;
    web)        echo "auth_gateway agent_tools audit_outbox traefik harness_gateway" ;;
    inference)  echo "litellm inference audit_outbox" ;;
    data)       echo "valkey victorialogs seaweedfs" ;;
    *)
      echo "Unknown role: ${ROLE} (expected all|web|inference|data)" >&2
      return 1
      ;;
  esac
}

role_owns() {
  local svc list
  list="$(role_services)" || return 1
  for svc in $list; do
    [ "$svc" = "$1" ] && return 0
  done
  return 1
}

# Export peer URLs that need no secret (safe to set for every command).
#
# VALKEY_HOST/VALKEY_PORT matter as much as VALKEY_URL: ForwardAuth
# (auth_gateway/server.py) builds its P1 elevation client from
# VALKEY_HOST/VALKEY_PORT/VALKEY_PASSWORD, not from VALKEY_URL. On the web role
# they must name the data peer instead of loopback, otherwise every elevation
# looks for a local Valkey that does not exist and fails closed with 503.
# The SYSADMIN_* mirrors are what the harness admin console (web) probes.
export_peer_urls() {
  export VICTORIALOGS_URL LITELLM_URL SYSADMIN_LITELLM_URL
  export VALKEY_HOST VALKEY_PORT
  export SYSADMIN_VALKEY_HOST SYSADMIN_VALKEY_PORT
  export SYSADMIN_SEAWEEDFS_HOST SYSADMIN_SEAWEEDFS_PORT SYSADMIN_SEAWEEDFS_MASTER_PORT
  export SYSADMIN_INFERENCE_LOCAL
  VICTORIALOGS_URL="http://${PEER_DATA_HOST}:${PEER_DATA_LOGS_PORT}"
  LITELLM_URL="http://${PEER_INFERENCE_HOST}:${PEER_INFERENCE_PORT}/v1"
  SYSADMIN_LITELLM_URL="http://${PEER_INFERENCE_HOST}:${PEER_INFERENCE_PORT}/v1"
  VALKEY_HOST="${PEER_DATA_HOST}"
  VALKEY_PORT="${PEER_DATA_VALKEY_PORT}"
  SYSADMIN_VALKEY_HOST="${PEER_DATA_HOST}"
  SYSADMIN_VALKEY_PORT="${PEER_DATA_VALKEY_PORT}"
  SYSADMIN_SEAWEEDFS_HOST="${PEER_DATA_HOST}"
  SYSADMIN_SEAWEEDFS_PORT="${PEER_DATA_SEAWEEDFS_PORT}"
  SYSADMIN_SEAWEEDFS_MASTER_PORT="${SYSADMIN_SEAWEEDFS_MASTER_PORT:-9333}"
  # The inference engine binds loopback on the inference host by design, so a
  # web-host console cannot probe it: it is a local probe only on the roles
  # that actually run the engine.
  if role_owns inference; then
    SYSADMIN_INFERENCE_LOCAL=1
  else
    SYSADMIN_INFERENCE_LOCAL=0
  fi
}
export_peer_urls

# Prefer the installer's isolated vLLM environment when present. Keep an
# explicit operator override intact.
if [ -z "${VLLM_BIN:-}" ] && [ -x "${VLLM_VENV_DIR}/bin/vllm" ]; then
  export VLLM_BIN="${VLLM_VENV_DIR}/bin/vllm"
fi

SERVICE_NAMES="valkey victorialogs audit_outbox seaweedfs inference auth_gateway agent_tools litellm traefik harness_gateway"

is_running() {
  local pid_file="$1"
  if [ -f "$pid_file" ]; then
    local pid
    pid=$(cat "$pid_file" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

start_service() {
  local name="$1"
  local pid_file="${RUN_DIR}/${name}.pid"
  local log_file="${LOGS_DIR}/${name}.log"
  shift

  if is_running "$pid_file"; then
    echo "  [*] ${name} is already running (PID: $(cat "$pid_file"))"
    return 0
  fi

  echo "  [+] Starting ${name}..."
  nohup "$@" > "$log_file" 2>&1 </dev/null &
  local new_pid=$!
  echo "$new_pid" > "$pid_file"
  disown "$new_pid" 2>/dev/null || true
  sleep 0.5

  if kill -0 "$new_pid" 2>/dev/null; then
    echo "      Started ${name} (PID: ${new_pid})"
  else
    echo "      [!] Failed to start ${name}. Check log: ${log_file}"
    tail -n 10 "$log_file"
    return 1
  fi
}

stop_service() {
  local name="$1"
  local pid_file="${RUN_DIR}/${name}.pid"

  if [ -f "$pid_file" ]; then
    local pid
    pid=$(cat "$pid_file" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "  [-] Stopping ${name} (PID: ${pid})..."
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 10); do
        if ! kill -0 "$pid" 2>/dev/null; then
          break
        fi
        sleep 0.2
      done
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
      fi
    fi
    rm -f "$pid_file"
  fi
}

status_service() {
  local name="$1"
  local port="$2"
  local pid_file="${RUN_DIR}/${name}.pid"

  if is_running "$pid_file"; then
    local pid
    pid=$(cat "$pid_file")
    local mem_rss
    mem_rss=$(ps -o rss= -p "$pid" 2>/dev/null | awk '{printf "%.1f MB", $1/1024}' || echo "N/A")
    printf "  %-18s \e[32mRUNNING\e[0m (PID: %-6s | Port: %-5s | RAM: %s)\n" "$name" "$pid" "$port" "$mem_rss"
  else
    printf "  %-18s \e[31mSTOPPED\e[0m (Port: %-5s)\n" "$name" "$port"
  fi
}

# Secrets this role actually needs (docs/multi-host.md §3):
#   all | web | inference -> master.key + valkey-password.key
#   data                  -> valkey-password.key only. The state tier holds no
#                            LiteLLM key by design, so demanding master.key here
#                            made the data host unable to start at all.
role_needs_master_key() {
  case "$ROLE" in all|web|inference) return 0 ;; *) return 1 ;; esac
}

# Interpreter for the small runtime-config render below. `data` installs only
# static binaries (no venv), so fall back to the system python3.
platform_python() {
  if [ -x "$VENV_PYTHON" ]; then
    echo "$VENV_PYTHON"
    return 0
  fi
  command -v python3 || true
}

# Export the platform secrets and regenerate the runtime Valkey configuration.
# Called before any service start because LiteLLM and Valkey need them.
load_secrets() {
  local master_key_file="${CONFIG_DIR}/keys/master.key"
  local valkey_password_file="${CONFIG_DIR}/keys/valkey-password.key"

  if role_needs_master_key; then
    if [ ! -s "$master_key_file" ]; then
      echo "Missing LiteLLM master key: $master_key_file. Run ./install.sh first." >&2
      return 1
    fi
    export LITELLM_MASTER_KEY
    LITELLM_MASTER_KEY="$(cat "$master_key_file")"
  fi

  if [ ! -s "$valkey_password_file" ]; then
    echo "Missing Valkey password: $valkey_password_file." >&2
    echo "Run ./install.sh on the web host, then copy it here (docs/multi-host.md §3)." >&2
    return 1
  fi
  export VALKEY_PASSWORD VALKEY_URL
  VALKEY_PASSWORD="$(cat "$valkey_password_file")"
  # Valkey lives on the data peer (loopback for `all`); the password still
  # comes from this machine's key file (see docs/multi-host.md §key-copy).
  VALKEY_URL="redis://:${VALKEY_PASSWORD}@${PEER_DATA_HOST}:${PEER_DATA_VALKEY_PORT}/0"

  # Only the host that serves Valkey renders the runtime config: every other
  # role reads the checked-in file only for reference and never starts Valkey.
  case "$ROLE" in
    all|data)
      local python_bin
      python_bin="$(platform_python)"
      if [ -z "$python_bin" ]; then
        echo "No python3 interpreter found to render ${RUN_DIR}/valkey.conf." >&2
        return 1
      fi
      VALKEY_CONFIG_SOURCE="${CONFIG_DIR}/valkey/valkey.conf" VALKEY_CONFIG_RUNTIME="${RUN_DIR}/valkey.conf" \
        "$python_bin" -c 'import os, pathlib; src = pathlib.Path(os.environ["VALKEY_CONFIG_SOURCE"]); dst = pathlib.Path(os.environ["VALKEY_CONFIG_RUNTIME"]); dst.write_text(src.read_text().replace("CONFIGURE_VIA_PLATFORM_SH", os.environ["VALKEY_PASSWORD"])); dst.chmod(0o600)'
      ;;
  esac
}

# Start one service by name. Secrets must already be loaded (load_secrets).
start_one() {
  case "${1:-}" in
    valkey)
      start_service "valkey" "${BIN_DIR}/valkey-server" "${RUN_DIR}/valkey.conf"
      ;;
    victorialogs)
      start_service "victorialogs" "${BIN_DIR}/victoria-logs-prod" \
        "-storageDataPath=${DATA_DIR}/victorialogs" \
        "-retentionPeriod=90d" \
        "-httpListenAddr=${LAN_BIND_IP}:9428"
      ;;
    audit_outbox)
      start_service "audit_outbox" "$VENV_PYTHON" "-u" "${SERVICES_DIR}/agent_tools/audit.py" "--worker"
      ;;
    seaweedfs)
      start_service "seaweedfs" "${BIN_DIR}/weed" "server" \
        "-dir=${DATA_DIR}/seaweedfs" \
        "-ip=${LAN_BIND_IP}" \
        "-ip.bind=${LAN_BIND_IP}" \
        "-master.peers=none" \
        "-s3" \
        "-s3.port=8333" \
        "-master.port=9333" \
        "-filer.port=8888" \
        "-volume.port=8085"
      ;;
    inference)
      start_service "inference" "$VENV_PYTHON" "-u" "${SERVICES_DIR}/inference_engine/server.py" 8000
      ;;
    auth_gateway)
      start_service "auth_gateway" "$VENV_PYTHON" "-u" "${SERVICES_DIR}/auth_gateway/server.py" 3081
      ;;
    agent_tools)
      start_service "agent_tools" "$VENV_PYTHON" "-u" "${SERVICES_DIR}/agent_tools/server.py" "$AGENT_PORT"
      ;;
    litellm)
      start_service "litellm" "$VENV_LITELLM" \
        "--config" "${CONFIG_DIR}/litellm/config.yaml" \
        "--port" "4000" \
        "--host" "${LAN_BIND_IP}" \
        "--num_workers" "2"
      ;;
    traefik)
      start_service "traefik" "${BIN_DIR}/traefik" \
        "--configFile=${CONFIG_DIR}/traefik/traefik.yml"
      ;;
    harness_gateway)
      start_harness
      ;;
    *)
      echo "Unknown service: ${1:-<empty>}" >&2
      echo "Known services: ${SERVICE_NAMES}" >&2
      return 1
      ;;
  esac
}

# Listening port per service ("-" when it has none). One table for status
# lines and TCP probes.
service_port() {
  case "${1:-}" in
    valkey) echo "6379" ;;
    victorialogs) echo "9428" ;;
    audit_outbox) echo "-" ;;
    seaweedfs) echo "8333" ;;
    inference) echo "8000" ;;
    auth_gateway) echo "3081" ;;
    agent_tools) echo "$AGENT_PORT" ;;
    litellm) echo "4000" ;;
    traefik) echo "8080" ;;
    harness_gateway) echo "3085" ;;
    *) return 1 ;;
  esac
}

# Print one service's status line by name.
status_one() {
  local port
  if ! port="$(service_port "${1:-}")"; then
    echo "Unknown service: ${1:-<empty>} (known: ${SERVICE_NAMES})" >&2
    return 1
  fi
  status_service "$1" "$port"
}

# The gateway deliberately leaves its harness children running on SIGTERM so a
# restarted gateway re-adopts them. A full platform stop reaps them from the
# gateway's registry instead.
reap_harness_instances() {
  local registry_dir="${HARNESS_STATE_DIR}/instances"
  [ -d "$registry_dir" ] || return 0
  local python_bin="$VENV_PYTHON"
  if [ ! -x "$python_bin" ]; then
    python_bin="$(command -v python3 || true)"
  fi
  [ -n "$python_bin" ] || return 0
  local file pid
  for file in "$registry_dir"/*.json; do
    [ -e "$file" ] || continue
    pid=$("$python_bin" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("pid") or "")' "$file" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "  [-] Stopping harness instance (PID: ${pid})..."
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do
        if ! kill -0 "$pid" 2>/dev/null; then
          break
        fi
        sleep 0.2
      done
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
      fi
    fi
    rm -f "$file"
  done
}

start_all() {
  umask 077
  load_secrets

  echo "=========================================================="
  echo " Starting Sysadmin AI Platform Backend (role: ${ROLE})"
  echo "=========================================================="

  local svc
  for svc in $SERVICE_START_ORDER; do
    role_owns "$svc" || continue
    start_one "$svc"
  done

  echo "=========================================================="
  echo " Role '${ROLE}' services launched. Run './platform.sh status'."
  echo "=========================================================="
}

stop_all() {
  echo "Stopping Sysadmin AI Platform services (role: ${ROLE})..."
  # Reverse start order. On web/all the harness gateway is stopped first and
  # its per-user children reaped from the registry; on other roles these are
  # no-ops.
  local svc
  for svc in harness_gateway traefik litellm agent_tools auth_gateway inference seaweedfs audit_outbox victorialogs valkey; do
    role_owns "$svc" || continue
    stop_service "$svc"
    [ "$svc" = "harness_gateway" ] && reap_harness_instances
  done
  echo "All '${ROLE}' services stopped."
}

# Peer services this role consumes but does not host. They are reported so an
# operator can tell "the tier is down" from "this host owns it elsewhere".
peer_targets() {
  case "$ROLE" in
    web)
      echo "litellm(inference) ${PEER_INFERENCE_HOST}:${PEER_INFERENCE_PORT}"
      echo "valkey(data) ${PEER_DATA_HOST}:${PEER_DATA_VALKEY_PORT}"
      echo "seaweedfs(data) ${PEER_DATA_HOST}:${PEER_DATA_SEAWEEDFS_PORT}"
      echo "victorialogs(data) ${PEER_DATA_HOST}:${PEER_DATA_LOGS_PORT}"
      ;;
    inference)
      echo "valkey(data) ${PEER_DATA_HOST}:${PEER_DATA_VALKEY_PORT}"
      echo "victorialogs(data) ${PEER_DATA_HOST}:${PEER_DATA_LOGS_PORT}"
      ;;
  esac
}

show_status() {
  echo "=== Sysadmin AI Platform Service Status (role: ${ROLE}) ==="
  local svc
  for svc in traefik litellm agent_tools auth_gateway inference seaweedfs audit_outbox victorialogs valkey harness_gateway; do
    role_owns "$svc" || continue
    status_one "$svc"
  done
  if [ "$ROLE" != "all" ]; then
    local target
    peer_targets | while read -r target; do
      [ -n "$target" ] || continue
      printf "  %-18s peer %s\n" "${target%% *}" "${target#* }"
    done
  fi
  echo "==========================================="
}

# The custom DeepSeek Harness multi-user gateway (packages/harness-integration).
# It is opt-in rather than part of start_all because it needs the `dsh` CLI and
# boots one harness process per logged-in sysadmin on demand.
start_harness() {
  local node_bin dsh_bin
  node_bin="$(command -v node || true)"
  if [ -z "$node_bin" ]; then
    echo "  [!] node is required to run the harness gateway" >&2
    return 1
  fi
  if [ ! -f "${HARNESS_DIR}/gateway/server.js" ]; then
    echo "  [!] harness gateway not found at ${HARNESS_DIR}/gateway/server.js" >&2
    return 1
  fi
  dsh_bin="${DSH_BIN:-}"
  if [ -z "$dsh_bin" ]; then
    # Prefer the stable global install over an ephemeral npx cache.
    dsh_bin="$(command -v dsh 2>/dev/null || true)"
  fi
  if [ -z "$dsh_bin" ] || { ! command -v "$dsh_bin" >/dev/null 2>&1 && [ ! -x "$dsh_bin" ]; }; then
    echo "  [!] '${DSH_BIN:-dsh}' not found. Install @deepseek-ai/dsh globally or set DSH_BIN to the launcher." >&2
    return 1
  fi
  export DSH_BIN="$dsh_bin"
  export SYSADMIN_BACKEND_ROOT="${ROOT_DIR}"
  # The harness plugin and gateway talk to the agent platform on the port this
  # host actually uses (3080 by default, SYSADMIN_AGENT_PORT when overridden).
  export SYSADMIN_BACKEND_URL="${SYSADMIN_BACKEND_URL:-http://127.0.0.1:${AGENT_PORT}}"
  start_service "harness_gateway" "$node_bin" "${HARNESS_DIR}/gateway/server.js"
}

# Start, stop, restart or inspect one service.
service_cmd() {
  local name="${1:-}"
  local action="${2:-status}"
  case " ${SERVICE_NAMES} " in
    *" ${name} "*) ;;
    *)
      echo "Unknown service: ${name:-<empty>}" >&2
      echo "Known services: ${SERVICE_NAMES}" >&2
      return 1
      ;;
  esac

  case "$action" in
    start)
      if [ "$name" = "harness_gateway" ]; then
        start_harness
      else
        load_secrets
        start_one "$name"
      fi
      ;;
    stop)
      stop_service "$name"
      if [ "$name" = "harness_gateway" ]; then
        reap_harness_instances
      fi
      ;;
    restart)
      if [ "$name" = "harness_gateway" ]; then
        stop_service "$name"
        reap_harness_instances
        start_harness
      else
        load_secrets
        stop_service "$name"
        start_one "$name"
      fi
      ;;
    status)
      status_one "$name"
      ;;
    *)
      echo "Usage: $0 service <name> {start|stop|restart|status}" >&2
      return 1
      ;;
  esac
}

show_logs() {
  local target="${1:-all}"
  if [ "$target" = "all" ]; then
    tail -f "${LOGS_DIR}/"*.log
  else
    if [ -f "${LOGS_DIR}/${target}.log" ]; then
      tail -f "${LOGS_DIR}/${target}.log"
    else
      echo "No log found for '${target}'. Available:"
      ls -1 "${LOGS_DIR}"
    fi
  fi
}

# Wait for one host:port to accept TCP. Uses the same interpreter selection as
# load_secrets, so the data role (no venv) can still probe its own listeners.
wait_for_port() {
  local host="$1" port="$2"
  [ "$port" = "-" ] && return 0
  local python_bin
  python_bin="$(platform_python)"
  [ -n "$python_bin" ] || return 1
  local _
  for _ in $(seq 1 10); do
    if "$python_bin" -c "import socket; s = socket.socket(); s.settimeout(0.5); exit(s.connect_ex(('$host', $port)))" 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

run_tests() {
  # The end-to-end suite drives the user-facing HTTP path, which only the web
  # role (or a single host) serves.
  if ! role_owns traefik && ! role_owns auth_gateway; then
    echo "Role '${ROLE}' runs no application services; run './platform.sh test' on the web host." >&2
    return 1
  fi
  if [ ! -x "$VENV_PYTHON" ]; then
    echo "Missing ${VENV_PYTHON}; run ./install.sh on this host first." >&2
    return 1
  fi

  local auto_started=false
  local svc
  for svc in $(role_services); do
    if ! is_running "${RUN_DIR}/${svc}.pid"; then
      auto_started=true
      break
    fi
  done
  if [ "$auto_started" = true ]; then
    echo "Services not running. Starting this role's services for testing..."
    start_all
    sleep 4
    # Wait for local listeners, then for the peer services this role reaches
    # out to (they live on other machines in the multi-host topology).
    for svc in $SERVICE_START_ORDER; do
      role_owns "$svc" || continue
      wait_for_port "127.0.0.1" "$(service_port "$svc")" || true
    done
    case "$ROLE" in
      web)
        wait_for_port "$PEER_INFERENCE_HOST" "$PEER_INFERENCE_PORT" || true
        wait_for_port "$PEER_DATA_HOST" "$PEER_DATA_VALKEY_PORT" || true
        wait_for_port "$PEER_DATA_HOST" "$PEER_DATA_SEAWEEDFS_PORT" || true
        wait_for_port "$PEER_DATA_HOST" "$PEER_DATA_LOGS_PORT" || true
        ;;
      inference)
        wait_for_port "$PEER_DATA_HOST" "$PEER_DATA_VALKEY_PORT" || true
        wait_for_port "$PEER_DATA_HOST" "$PEER_DATA_LOGS_PORT" || true
        ;;
    esac
  fi

  echo "Running end-to-end backend verification test suite..."
  local test_exit=0
  "$VENV_PYTHON" "${ROOT_DIR}/tests/test_platform.py" || test_exit=$?

  if [ "$auto_started" = true ]; then
    echo "Stopping test-spawned services..."
    stop_all
  fi
  return $test_exit
}

# Print which services each role starts, for planning and for install.sh's
# dry run. The role of the *current* host is marked.
show_role_matrix() {
  local role
  for role in all web inference data; do
    local marker=" "
    [ "$role" = "$ROLE" ] && marker="*"
    printf " %s %-10s %s\n" "$marker" "$role" "$(ROLE="$role" role_services)"
  done
}

case "${1:-status}" in
  start)
    start_all
    ;;
  stop)
    stop_all
    ;;
  restart)
    stop_all
    sleep 1
    start_all
    ;;
  status)
    show_status
    ;;
  service)
    service_cmd "${2:-}" "${3:-status}"
    ;;
  logs)
    show_logs "${2:-all}"
    ;;
  test)
    run_tests
    ;;
  dashboard)
    "$VENV_PYTHON" "${ROOT_DIR}/platform_tui.py"
    ;;
  survey)
    "$VENV_PYTHON" "${ROOT_DIR}/services/hardware_survey.py"
    ;;
  chat)
    "$VENV_PYTHON" "${ROOT_DIR}/sysadmin_cli.py" "${@:2}"
    ;;
  harness)
    start_harness
    ;;
  harness-stop)
    stop_service "harness_gateway"
    reap_harness_instances
    ;;
  roles)
    show_role_matrix
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|service <name> {start|stop|restart|status}|harness|harness-stop|dashboard|survey|chat|logs [service]|test|roles}"
    exit 1
    ;;
esac
