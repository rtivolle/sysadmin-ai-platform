#!/usr/bin/env bash
# ==============================================================================
# Install the custom `sysadmin` DeepSeek Harness profile into $DSH_HOME.
#
# After this runs, one sysadmin harness instance can be booted with:
#
#   DSH_HOME=$DSH_HOME SYSADMIN_USER=sysadmin-01 \
#   SYSADMIN_TOKEN=$(cat backend/config/keys/sysadmin-01.key) \
#   SYSADMIN_LITELLM_KEY=$(cat backend/config/keys/sysadmin-01.key) \
#   dsh --profile sysadmin --no-open --port 3180
#
# The multi-user gateway (packages/harness-integration/gateway/server.js) sets
# that environment per user and starts one instance per login, so you normally do
# not run the command above by hand.
# ==============================================================================
set -euo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${PKG_DIR}/../.." && pwd)"
PROFILE_NAME="${SYSADMIN_PROFILE:-sysadmin}"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
PROFILE_DIR="${DSH_HOME}/profiles/${PROFILE_NAME}"
PLUGIN_NAME="dsh-plugin-sysadmin"

if [ ! -f "${PKG_DIR}/profile/package.json" ]; then
  echo "Error: profile template missing at ${PKG_DIR}/profile/package.json" >&2
  exit 1
fi
if [ ! -f "${PKG_DIR}/${PLUGIN_NAME}/index.js" ]; then
  echo "Error: plugin missing at ${PKG_DIR}/${PLUGIN_NAME}/index.js" >&2
  exit 1
fi

echo "Installing sysadmin harness profile"
echo "  repository : ${REPO_ROOT}"
echo "  DSH_HOME   : ${DSH_HOME}"
echo "  profile    : ${PROFILE_NAME}"

umask 077
mkdir -p "${PROFILE_DIR}/node_modules"

install -m 0644 "${PKG_DIR}/profile/package.json" "${PROFILE_DIR}/package.json"
install -m 0644 "${PKG_DIR}/profile/cordis.patch.yml" "${PROFILE_DIR}/cordis.patch.yml"

# The loader resolves the bundle from the profile's own node_modules, so the
# plugin is staged as a real directory (not a symlink): Node resolves a symlink
# to its realpath, which would move module lookup back to the repository and out
# of $DSH_HOME/profiles/node_modules, where the dsh dependency closure lives.
rm -rf "${PROFILE_DIR}/node_modules/${PLUGIN_NAME}"
cp -R "${PKG_DIR}/${PLUGIN_NAME}" "${PROFILE_DIR}/node_modules/${PLUGIN_NAME}"

# The user's own patch layer is never overwritten once created.
if [ ! -f "${PROFILE_DIR}/cordis.patch.yml.user" ]; then
  {
    echo "# Optional user overrides for the '${PROFILE_NAME}' profile."
    echo "# This file is not read automatically: merge entries into cordis.patch.yml"
    echo "# or pass them with --patch. Kept so local edits have a home."
    echo "[]"
  } > "${PROFILE_DIR}/cordis.patch.yml.user"
  chmod 0600 "${PROFILE_DIR}/cordis.patch.yml.user"
fi

echo "Installed:"
echo "  ${PROFILE_DIR}/package.json"
echo "  ${PROFILE_DIR}/cordis.patch.yml"
echo "  ${PROFILE_DIR}/node_modules/${PLUGIN_NAME}/"
echo
echo "Verify the composed tree with:"
echo "  DSH_HOME=${DSH_HOME} dsh --profile ${PROFILE_NAME} --dump-config | grep -A3 sysadmin-harness"
echo "Run the full check with:"
echo "  node ${PKG_DIR}/scripts/verify-harness.mjs"
