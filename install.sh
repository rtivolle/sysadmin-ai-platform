#!/usr/bin/env bash
# ==============================================================================
# One-Command Installer & Configurator for Sysadmin AI Platform Backend
# Philosophy: Minimal Overhead, Native Static Binaries, Zero Docker Bloat
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${SCRIPT_DIR}/backend"
BIN_DIR="${BACKEND_DIR}/bin"
CONFIG_DIR="${BACKEND_DIR}/config"
DATA_DIR="${BACKEND_DIR}/data"
LOGS_DIR="${BACKEND_DIR}/logs"
RUN_DIR="${BACKEND_DIR}/run"
VENV_DIR="${BACKEND_DIR}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python3"
VLLM_VENV_DIR="${VLLM_VENV_DIR:-${BACKEND_DIR}/.vllm-venv}"

# GPU-only workflow does not provision platform state or credentials.
if [ "${1:-}" = "--nvidia" ]; then
  shift
  exec python3 "${BACKEND_DIR}/scripts/nvidia_setup.py" "$@"
fi
if [ "${1:-}" = "--help" ]; then
  echo "Usage: ./install.sh [--role all|web|inference|data] [--skip-vllm] [--vllm-version VERSION] [--peer-* ...]"
  echo "  --role all        one machine: every service, all binds loopback (default)"
  echo "  --role web        application tier; needs --lan-bind-ip, --peer-inference, --peer-data"
  echo "  --role inference  GPU tier (LiteLLM + engine); needs --lan-bind-ip, --peer-data"
  echo "  --role data       state tier (Valkey, VictoriaLogs, SeaweedFS); needs --lan-bind-ip"
  echo "  --dry-run         resolve and validate the role, print the plan, change nothing"
  echo "  Without --role the role recorded in backend/config/roles/deployment.env is reused;"
  echo "  --role all switches a split host back to all-in-one. See docs/multi-host.md."
  echo "GPU setup: ./install.sh --nvidia [--apply] [--driver auto|BRANCH] [--cuda-toolkit MAJOR-MINOR]"
  echo "Wizard: ./install.sh --tui [--unattended] [--role ... --lan-bind-ip ... --peer-* ...]"
  echo "Inventory: ./install.sh --survey"
  exit 0
fi
SKIP_VLLM=0
DRY_RUN=0
VLLM_VERSION="${VLLM_VERSION:-}"

# Check CLI flags
if [ "${1:-}" = "--survey" ]; then
  echo "Running hardware survey..."
  if [ -x "$VENV_PYTHON" ]; then
    exec "$VENV_PYTHON" "${BACKEND_DIR}/services/hardware_survey.py"
  else
    exec python3 "${BACKEND_DIR}/services/hardware_survey.py"
  fi
fi

if [ "${1:-}" = "--tui" ] || [ "${1:-}" = "-i" ]; then
  # Ensure python venv exists for TUI wizard
  if [ ! -x "$VENV_PYTHON" ]; then
    echo "Bootstrapping environment for TUI wizard..."
    python3 -m venv "${VENV_DIR}"
    "${VENV_DIR}/bin/pip" install -q "litellm[proxy]" fastapi uvicorn httpx pyyaml redis pydantic rich huggingface_hub
  fi
  shift
  exec "$VENV_PYTHON" "${BACKEND_DIR}/installer_tui.py" "$@"
fi

# ---- Multi-host role flags (unattended; PR-H1) ------------------------------
# The machine role is a three-way choice the operator makes at install time:
#
#   --role all        one machine, everything local, all binds loopback
#   --role web        application tier (Traefik, ForwardAuth, agent platform,
#                     sandbox, harness gateway, workspaces)
#   --role inference  GPU tier (LiteLLM + inference engine + vLLM)
#   --role data       state tier (Valkey, VictoriaLogs, SeaweedFS, backups)
#
# Without --role the installer reuses the role recorded in
# backend/config/roles/deployment.env, so ./update.sh (which calls the installer
# with no flags) never silently re-roles a machine; with neither the flag nor a
# recorded role the host is single-host (all). Passing --role all on a host that
# was web/inference/data switches it back to all-in-one: the recorded peers are
# reset to loopback, the role-dependent files are re-rendered to their
# single-host form, and only then is anything installed.
# The interactive TUI asks the same questions (installer_tui.py role step).
ROLE=""
LAN_BIND_IP=""
PEER_INFERENCE_HOST=""
PEER_INFERENCE_PORT=""
PEER_DATA_HOST=""
PEER_DATA_VALKEY_PORT=""
PEER_DATA_LOGS_PORT=""
PEER_DATA_SEAWEEDFS_PORT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-vllm) SKIP_VLLM=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --vllm-version) VLLM_VERSION="${2:?--vllm-version requires a version}"; shift 2 ;;
    --role)                  ROLE="${2:?--role requires all|web|inference|data}"; shift 2 ;;
    --lan-bind-ip)           LAN_BIND_IP="${2:?--lan-bind-ip requires an address}"; shift 2 ;;
    --peer-inference)        PEER_INFERENCE_HOST="${2:?--peer-inference requires an address}"; shift 2 ;;
    --peer-inference-port)   PEER_INFERENCE_PORT="${2:?--peer-inference-port requires a port}"; shift 2 ;;
    --peer-data)             PEER_DATA_HOST="${2:?--peer-data requires an address}"; shift 2 ;;
    --peer-data-valkey-port)     PEER_DATA_VALKEY_PORT="${2:?--peer-data-valkey-port requires a port}"; shift 2 ;;
    --peer-data-logs-port)       PEER_DATA_LOGS_PORT="${2:?--peer-data-logs-port requires a port}"; shift 2 ;;
    --peer-data-seaweedfs-port)  PEER_DATA_SEAWEEDFS_PORT="${2:?--peer-data-seaweedfs-port requires a port}"; shift 2 ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Usage: $0 [--role all|web|inference|data] [--peer-* ...] [--lan-bind-ip IP]" >&2
      exit 2
      ;;
  esac
done

if [ -n "$VLLM_VERSION" ] && ! [[ "$VLLM_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([a-zA-Z0-9.+-]*)$ ]]; then
  echo "Invalid vLLM version" >&2
  exit 2
fi

DEPLOYMENT_ENV_FILE="${CONFIG_DIR}/roles/deployment.env"

# Read one key from the recorded deployment.env. Parsed, never sourced: this
# file is machine state, not code.
recorded_env() {
  local key="$1"
  [ -f "$DEPLOYMENT_ENV_FILE" ] || return 0
  sed -n "s/^$key=//p" "$DEPLOYMENT_ENV_FILE" | tail -n 1
}

# Explicit flag > recorded value > built-in default.
resolve_value() {
  if [ -n "$1" ]; then echo "$1"; return 0; fi
  if [ -n "$2" ]; then echo "$2"; return 0; fi
  echo "$3"
}

is_loopback_address() {
  case "$1" in
    ""|127.*|localhost|::1) return 0 ;;
    *) return 1 ;;
  esac
}

fail_closed() {
  echo "  [!] $1" >&2
  exit 2
}

RECORDED_ROLE="$(recorded_env ROLE)"
if [ -z "$ROLE" ]; then
  if [ -n "$RECORDED_ROLE" ]; then
    ROLE="$RECORDED_ROLE"
    echo "  [i] Reusing the recorded machine role '${ROLE}' from ${DEPLOYMENT_ENV_FILE}."
    echo "      Pass --role all|web|inference|data to change it."
  else
    ROLE="all"
  fi
fi

case "$ROLE" in
  all|web|inference|data) ;;
  *)
    echo "Invalid role: ${ROLE} (expected all|web|inference|data)" >&2
    exit 2
    ;;
esac

LAN_BIND_IP="$(resolve_value "$LAN_BIND_IP" "$(recorded_env LAN_BIND_IP)" "127.0.0.1")"
PEER_INFERENCE_HOST="$(resolve_value "$PEER_INFERENCE_HOST" "$(recorded_env PEER_INFERENCE_HOST)" "127.0.0.1")"
PEER_INFERENCE_PORT="$(resolve_value "$PEER_INFERENCE_PORT" "$(recorded_env PEER_INFERENCE_PORT)" "4000")"
PEER_DATA_HOST="$(resolve_value "$PEER_DATA_HOST" "$(recorded_env PEER_DATA_HOST)" "127.0.0.1")"
PEER_DATA_VALKEY_PORT="$(resolve_value "$PEER_DATA_VALKEY_PORT" "$(recorded_env PEER_DATA_VALKEY_PORT)" "6379")"
PEER_DATA_LOGS_PORT="$(resolve_value "$PEER_DATA_LOGS_PORT" "$(recorded_env PEER_DATA_LOGS_PORT)" "9428")"
PEER_DATA_SEAWEEDFS_PORT="$(resolve_value "$PEER_DATA_SEAWEEDFS_PORT" "$(recorded_env PEER_DATA_SEAWEEDFS_PORT)" "8333")"

if [ "$ROLE" = "all" ]; then
  # Single host: every address is loopback, whatever an earlier role recorded.
  # A stale peer address here would make the local services talk to a machine
  # that is no longer part of the deployment.
  LAN_BIND_IP="127.0.0.1"
  PEER_INFERENCE_HOST="127.0.0.1"
  PEER_INFERENCE_PORT="4000"
  PEER_DATA_HOST="127.0.0.1"
  PEER_DATA_VALKEY_PORT="6379"
  PEER_DATA_LOGS_PORT="9428"
  PEER_DATA_SEAWEEDFS_PORT="8333"
else
  # A split deployment talking to itself on loopback is the failure this
  # installer must not allow: peers would be unreachable, and on data Valkey
  # would bind an interface nobody can reach. Fail closed before applying.
  if is_loopback_address "$LAN_BIND_IP"; then
    fail_closed "Role '${ROLE}' needs this machine's LAN address: pass --lan-bind-ip <ip>."
  fi
  if [ "$ROLE" = "web" ]; then
    if is_loopback_address "$PEER_INFERENCE_HOST"; then
      fail_closed "Role 'web' needs the inference host: pass --peer-inference <ip>."
    fi
    if is_loopback_address "$PEER_DATA_HOST"; then
      fail_closed "Role 'web' needs the data host: pass --peer-data <ip>."
    fi
  elif [ "$ROLE" = "inference" ]; then
    if is_loopback_address "$PEER_DATA_HOST"; then
      fail_closed "Role 'inference' needs the data host: pass --peer-data <ip>."
    fi
  fi
fi

# Plan preview: resolve and validate the role, print what would happen, and
# touch nothing. This is how a three-machine plan is checked before any peer is
# up, and how the role decision itself is regression-tested.
if [ "$DRY_RUN" = 1 ]; then
  echo "  [i] Dry run: nothing is written, installed or started."
  echo "      role:        ${ROLE}"
  echo "      lan-bind-ip: ${LAN_BIND_IP}"
  echo "      inference:   ${PEER_INFERENCE_HOST}:${PEER_INFERENCE_PORT}"
  echo "      data:        ${PEER_DATA_HOST} (valkey ${PEER_DATA_VALKEY_PORT}, logs ${PEER_DATA_LOGS_PORT}, seaweedfs ${PEER_DATA_SEAWEEDFS_PORT})"
  if [ -x "${BACKEND_DIR}/platform.sh" ]; then
    echo "      services per role:"
    bash "${BACKEND_DIR}/platform.sh" roles 2>/dev/null || true
  fi
  if [ "$ROLE" = "all" ]; then
    echo "      next: ./install.sh --role all   # single host, no peer flags"
  else
    echo "      next: ./install.sh --role ${ROLE} --lan-bind-ip ${LAN_BIND_IP} \\"
    echo "              --peer-inference ${PEER_INFERENCE_HOST} --peer-data ${PEER_DATA_HOST}"
  fi
  exit 0
fi

# Fail closed on a broken peer link BEFORE anything is written: the check runs
# against the resolved values (not a file), so a failure leaves the machine
# exactly as it was — not even a new deployment.env.
if [ "$ROLE" != "all" ]; then
  echo "  [i] Pre-apply connectivity check to required peer ports..."
  if ! ROLE="$ROLE" LAN_BIND_IP="$LAN_BIND_IP" \
       PEER_INFERENCE_HOST="$PEER_INFERENCE_HOST" PEER_INFERENCE_PORT="$PEER_INFERENCE_PORT" \
       PEER_DATA_HOST="$PEER_DATA_HOST" PEER_DATA_VALKEY_PORT="$PEER_DATA_VALKEY_PORT" \
       PEER_DATA_LOGS_PORT="$PEER_DATA_LOGS_PORT" PEER_DATA_SEAWEEDFS_PORT="$PEER_DATA_SEAWEEDFS_PORT" \
       python3 "${CONFIG_DIR}/roles/connectivity_check.py"; then
    echo "  [!] Connectivity check failed; nothing was applied." >&2
    exit 1
  fi
fi

# Record the decision (every role, including all) after the checks passed.
mkdir -p "${CONFIG_DIR}/roles"
cat > "$DEPLOYMENT_ENV_FILE" <<EOF
# Generated by install.sh (PR-H1). Machine-specific; git-ignored.
# Written for every role: ROLE=all means "one machine, everything loopback".
ROLE=${ROLE}
LAN_BIND_IP=${LAN_BIND_IP}
PEER_INFERENCE_HOST=${PEER_INFERENCE_HOST}
PEER_INFERENCE_PORT=${PEER_INFERENCE_PORT}
PEER_DATA_HOST=${PEER_DATA_HOST}
PEER_DATA_VALKEY_PORT=${PEER_DATA_VALKEY_PORT}
PEER_DATA_LOGS_PORT=${PEER_DATA_LOGS_PORT}
PEER_DATA_SEAWEEDFS_PORT=${PEER_DATA_SEAWEEDFS_PORT}
EOF
echo "  [i] Wrote ${DEPLOYMENT_ENV_FILE} (role=${ROLE})"

# Re-render the role-dependent configs (valkey bind on data, Traefik upstreams
# on web) before anything is installed. For all this reverts the rendering of a
# previous split, so switching a machine back to all-in-one is a real choice and
# not a one-way door.
if ! python3 "${CONFIG_DIR}/roles/render_config.py" "$DEPLOYMENT_ENV_FILE"; then
  echo "  [!] Failed to render role configuration for role '${ROLE}'." >&2
  exit 1
fi

echo "===================================================================="
echo "    Sysadmin AI Platform - One-Command Backend Installer & Config   "
echo "===================================================================="
echo "Timestamp: $(date -u '+%Y-%m-%d %H:%M:%SZ')"
echo "Host OS:   $(uname -s) $(uname -r) ($(uname -m))"
echo "Target:    Ultra-Low Overhead Native Deployment (Zero-Docker)"
echo "--------------------------------------------------------------------"

# 1. Directory Structure
echo "[1/6] Initializing directory hierarchy..."
mkdir -p "${BIN_DIR}" \
         "${CONFIG_DIR}"/{traefik,valkey,litellm,seaweedfs,victorialogs,sandbox,keys} \
         "${LOGS_DIR}" "${RUN_DIR}"

# State directories are role-scoped: data hosts Valkey/SeaweedFS/VictoriaLogs,
# web hosts the workspaces; `all` hosts both.
case "$ROLE" in
  all|data)
    mkdir -p "${DATA_DIR}"/{valkey,seaweedfs,victorialogs,runbooks,logs}
    ;;
  *)
    mkdir -p "${DATA_DIR}"/runbooks
    ;;
esac

if [ "$ROLE" = "all" ] || [ "$ROLE" = "web" ]; then
  # Per-user workspaces (sysadmin-01 to sysadmin-10) with 0700 permissions
  for i in $(seq -w 1 10); do
    mkdir -p "${DATA_DIR}/workspaces/sysadmin-${i}"
    chmod 700 "${DATA_DIR}/workspaces/sysadmin-${i}"
  done
  mkdir -p "${DATA_DIR}/workspaces/emergency-p1-oncall"
  chmod 700 "${DATA_DIR}/workspaces/emergency-p1-oncall"
  echo "  [+] Initialized directories and 10 isolated workspaces (mode 0700)."
else
  echo "  [+] Initialized directories (role=${ROLE})."
fi

# 2. Bubblewrap Verification (sandbox runs on web only)
echo "[2/6] Verifying Linux Kernel Sandboxing (Bubblewrap)..."
if [ "$ROLE" = "all" ] || [ "$ROLE" = "web" ]; then
  if ! command -v bwrap >/dev/null 2>&1; then
    echo "  [!] bwrap not found in PATH. Checking /usr/bin/bwrap..."
    if [ -x "/usr/bin/bwrap" ]; then
      echo "  [+] Found /usr/bin/bwrap."
    else
      echo "  [!] Bubblewrap is required for secure sandbox execution."
      echo "      Install via: sudo apt-get install -y bubblewrap"
      exit 1
    fi
  else
    echo "  [+] Bubblewrap available: $(which bwrap)"
  fi
else
  echo "  [i] Skipping Bubblewrap check (sandbox runs on web only)."
fi

# 3. Native Static Binaries (Zero Docker Daemon overhead)
echo "[3/6] Installing native static Go & C binaries (role=${ROLE})..."

# 3.1 Traefik (Reverse Proxy & ForwardAuth Router) — web/all only
if [ "$ROLE" = "all" ] || [ "$ROLE" = "web" ]; then
  if [ ! -x "${BIN_DIR}/traefik" ]; then
    echo "  [+] Downloading Traefik static binary..."
    TMP_TAR="/tmp/traefik_install.tar.gz"
    curl -sSL "https://github.com/traefik/traefik/releases/download/v3.7.13/traefik_v3.7.13_linux_amd64.tar.gz" -o "$TMP_TAR"
    tar -xzf "$TMP_TAR" -C "${BIN_DIR}" traefik
    rm -f "$TMP_TAR"
    chmod +x "${BIN_DIR}/traefik"
    echo "      Installed Traefik: $(${BIN_DIR}/traefik version | head -n 1)"
  else
    echo "  [*] Traefik already installed in ${BIN_DIR}/traefik"
  fi
else
  echo "  [i] Skipping Traefik (web/all only)."
fi

# 3.2 VictoriaLogs (High-Efficiency Audit Logs Engine) — data/all only
if [ "$ROLE" = "all" ] || [ "$ROLE" = "data" ]; then
  if [ ! -x "${BIN_DIR}/victoria-logs-prod" ]; then
    echo "  [+] Downloading VictoriaLogs static binary..."
    TMP_TAR="/tmp/vl_install.tar.gz"
    curl -sSL "https://github.com/VictoriaMetrics/VictoriaLogs/releases/download/v1.52.0/victoria-logs-linux-amd64-v1.52.0.tar.gz" -o "$TMP_TAR"
    tar -xzf "$TMP_TAR" -C "${BIN_DIR}" victoria-logs-prod
    rm -f "$TMP_TAR"
    chmod +x "${BIN_DIR}/victoria-logs-prod"
    echo "      Installed VictoriaLogs: $(${BIN_DIR}/victoria-logs-prod --version 2>&1 | head -n 1)"
  else
    echo "  [*] VictoriaLogs already installed in ${BIN_DIR}/victoria-logs-prod"
  fi
else
  echo "  [i] Skipping VictoriaLogs (data/all only)."
fi

# 3.3 SeaweedFS (Local S3 Object Storage & Filer) — data/all only
if [ "$ROLE" = "all" ] || [ "$ROLE" = "data" ]; then
  if [ ! -x "${BIN_DIR}/weed" ]; then
    echo "  [+] Downloading SeaweedFS static binary..."
    TMP_TAR="/tmp/weed_install.tar.gz"
    curl -sSL "https://github.com/seaweedfs/seaweedfs/releases/download/4.47/linux_amd64.tar.gz" -o "$TMP_TAR"
    tar -xzf "$TMP_TAR" -C "${BIN_DIR}" weed
    rm -f "$TMP_TAR"
    chmod +x "${BIN_DIR}/weed"
    echo "      Installed SeaweedFS: $(${BIN_DIR}/weed version 2>&1 | head -n 1)"
  else
    echo "  [*] SeaweedFS already installed in ${BIN_DIR}/weed"
  fi
else
  echo "  [i] Skipping SeaweedFS (data/all only)."
fi

# 3.4 Valkey (Memory Cache & Quotas) — data/all only
# Resolution order: PATH binary, Linuxbrew prefix, brew install, the official
# prebuilt binary tarball from download.valkey.io (sha256-verified), then the
# distro package manager (apt on Ubuntu/Debian, apk on Alpine). Whatever wins
# must end up at ${BIN_DIR}/valkey-server because platform.sh uses that name.
if [ "$ROLE" = "all" ] || [ "$ROLE" = "data" ]; then
  if [ ! -x "${BIN_DIR}/valkey-server" ]; then
    echo "  [+] Locating or installing Valkey..."
    BREW_VALKEY="/home/linuxbrew/.linuxbrew/opt/valkey/bin/valkey-server"
    if command -v valkey-server >/dev/null 2>&1; then
      ln -sf "$(command -v valkey-server)" "${BIN_DIR}/valkey-server"
    elif [ -x "$BREW_VALKEY" ]; then
      ln -sf "$BREW_VALKEY" "${BIN_DIR}/valkey-server"
    elif command -v brew >/dev/null 2>&1 && brew install valkey >/dev/null 2>&1 \
         && [ -x "$BREW_VALKEY" ]; then
      ln -sf "$BREW_VALKEY" "${BIN_DIR}/valkey-server"
    else
      # Official prebuilt binary. `jammy` is the oldest distro build published,
      # so it runs on jammy, noble and any newer glibc-based distribution.
      VALKEY_VERSION="7.2.14"
      VALKEY_DIST="jammy"
      VALKEY_ARCH="$(uname -m)"
      VALKEY_TARBALL="valkey-${VALKEY_VERSION}-${VALKEY_DIST}-${VALKEY_ARCH}.tar.gz"
      TMP_TAR="/tmp/${VALKEY_TARBALL}"
      echo "      Downloading Valkey binary (download.valkey.io)..."
      if curl -sSL "https://download.valkey.io/releases/${VALKEY_TARBALL}" -o "$TMP_TAR" \
         && curl -sSL "https://download.valkey.io/releases/${VALKEY_TARBALL}.sha256" -o "${TMP_TAR}.sha256" \
         && (cd /tmp && sha256sum -c --status "${VALKEY_TARBALL}.sha256"); then
        tar -xzf "$TMP_TAR" -C /tmp \
          "valkey-${VALKEY_VERSION}-${VALKEY_DIST}-${VALKEY_ARCH}/bin/valkey-server" \
          "valkey-${VALKEY_VERSION}-${VALKEY_DIST}-${VALKEY_ARCH}/bin/valkey-cli"
        mv "/tmp/valkey-${VALKEY_VERSION}-${VALKEY_DIST}-${VALKEY_ARCH}/bin/valkey-server" "${BIN_DIR}/valkey-server"
        mv "/tmp/valkey-${VALKEY_VERSION}-${VALKEY_DIST}-${VALKEY_ARCH}/bin/valkey-cli" "${BIN_DIR}/valkey-cli"
        rm -rf "$TMP_TAR" "${TMP_TAR}.sha256" "/tmp/valkey-${VALKEY_VERSION}-${VALKEY_DIST}-${VALKEY_ARCH}"
        echo "      sha256 verified: ${VALKEY_TARBALL}"
      else
        rm -f "$TMP_TAR" "${TMP_TAR}.sha256"
        echo "      Prebuilt download failed; trying the distro package manager..."
        if command -v apt-get >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
          sudo apt-get install -y valkey-server >/dev/null 2>&1 \
            || sudo apt-get install -y valkey >/dev/null 2>&1 || true
        elif command -v apk >/dev/null 2>&1; then
          apk add --no-cache valkey >/dev/null 2>&1 || true
        fi
        if command -v valkey-server >/dev/null 2>&1; then
          ln -sf "$(command -v valkey-server)" "${BIN_DIR}/valkey-server"
        fi
      fi
    fi
    if [ -x "${BIN_DIR}/valkey-server" ]; then
      echo "      Installed Valkey: $(${BIN_DIR}/valkey-server --version 2>&1 | head -n 1)"
    else
      echo "  [!] Could not obtain valkey-server." >&2
      echo "      Install it yourself, then re-run ./install.sh:" >&2
      echo "        Ubuntu/Debian:  sudo apt-get install -y valkey-server" >&2
      echo "        Alpine:         sudo apk add valkey" >&2
      echo "        Other:          https://valkey.io/download/ (prebuilt tarballs)" >&2
      exit 1
    fi
  else
    echo "  [*] Valkey already installed in ${BIN_DIR}/valkey-server"
  fi
else
  echo "  [i] Skipping Valkey (data/all only)."
fi

# 4. Python Virtual Environment & Lightweight Services (web/inference/all only)
echo "[4/6] Setting up Python virtual environment and dependencies..."
if [ "$ROLE" = "data" ]; then
  echo "  [i] Skipping Python venv (data runs only static binaries)."
else
  PYTHON_SYS="$(which python3)"
  if [ ! -d "${VENV_DIR}" ]; then
    "$PYTHON_SYS" -m venv "${VENV_DIR}"
  fi

  echo "  [+] Checking / updating required python packages..."
  "${VENV_DIR}/bin/pip" install -q --prefer-binary \
    "litellm[proxy]" fastapi uvicorn httpx pyyaml redis pydantic huggingface_hub
  echo "  [+] Python dependencies verified."
  if { [ "$ROLE" = "all" ] || [ "$ROLE" = "inference" ]; } && [ "$SKIP_VLLM" = 0 ]; then
    echo "  [+] Installing vLLM in an isolated Python 3.12 environment..."
    # vLLM bundles compiled CUDA/PyTorch components; keep them separate from
    # the platform's service environment to avoid dependency conflicts.
    "${VENV_DIR}/bin/pip" install -q --prefer-binary uv
    if [ ! -x "${VLLM_VENV_DIR}/bin/python" ]; then
      "${VENV_DIR}/bin/uv" venv --python 3.12 --seed --managed-python "${VLLM_VENV_DIR}"
    fi
    VLLM_PACKAGE="vllm"
    if [ -n "$VLLM_VERSION" ]; then VLLM_PACKAGE="vllm==${VLLM_VERSION}"; fi
    "${VENV_DIR}/bin/uv" pip install --python "${VLLM_VENV_DIR}/bin/python" "$VLLM_PACKAGE"
    echo "  [+] vLLM installed in ${VLLM_VENV_DIR}."
    # Let the vLLM wheel resolve its matching torch dependencies. Selecting a
    # backend from the host driver alone can mix CUDA 12 torch with CUDA 13 vLLM.
    if ! "${VLLM_VENV_DIR}/bin/python" "${BACKEND_DIR}/scripts/verify_vllm_runtime.py"; then
      echo "  [!] Runtime readiness failed; see docs/nvidia-vllm.md and docs/runbooks/vast-deepseek.md." >&2
      exit 1
    fi
  else
    echo "  [i] Skipping vLLM installation (role=${ROLE}; install it on the inference host)."
  fi
fi

# 5. Configurations & Script Permissions
echo "[5/6] Finalizing configurations and permissions..."
chmod +x "${BACKEND_DIR}/platform.sh"
chmod +x "${BACKEND_DIR}/config/sandbox/bwrap-runner.sh"
chmod +x "${BACKEND_DIR}/config/keys/provision-keys.sh"

# Role-dependent configs were already rendered up front (before any install
# step) so a host switched back to `all` reverts them even if a later step
# fails; re-running the renderer here is a cheap no-op that also covers a
# deployment.env edited by hand between the two points.
python3 "${CONFIG_DIR}/roles/render_config.py" "${CONFIG_DIR}/roles/deployment.env"

# Create root level symlinks for ease of access
ln -sf "backend/platform.sh" "${SCRIPT_DIR}/platform.sh"
chmod +x "${SCRIPT_DIR}/platform.sh"

# 6. Provision Keys (web/all only; inference/data copy keys from W)
echo "[6/6] Provisioning Sysadmin API keys (10 users + P1 bypass)..."
if [ "$ROLE" = "all" ] || [ "$ROLE" = "web" ]; then
  "${BACKEND_DIR}/config/keys/provision-keys.sh"
  "${VENV_PYTHON}" "${BACKEND_DIR}/config/keys/provision-logins.py"
else
  echo "  [i] Keys are provisioned on the WEB host only. Copy them from W over a"
  echo "      secure channel (scp/rsync over SSH) and verify permissions (0600)."
  if [ "$ROLE" = "inference" ]; then
    echo "      inference needs:  master.key, sysadmin-*.key, emergency-p1.key,"
    echo "                        valkey-password.key"
  elif [ "$ROLE" = "data" ]; then
    echo "      data needs:       valkey-password.key"
  fi
  echo "      See docs/multi-host.md (key-copy and revocation)."
fi

echo "--------------------------------------------------------------------"
echo " Installation & Configuration Complete! Zero Docker Overhead."
echo " Machine role: ${ROLE}"
if [ "$ROLE" != "all" ]; then
  echo "   This host runs only the '${ROLE}' services."
  echo "   Switch to a single machine later with: ./install.sh --role all"
fi
echo "--------------------------------------------------------------------"
echo " Usage Commands:"
echo "   ./platform.sh start    # Start this role's services"
echo "   ./platform.sh status   # Show status & memory usage"
echo "   ./platform.sh test     # Run end-to-end verification test suite"
echo "   ./platform.sh stop     # Gracefully stop this role's services"
echo "===================================================================="
