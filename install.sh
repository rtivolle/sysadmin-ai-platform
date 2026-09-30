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
  echo "Usage: ./install.sh [--role all|web|inference|data|platform] [--skip-vllm] [--vllm-version VERSION] [--peer-* ...]"
  echo "  --role all        one machine: every service, all binds loopback (default)"
  echo "  --role web        application tier; needs --lan-bind-ip, --peer-inference, --peer-data (compat, PR-H1)"
  echo "  --role data       state tier (Valkey, VictoriaLogs, SeaweedFS); needs --lan-bind-ip (compat, PR-H1)"
  echo "  --role inference  GPU node (inference engine + node-agent); needs --lan-bind-ip, --platform-url"
  echo "  --role platform   data+admin tier (state, agent platform, LiteLLM, fleet control); needs --lan-bind-ip"
  echo "  --dry-run         resolve and validate the role, print the plan, change nothing"
  echo "  --unattended      never prompt; fail closed when a required value is missing"
  echo "  --platform-url    platform fleet API base URL (role inference), e.g. https://10.0.0.20:3080"
  echo "  --node-name       fleet identity of this node (default: hostname)"
  echo "  --peer-inference-hosts  comma-separated GPU bootstrap list (role platform, optional)"
  echo "  --nvidia          install NVIDIA drivers on this host (role inference; extends the vLLM setup)"
  echo "  Without --role the role recorded in backend/config/roles/deployment.env is reused;"
  echo "  --role all switches a split host back to all-in-one. See docs/multi-host.md."
  echo "GPU setup: ./install.sh --nvidia [--apply] [--driver auto|BRANCH] [--cuda-toolkit MAJOR-MINOR]"
  echo "Wizard: ./install.sh --tui [--unattended] [--role ... --lan-bind-ip ... --peer-* ...]"
  echo "Inventory: ./install.sh --survey"
  exit 0
fi
SKIP_VLLM=0
DRY_RUN=0
UNATTENDED=0
NVIDIA_SETUP=0
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
    "${VENV_DIR}/bin/pip" install -q --disable-pip-version-check --upgrade pip
    "${VENV_DIR}/bin/pip" install -q --disable-pip-version-check "litellm[proxy]" fastapi uvicorn httpx pyyaml redis pydantic rich huggingface_hub
  fi
  shift
  exec "$VENV_PYTHON" "${BACKEND_DIR}/installer_tui.py" "$@"
fi

# ---- Multi-host role flags (unattended; PR-H1) ------------------------------
# The machine role is the operator's topology choice at install time:
#
#   --role all        one machine, everything local, all binds loopback
#   --role web        application tier (Traefik, ForwardAuth, agent platform,
#                     sandbox, harness gateway, workspaces) — compat (PR-H1)
#   --role data       state tier (Valkey, VictoriaLogs, SeaweedFS, backups) —
#                     compat (PR-H1)
#   --role inference  GPU node: inference engine + node-agent (fleet). Needs
#                     --lan-bind-ip and --platform-url; secretless by design.
#   --role platform   data+admin tier: Valkey, VictoriaLogs, SeaweedFS, agent
#                     platform, LiteLLM (loopback), fleet control. Needs
#                     --lan-bind-ip; GPU nodes register to it.
#
# Without --role the installer reuses the role recorded in
# backend/config/roles/deployment.env, so ./update.sh (which calls the installer
# with no flags) never silently re-roles a machine; with neither the flag nor a
# recorded role the host is single-host (all). Passing --role all on a host that
# was web/inference/data/platform switches it back to all-in-one: the recorded
# peers are reset to loopback, the role-dependent files are re-rendered to their
# single-host form, and only then is anything installed.
# The interactive TUI asks the same questions (installer_tui.py role step).
ROLE=""
LAN_BIND_IP=""
PEER_INFERENCE_HOST=""
PEER_INFERENCE_PORT=""
PEER_INFERENCE_HOSTS=""
PEER_DATA_HOST=""
PEER_DATA_VALKEY_PORT=""
PEER_DATA_LOGS_PORT=""
PEER_DATA_SEAWEEDFS_PORT=""
PLATFORM_URL=""
NODE_NAME=""

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-vllm) SKIP_VLLM=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    # --unattended asserts the non-interactive contract: this installer never
    # prompts (a missing required value fails closed with exit 2), so the flag
    # is a no-op here — it exists so fleet provisioning scripts can declare
    # "no human will answer" explicitly. The TUI honours it with defaults.
    --unattended) UNATTENDED=1; shift ;;
    # --nvidia opts this host into the NVIDIA driver install (role inference).
    # Kept as a flag here; the legacy first-argument form at the top of this
    # script still execs nvidia_setup.py directly.
    --nvidia) NVIDIA_SETUP=1; shift ;;
    --vllm-version) VLLM_VERSION="${2:?--vllm-version requires a version}"; shift 2 ;;
    --role)                  ROLE="${2:?--role requires all|web|inference|data|platform}"; shift 2 ;;
    --lan-bind-ip)           LAN_BIND_IP="${2:?--lan-bind-ip requires an address}"; shift 2 ;;
    --peer-inference)        PEER_INFERENCE_HOST="${2:?--peer-inference requires an address}"; shift 2 ;;
    --peer-inference-port)   PEER_INFERENCE_PORT="${2:?--peer-inference-port requires a port}"; shift 2 ;;
    --peer-inference-hosts)  PEER_INFERENCE_HOSTS="${2:?--peer-inference-hosts requires a comma-separated host list}"; shift 2 ;;
    --peer-data)             PEER_DATA_HOST="${2:?--peer-data requires an address}"; shift 2 ;;
    --peer-data-valkey-port)     PEER_DATA_VALKEY_PORT="${2:?--peer-data-valkey-port requires a port}"; shift 2 ;;
    --peer-data-logs-port)       PEER_DATA_LOGS_PORT="${2:?--peer-data-logs-port requires a port}"; shift 2 ;;
    --peer-data-seaweedfs-port)  PEER_DATA_SEAWEEDFS_PORT="${2:?--peer-data-seaweedfs-port requires a port}"; shift 2 ;;
    --platform-url)          PLATFORM_URL="${2:?--platform-url requires a URL}"; shift 2 ;;
    --node-name)             NODE_NAME="${2:?--node-name requires a name}"; shift 2 ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Usage: $0 [--role all|web|inference|data|platform] [--peer-* ...] [--lan-bind-ip IP]" >&2
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
    echo "      Pass --role all|web|inference|data|platform to change it."
  else
    ROLE="all"
  fi
fi

case "$ROLE" in
  all|web|inference|data|platform) ;;
  *)
    echo "Invalid role: ${ROLE} (expected all|web|inference|data|platform)" >&2
    exit 2
    ;;
esac

LAN_BIND_IP="$(resolve_value "$LAN_BIND_IP" "$(recorded_env LAN_BIND_IP)" "127.0.0.1")"
PEER_INFERENCE_HOST="$(resolve_value "$PEER_INFERENCE_HOST" "$(recorded_env PEER_INFERENCE_HOST)" "127.0.0.1")"
PEER_INFERENCE_PORT="$(resolve_value "$PEER_INFERENCE_PORT" "$(recorded_env PEER_INFERENCE_PORT)" "4000")"
PEER_INFERENCE_HOSTS="$(resolve_value "$PEER_INFERENCE_HOSTS" "$(recorded_env PEER_INFERENCE_HOSTS)" "")"
PEER_DATA_HOST="$(resolve_value "$PEER_DATA_HOST" "$(recorded_env PEER_DATA_HOST)" "127.0.0.1")"
PEER_DATA_VALKEY_PORT="$(resolve_value "$PEER_DATA_VALKEY_PORT" "$(recorded_env PEER_DATA_VALKEY_PORT)" "6379")"
PEER_DATA_LOGS_PORT="$(resolve_value "$PEER_DATA_LOGS_PORT" "$(recorded_env PEER_DATA_LOGS_PORT)" "9428")"
PEER_DATA_SEAWEEDFS_PORT="$(resolve_value "$PEER_DATA_SEAWEEDFS_PORT" "$(recorded_env PEER_DATA_SEAWEEDFS_PORT)" "8333")"
PLATFORM_URL="$(resolve_value "$PLATFORM_URL" "$(recorded_env PLATFORM_URL)" "")"
NODE_NAME="$(resolve_value "$NODE_NAME" "$(recorded_env NODE_NAME)" "$(hostname 2>/dev/null || echo gpu-node)")"

if [ "$ROLE" = "all" ]; then
  # Single host: every address is loopback, whatever an earlier role recorded.
  # A stale peer address here would make the local services talk to a machine
  # that is no longer part of the deployment.
  LAN_BIND_IP="127.0.0.1"
  PEER_INFERENCE_HOST="127.0.0.1"
  PEER_INFERENCE_PORT="4000"
  PEER_INFERENCE_HOSTS=""
  PEER_DATA_HOST="127.0.0.1"
  PEER_DATA_VALKEY_PORT="6379"
  PEER_DATA_LOGS_PORT="9428"
  PEER_DATA_SEAWEEDFS_PORT="8333"
  PLATFORM_URL=""
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
    # The GPU node is secretless: its only peer is the platform (fleet API for
    # register/heartbeat, VictoriaLogs for the audit outbox replay). The data
    # host defaults to the platform URL's host — same machine in the reference
    # topology — and --peer-data overrides it when they differ.
    if [ -z "$PLATFORM_URL" ]; then
      fail_closed "Role 'inference' needs the platform URL: pass --platform-url <url>."
    fi
    if is_loopback_address "$PEER_DATA_HOST"; then
      PEER_DATA_HOST="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.urlparse(sys.argv[1]).hostname or "")' "$PLATFORM_URL")"
    fi
    if is_loopback_address "$PEER_DATA_HOST"; then
      fail_closed "Role 'inference' needs the platform (data) host: pass --peer-data <ip>."
    fi
  fi
  # platform needs no peers: GPU nodes register to it (PEER_INFERENCE_HOSTS is
  # an optional bootstrap list), and data needs none either.
fi

# Plan preview: resolve and validate the role, print what would happen, and
# touch nothing. This is how a three-machine plan is checked before any peer is
# up, and how the role decision itself is regression-tested.
if [ "$DRY_RUN" = 1 ]; then
  echo "  [i] Dry run: nothing is written, installed or started."
  echo "      role:        ${ROLE}"
  echo "      lan-bind-ip: ${LAN_BIND_IP}"
  if [ "$ROLE" = "platform" ]; then
    echo "      inference:   (fleet registry; bootstrap list below)"
  elif [ "$ROLE" != "inference" ]; then
    echo "      inference:   ${PEER_INFERENCE_HOST}:${PEER_INFERENCE_PORT}"
  fi
  echo "      data:        ${PEER_DATA_HOST} (valkey ${PEER_DATA_VALKEY_PORT}, logs ${PEER_DATA_LOGS_PORT}, seaweedfs ${PEER_DATA_SEAWEEDFS_PORT})"
  if [ -n "$PLATFORM_URL" ]; then
    echo "      platform-url: ${PLATFORM_URL}"
  fi
  if [ -n "$NODE_NAME" ]; then
    echo "      node-name:    ${NODE_NAME}"
  fi
  if [ -n "$PEER_INFERENCE_HOSTS" ]; then
    echo "      inference-hosts (bootstrap): ${PEER_INFERENCE_HOSTS}"
  fi
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
       PEER_INFERENCE_HOSTS="$PEER_INFERENCE_HOSTS" \
       PEER_DATA_HOST="$PEER_DATA_HOST" PEER_DATA_VALKEY_PORT="$PEER_DATA_VALKEY_PORT" \
       PEER_DATA_LOGS_PORT="$PEER_DATA_LOGS_PORT" PEER_DATA_SEAWEEDFS_PORT="$PEER_DATA_SEAWEEDFS_PORT" \
       PLATFORM_URL="$PLATFORM_URL" NODE_NAME="$NODE_NAME" \
       python3 "${CONFIG_DIR}/roles/connectivity_check.py"; then
    echo "  [!] Connectivity check failed; nothing was applied." >&2
    exit 1
  fi
fi

# Record the decision (every role, including all) after the checks passed.
mkdir -p "${CONFIG_DIR}/roles"
cat > "$DEPLOYMENT_ENV_FILE" <<EOF
# Generated by install.sh (PR-H1; platform/inference roles: Phase B). Machine-specific; git-ignored.
# Written for every role: ROLE=all means "one machine, everything loopback".
ROLE=${ROLE}
LAN_BIND_IP=${LAN_BIND_IP}
PEER_INFERENCE_HOST=${PEER_INFERENCE_HOST}
PEER_INFERENCE_PORT=${PEER_INFERENCE_PORT}
PEER_INFERENCE_HOSTS=${PEER_INFERENCE_HOSTS}
PEER_DATA_HOST=${PEER_DATA_HOST}
PEER_DATA_VALKEY_PORT=${PEER_DATA_VALKEY_PORT}
PEER_DATA_LOGS_PORT=${PEER_DATA_LOGS_PORT}
PEER_DATA_SEAWEEDFS_PORT=${PEER_DATA_SEAWEEDFS_PORT}
PLATFORM_URL=${PLATFORM_URL}
NODE_NAME=${NODE_NAME}
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

# State directories are role-scoped: data and platform host Valkey/SeaweedFS/
# VictoriaLogs, web hosts the workspaces; `all` hosts both.
case "$ROLE" in
  all|data|platform)
    mkdir -p "${DATA_DIR}"/{valkey,seaweedfs,victorialogs,runbooks,logs}
    ;;
  *)
    mkdir -p "${DATA_DIR}"/runbooks
    ;;
esac

if [ "$ROLE" = "all" ] || [ "$ROLE" = "web" ] || [ "$ROLE" = "platform" ]; then
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

# 2. Bubblewrap Verification (the sandbox runs where agent_tools runs)
echo "[2/6] Verifying Linux Kernel Sandboxing (Bubblewrap)..."
if [ "$ROLE" = "all" ] || [ "$ROLE" = "web" ] || [ "$ROLE" = "platform" ]; then
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
  echo "  [i] Skipping Bubblewrap check (no sandbox on role=${ROLE})."
fi

# 3. Native Static Binaries (Zero Docker Daemon overhead)
echo "[3/6] Installing native static Go & C binaries (role=${ROLE})..."

# 3.1 Traefik (Reverse Proxy & ForwardAuth Router) — web/all/platform only
if [ "$ROLE" = "all" ] || [ "$ROLE" = "web" ] || [ "$ROLE" = "platform" ]; then
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
  echo "  [i] Skipping Traefik (web/all/platform only)."
fi

# 3.2 VictoriaLogs (High-Efficiency Audit Logs Engine) — data/all/platform only
if [ "$ROLE" = "all" ] || [ "$ROLE" = "data" ] || [ "$ROLE" = "platform" ]; then
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
  echo "  [i] Skipping VictoriaLogs (data/all/platform only)."
fi

# 3.3 SeaweedFS (Local S3 Object Storage & Filer) — data/all/platform only
if [ "$ROLE" = "all" ] || [ "$ROLE" = "data" ] || [ "$ROLE" = "platform" ]; then
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
  echo "  [i] Skipping SeaweedFS (data/all/platform only)."
fi

# 3.4 Valkey (Memory Cache & Quotas) — data/all/platform only
# Resolution order: PATH binary, Linuxbrew prefix, brew install, the official
# prebuilt binary tarball from download.valkey.io (sha256-verified), then the
# distro package manager (apt on Ubuntu/Debian, apk on Alpine). Whatever wins
# must end up at ${BIN_DIR}/valkey-server because platform.sh uses that name.
if [ "$ROLE" = "all" ] || [ "$ROLE" = "data" ] || [ "$ROLE" = "platform" ]; then
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
  echo "  [i] Skipping Valkey (data/all/platform only)."
fi

# 4. Python Virtual Environment & Lightweight Services (all roles except data)
echo "[4/6] Setting up Python virtual environment and dependencies..."
if [ "$ROLE" = "data" ]; then
  echo "  [i] Skipping Python venv (data runs only static binaries)."
else
  PYTHON_SYS="$(which python3)"
  if [ ! -d "${VENV_DIR}" ]; then
    "$PYTHON_SYS" -m venv "${VENV_DIR}"
  fi

  echo "  [+] Checking / updating required python packages..."
  "${VENV_DIR}/bin/pip" install -q --disable-pip-version-check --upgrade pip
  "${VENV_DIR}/bin/pip" install -q --disable-pip-version-check --prefer-binary \
    "litellm[proxy]" fastapi uvicorn httpx pyyaml redis pydantic huggingface_hub
  echo "  [+] Python dependencies verified."
  if { [ "$ROLE" = "all" ] || [ "$ROLE" = "inference" ]; } && [ "$SKIP_VLLM" = 0 ]; then
    if [ "$NVIDIA_SETUP" = 1 ]; then
      echo "  [+] Installing NVIDIA drivers (--nvidia)..."
      # Explicit operator opt-in: driver install needs sudo and a reboot may
      # follow. Fail closed when the setup script reports a problem.
      if ! python3 "${BACKEND_DIR}/scripts/nvidia_setup.py" --apply; then
        echo "  [!] NVIDIA driver setup failed; not continuing to the vLLM install." >&2
        exit 1
      fi
    else
      echo "  [i] Skipping NVIDIA driver install (pass --nvidia to install drivers)."
    fi
    echo "  [+] Installing vLLM in an isolated Python 3.12 environment..."
    # vLLM bundles compiled CUDA/PyTorch components; keep them separate from
    # the platform's service environment to avoid dependency conflicts.
    "${VENV_DIR}/bin/pip" install -q --disable-pip-version-check --prefer-binary uv

    # Determine uv link mode to avoid cross-device hardlink failures.
    # In container or cloud environments (e.g. Azure / Codespaces), ~/.cache and /workspaces
    # reside on different filesystems where hardlinks fail (EXDEV). Test if hardlinks work
    # between uv's cache and the target venv; if not, automatically fall back to copy mode.
    if [ -z "${UV_LINK_MODE:-}" ]; then
      UV_CACHE_PATH="$("${VENV_DIR}/bin/uv" cache dir 2>/dev/null || echo "${HOME}/.cache/uv")"
      mkdir -p "$UV_CACHE_PATH" "${VLLM_VENV_DIR}"
      _PROBE_CACHE="${UV_CACHE_PATH}/.probe_link_$$"
      _PROBE_TARGET="${VLLM_VENV_DIR}/.probe_link_$$"
      touch "$_PROBE_CACHE" 2>/dev/null || true
      if [ -f "$_PROBE_CACHE" ]; then
        if ! ln "$_PROBE_CACHE" "$_PROBE_TARGET" 2>/dev/null; then
          export UV_LINK_MODE="copy"
        fi
        rm -f "$_PROBE_CACHE" "$_PROBE_TARGET" 2>/dev/null || true
      fi
    fi

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

# 6. Provision Keys (all/web/platform only; data copies valkey-password from
# the platform host; inference is secretless by design)
echo "[6/6] Provisioning Sysadmin API keys (10 users + P1 bypass)..."
if [ "$ROLE" = "all" ] || [ "$ROLE" = "web" ] || [ "$ROLE" = "platform" ]; then
  "${BACKEND_DIR}/config/keys/provision-keys.sh"
  "${VENV_PYTHON}" "${BACKEND_DIR}/config/keys/provision-logins.py"
elif [ "$ROLE" = "data" ]; then
  echo "  [i] Keys are provisioned on the application host (web/platform role) only."
  echo "      Copy valkey-password.key over a secure channel (scp/rsync over SSH)"
  echo "      and verify permissions (0600). See docs/multi-host.md (key-copy)."
else
  echo "  [i] No keys are provisioned on role '${ROLE}': a GPU node is secretless by"
  echo "      design — node identity comes from its fleet client certificate (below),"
  echo "      not from user keys."
fi

# 6b. Fleet node identity (platform/inference). The CA script is owned by the
# fleet worker; until it lands, warn instead of failing so the install can
# still complete and the operator can issue the certificate by hand.
if [ "$ROLE" = "platform" ] || [ "$ROLE" = "inference" ]; then
  echo "  [+] Provisioning fleet node identity (node '${NODE_NAME}')..."
  FLEET_CA="${BACKEND_DIR}/services/resilience/fleet-ca.sh"
  if [ -x "$FLEET_CA" ]; then
    if ! "$FLEET_CA" issue-node "$NODE_NAME"; then
      echo "  [!] fleet-ca.sh issue-node failed for '${NODE_NAME}'." >&2
      echo "      The node cannot register until its client certificate is issued." >&2
      exit 1
    fi
  else
    echo "  [!] ${FLEET_CA} not installed yet (fleet work in progress)."
    echo "      Issue the node certificate manually before the node registers:"
    echo "        backend/services/resilience/fleet-ca.sh issue-node ${NODE_NAME}"
  fi
fi

echo "--------------------------------------------------------------------"
echo " Installation & Configuration Complete! Zero Docker Overhead."
echo " Machine role: ${ROLE}"
if [ "$ROLE" != "all" ]; then
  echo "   This host runs only the '${ROLE}' services."
  echo "   Switch to a single machine later with: ./install.sh --role all"
fi
if [ "$ROLE" = "inference" ]; then
  echo "   GPU node '${NODE_NAME}': start with ./platform.sh start, then approve it"
  echo "   in the fleet admin UI. It registers to ${PLATFORM_URL}."
fi
if [ "$ROLE" = "platform" ]; then
  echo "   Fleet control plane: GPU nodes register to this host's :3080 fleet API."
fi
echo "--------------------------------------------------------------------"
echo " Usage Commands:"
echo "   ./platform.sh start    # Start this role's services"
echo "   ./platform.sh status   # Show status & memory usage"
echo "   ./platform.sh test     # Run end-to-end verification test suite"
echo "   ./platform.sh stop     # Gracefully stop this role's services"
echo "===================================================================="
