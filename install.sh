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
    "${VENV_DIR}/bin/pip" install -q "litellm[proxy]" fastapi uvicorn httpx pyyaml redis pydantic rich
  fi
  exec "$VENV_PYTHON" "${BACKEND_DIR}/installer_tui.py"
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
         "${DATA_DIR}"/{valkey,seaweedfs,victorialogs,workspaces,runbooks,logs} \
         "${LOGS_DIR}" "${RUN_DIR}"

# Set up per-user workspaces (sysadmin-01 to sysadmin-10) with 0700 permissions
for i in $(seq -w 1 10); do
  mkdir -p "${DATA_DIR}/workspaces/sysadmin-${i}"
  chmod 700 "${DATA_DIR}/workspaces/sysadmin-${i}"
done
mkdir -p "${DATA_DIR}/workspaces/emergency-p1-oncall"
chmod 700 "${DATA_DIR}/workspaces/emergency-p1-oncall"
echo "  [+] Initialized directories and 10 isolated workspaces (mode 0700)."

# 2. Bubblewrap Verification
echo "[2/6] Verifying Linux Kernel Sandboxing (Bubblewrap)..."
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

# 3. Native Static Binaries (Zero Docker Daemon overhead)
echo "[3/6] Installing native static Go & C binaries..."

# 3.1 Traefik (Reverse Proxy & ForwardAuth Router)
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

# 3.2 VictoriaLogs (High-Efficiency Audit Logs Engine)
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

# 3.3 SeaweedFS (Local S3 Object Storage & Filer)
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

# 3.4 Valkey (Memory Cache & Quotas)
if [ ! -x "${BIN_DIR}/valkey-server" ]; then
  echo "  [+] Locating or installing Valkey..."
  BREW_VALKEY="/home/linuxbrew/.linuxbrew/opt/valkey/bin/valkey-server"
  if [ -x "$BREW_VALKEY" ]; then
    ln -sf "$BREW_VALKEY" "${BIN_DIR}/valkey-server"
  elif command -v valkey-server >/dev/null 2>&1; then
    ln -sf "$(which valkey-server)" "${BIN_DIR}/valkey-server"
  elif command -v brew >/dev/null 2>&1; then
    brew install valkey >/dev/null 2>&1
    ln -sf "$BREW_VALKEY" "${BIN_DIR}/valkey-server"
  else
    echo "  [!] Neither valkey-server nor Homebrew found. Please install valkey-server."
    exit 1
  fi
  echo "      Installed Valkey: $(${BIN_DIR}/valkey-server --version 2>&1 | head -n 1)"
else
  echo "  [*] Valkey already installed in ${BIN_DIR}/valkey-server"
fi

# 4. Python Virtual Environment & Lightweight Services
echo "[4/6] Setting up Python virtual environment and dependencies..."
PYTHON_SYS="$(which python3)"
if [ ! -d "${VENV_DIR}" ]; then
  "$PYTHON_SYS" -m venv "${VENV_DIR}"
fi

echo "  [+] Checking / updating required python packages..."
"${VENV_DIR}/bin/pip" install -q --prefer-binary \
  "litellm[proxy]" fastapi uvicorn httpx pyyaml redis pydantic
echo "  [+] Python dependencies verified."

# 5. Configurations & Script Permissions
echo "[5/6] Finalizing configurations and permissions..."
chmod +x "${BACKEND_DIR}/platform.sh"
chmod +x "${BACKEND_DIR}/config/sandbox/bwrap-runner.sh"
chmod +x "${BACKEND_DIR}/config/keys/provision-keys.sh"

# Create root level symlinks for ease of access
ln -sf "backend/platform.sh" "${SCRIPT_DIR}/platform.sh"
chmod +x "${SCRIPT_DIR}/platform.sh"

# 6. Provision Keys
echo "[6/6] Provisioning Sysadmin API keys (10 users + P1 bypass)..."
"${BACKEND_DIR}/config/keys/provision-keys.sh"
"${VENV_PYTHON}" "${BACKEND_DIR}/config/keys/provision-logins.py"

echo "--------------------------------------------------------------------"
echo " Installation & Configuration Complete! Zero Docker Overhead."
echo "--------------------------------------------------------------------"
echo " Usage Commands:"
echo "   ./platform.sh start    # Start all 8 backend services"
echo "   ./platform.sh status   # Show status & memory usage"
echo "   ./platform.sh test     # Run end-to-end verification test suite"
echo "   ./platform.sh stop     # Gracefully stop all services"
echo "===================================================================="
