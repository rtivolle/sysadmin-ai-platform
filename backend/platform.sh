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
HARNESS_DIR="${ROOT_DIR}/../packages/harness-integration"

# The agent platform defaults to 3080. On a host where the DeepSeek Harness web
# surface already owns that port, export SYSADMIN_AGENT_PORT=<free port> before
# `platform.sh start` and point the Traefik agent-service route at the same
# port (see docs/status/TEST_READY.md).
AGENT_PORT="${SYSADMIN_AGENT_PORT:-3080}"
HARNESS_STATE_DIR="${DATA_DIR}/harness"

mkdir -p "$LOGS_DIR" "$RUN_DIR" "$DATA_DIR/valkey" "$DATA_DIR/seaweedfs" "$DATA_DIR/victorialogs"

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

# Export the platform secrets and regenerate the runtime Valkey configuration.
# Called before any service start because LiteLLM and Valkey need them.
load_secrets() {
  local master_key_file="${CONFIG_DIR}/keys/master.key"
  local valkey_password_file="${CONFIG_DIR}/keys/valkey-password.key"
  if [ ! -s "$master_key_file" ]; then
    echo "Missing LiteLLM master key: $master_key_file. Run ./install.sh first." >&2
    return 1
  fi
  if [ ! -s "$valkey_password_file" ]; then
    echo "Missing Valkey password: $valkey_password_file. Run ./install.sh first." >&2
    return 1
  fi
  export LITELLM_MASTER_KEY
  LITELLM_MASTER_KEY="$(cat "$master_key_file")"
  export VALKEY_PASSWORD VALKEY_URL
  VALKEY_PASSWORD="$(cat "$valkey_password_file")"
  VALKEY_URL="redis://:${VALKEY_PASSWORD}@127.0.0.1:6379/0"
  VALKEY_CONFIG_SOURCE="${CONFIG_DIR}/valkey/valkey.conf" VALKEY_CONFIG_RUNTIME="${RUN_DIR}/valkey.conf" \
    "$VENV_PYTHON" -c 'import os, pathlib; src = pathlib.Path(os.environ["VALKEY_CONFIG_SOURCE"]); dst = pathlib.Path(os.environ["VALKEY_CONFIG_RUNTIME"]); dst.write_text(src.read_text().replace("CONFIGURE_VIA_PLATFORM_SH", os.environ["VALKEY_PASSWORD"])); dst.chmod(0o600)'
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
        "-httpListenAddr=127.0.0.1:9428"
      ;;
    audit_outbox)
      start_service "audit_outbox" "$VENV_PYTHON" "-u" "${SERVICES_DIR}/agent_tools/audit.py" "--worker"
      ;;
    seaweedfs)
      start_service "seaweedfs" "${BIN_DIR}/weed" "server" \
        "-dir=${DATA_DIR}/seaweedfs" \
        "-ip=127.0.0.1" \
        "-ip.bind=127.0.0.1" \
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
        "--host" "127.0.0.1" \
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

# Print one service's status line by name.
status_one() {
  case "${1:-}" in
    valkey) status_service "valkey" "6379" ;;
    victorialogs) status_service "victorialogs" "9428" ;;
    audit_outbox) status_service "audit_outbox" "-" ;;
    seaweedfs) status_service "seaweedfs" "8333" ;;
    inference) status_service "inference" "8000" ;;
    auth_gateway) status_service "auth_gateway" "3081" ;;
    agent_tools) status_service "agent_tools" "$AGENT_PORT" ;;
    litellm) status_service "litellm" "4000" ;;
    traefik) status_service "traefik" "8080" ;;
    harness_gateway) status_service "harness_gateway" "3085" ;;
    *)
      echo "Unknown service: ${1:-<empty>} (known: ${SERVICE_NAMES})" >&2
      return 1
      ;;
  esac
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
  echo " Starting Sysadmin AI Platform Backend (Zero-Docker Stack)"
  echo "=========================================================="

  # 1. Valkey (Fast memory & state store)
  start_one valkey
  # 2. VictoriaLogs (Forensic audit logs database)
  start_one victorialogs
  # Replay audit events buffered while VictoriaLogs was unavailable.
  start_one audit_outbox
  # 3. SeaweedFS (Local S3 object storage & filer)
  start_one seaweedfs
  # 4. Inference Engine (Local mock / upstream vLLM router)
  start_one inference
  # 5. Auth Gateway (Traefik ForwardAuth adapter)
  start_one auth_gateway
  # 6. Agent Tools Platform (Sandboxed tools, approval gate & audit)
  start_one agent_tools
  # 7. LiteLLM Proxy (Token quotas, rate limits, virtual keys)
  start_one litellm
  # 8. Traefik Reverse Proxy (TLS termination & routing)
  start_one traefik

  echo "=========================================================="
  echo " All services launched. Run './platform.sh status' to inspect."
  echo "=========================================================="
}

stop_all() {
  echo "Stopping all Sysadmin AI Platform services..."
  # The harness gateway owns per-user dsh children; stop it before their backend
  # and reap the instances it left running for re-adoption.
  stop_service "harness_gateway"
  reap_harness_instances
  stop_service "traefik"
  stop_service "litellm"
  stop_service "agent_tools"
  stop_service "auth_gateway"
  stop_service "inference"
  stop_service "seaweedfs"
  stop_service "audit_outbox"
  stop_service "victorialogs"
  stop_service "valkey"
  echo "All services stopped."
}

show_status() {
  echo "=== Sysadmin AI Platform Service Status ==="
  status_one traefik
  status_one litellm
  status_one agent_tools
  status_one auth_gateway
  status_one inference
  status_one seaweedfs
  status_one audit_outbox
  status_one victorialogs
  status_one valkey
  status_one harness_gateway
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

run_tests() {
  local auto_started=false
  if ! is_running "${RUN_DIR}/traefik.pid" || ! is_running "${RUN_DIR}/valkey.pid"; then
    echo "Services not running. Starting all backend services for testing..."
    start_all
    auto_started=true
    sleep 4
    for port in 6379 9428 8333 8000 3081 "$AGENT_PORT" 4000 8080; do
      for _ in $(seq 1 10); do
        if "$VENV_PYTHON" -c "import socket; s = socket.socket(); s.settimeout(0.5); exit(s.connect_ex(('127.0.0.1', $port)))" 2>/dev/null; then
          break
        fi
        sleep 0.5
      done
    done
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
    "$VENV_PYTHON" "${ROOT_DIR}/sysadmin_cli.py"
    ;;
  harness)
    start_harness
    ;;
  harness-stop)
    stop_service "harness_gateway"
    reap_harness_instances
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|service <name> {start|stop|restart|status}|harness|harness-stop|dashboard|survey|chat|logs [service]|test}"
    exit 1
    ;;
esac
