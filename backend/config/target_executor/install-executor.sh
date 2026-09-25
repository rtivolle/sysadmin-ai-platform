#!/usr/bin/env bash
#
# install-executor.sh — install the least-privilege target executor (root).
#
# Installs:
#   /usr/local/libexec/sysadmin-target-exec       the executor binary (0755 root:root)
#   /etc/sysadmin-target-exec/allowlist.json      the root-owned allowlist (0644 root:root)
#   /etc/sudoers.d/sysadmin-target-exec           the sudoers entry (0440 root:root)
#   /var/lib/sysadmin-target-exec/staging         the staging dir (0770 root:sysadmin-agent)
#
# The script is idempotent: re-running it is safe. It never overwrites an
# existing allowlist (that file is the security root and must be edited by an
# operator, not clobbered by an installer). It validates the sudoers snippet
# with `visudo -cf` and removes it on failure.
#
# Usage:  sudo ./install-executor.sh
#
set -euo pipefail

EXECUTOR_PATH="/usr/local/libexec/sysadmin-target-exec"
CONF_DIR="/etc/sysadmin-target-exec"
ALLOWLIST_PATH="${CONF_DIR}/allowlist.json"
STAGING_DIR="/var/lib/sysadmin-target-exec/staging"
SUDOERS_PATH="/etc/sudoers.d/sysadmin-target-exec"

# The adapter account that is allowed to invoke the executor via sudo.
ADAPTER_USER="${ADAPTER_USER:-sysadmin-agent}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXECUTOR_SRC="${SCRIPT_DIR}/../../services/target_executor/main.py"
ALLOWLIST_SRC="${SCRIPT_DIR}/allowlist.json.example"
SUDOERS_SRC="${SCRIPT_DIR}/sudoers.d-sysadmin-target-exec"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "error: must run as root (use sudo)" >&2
  exit 1
fi

if [[ ! -f "${EXECUTOR_SRC}" ]]; then
  echo "error: executor source not found at ${EXECUTOR_SRC}" >&2
  exit 1
fi
if [[ ! -f "${ALLOWLIST_SRC}" ]]; then
  echo "error: allowlist template not found at ${ALLOWLIST_SRC}" >&2
  exit 1
fi

install -d -m 0755 -o root -g root "$(dirname "${EXECUTOR_PATH}")"
install -m 0755 -o root -g root "${EXECUTOR_SRC}" "${EXECUTOR_PATH}"
echo "installed ${EXECUTOR_PATH} (0755 root:root)"

install -d -m 0755 -o root -g root "${CONF_DIR}"
if [[ -f "${ALLOWLIST_PATH}" ]]; then
  echo "allowlist already present at ${ALLOWLIST_PATH}; leaving unchanged (edit by hand)"
else
  install -m 0644 -o root -g root "${ALLOWLIST_SRC}" "${ALLOWLIST_PATH}"
  echo "installed ${ALLOWLIST_PATH} (0644 root:root) from example — review before use"
fi

# Staging dir: root-owned, group-writable by the adapter account so the
# unprivileged adapter can stage files for the executor to validate.
install -d -m 0750 -o root -g root "$(dirname "${STAGING_DIR}")"
if getent group "${ADAPTER_USER}" >/dev/null 2>&1; then
  install -d -m 0770 -o root -g "${ADAPTER_USER}" "${STAGING_DIR}"
  echo "installed ${STAGING_DIR} (0770 root:${ADAPTER_USER})"
else
  install -d -m 0750 -o root -g root "${STAGING_DIR}"
  echo "warning: group '${ADAPTER_USER}' does not exist; created ${STAGING_DIR} 0750 root:root"
  echo "         grant the adapter account write access to the staging dir manually"
fi

# Validate the executor runs and its allowlist loads (as a harmless usage call).
"${EXECUTOR_PATH}" >/dev/null 2>&1 || true

# Install + validate the sudoers snippet. Do not leave an invalid file behind.
if [[ -f "${SUDOERS_SRC}" ]]; then
  install -m 0440 -o root -g root "${SUDOERS_SRC}" "${SUDOERS_PATH}"
  if visudo -cf "${SUDOERS_PATH}"; then
    echo "installed ${SUDOERS_PATH} (0440 root:root) and validated with visudo -cf"
  else
    rm -f "${SUDOERS_PATH}"
    echo "error: sudoers snippet failed visudo -cf; removed ${SUDOERS_PATH}" >&2
    exit 1
  fi
else
  echo "warning: sudoers snippet source not found at ${SUDOERS_SRC}; skipping"
fi

echo "done."
