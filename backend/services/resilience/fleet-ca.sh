#!/usr/bin/env bash
# ==============================================================================
# Sysadmin AI Platform - Fleet CA wrapper (mTLS PKI for platform <-> GPU nodes)
#
# Thin shell front-end to backend/services/resilience/fleet_ca.py. Implements
# the contract install.sh relies on:
#
#   fleet-ca.sh init-ca              # self-signed CA under backend/config/keys/fleet/
#   fleet-ca.sh issue-node <name>    # node key + CSR + signed cert (CN=<name>)
#   fleet-ca.sh issue-platform       # the platform node's own certificate
#   fleet-ca.sh revoke-node <name>   # revoke + regenerate CRL (ca.crl)
#   fleet-ca.sh list                 # issued certificates, status, expiry
#
# Conventions (same as backend/config/keys/provision-keys.sh):
#   - umask 077; private keys are chmod 600.
#   - Never writes to git: everything lives under backend/config/keys/fleet/
#     which is covered by .gitignore.
#   - Idempotent: issue-* reuses an existing certificate unless --force.
#   - Fail closed: exits 2 with a clear message when openssl or python3 is
#     missing, the CA is absent, or the node name is invalid.
# ==============================================================================
set -euo pipefail

umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v python3 >/dev/null 2>&1 || {
  echo "fleet-ca.sh: error: python3 is required (stdlib only) but was not found on PATH." >&2
  exit 2
}

# fleet_ca.py is stdlib-only and has no relative imports, so it can run as a
# plain script: this keeps provisioning working with the system python3 even
# before any venv exists (and avoids importing the package __init__, which
# pulls in third-party dependencies).
exec python3 "${SCRIPT_DIR}/fleet_ca.py" "$@"
