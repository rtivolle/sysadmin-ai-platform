#!/usr/bin/env bash
# Provision one random local bearer key per account. Existing random keys are kept.
set -euo pipefail

KEYS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
umask 077

new_key() {
  python3 -c 'import secrets; print("sk-" + secrets.token_urlsafe(48))'
}

provision() {
  local name="$1"
  local key_file="${KEYS_DIR}/${name}.key"
  local existing=""
  if [ -f "$key_file" ]; then
    existing="$(cat "$key_file")"
  fi

  # Replace the deterministic keys issued by earlier versions.
  if [ -z "$existing" ] || [[ "$existing" =~ ^sk-sysadmin-[0-9][0-9]-([a-f0-9]{16}|prod-token-[0-9]{4})$ ]] || [ "$existing" = "sk-emergency-incident-p1-critical" ]; then
    new_key > "$key_file"
    echo "Provisioned new random key for ${name}"
  else
    echo "Kept existing key for ${name}"
  fi
  chmod 600 "$key_file"
}

for i in $(seq -w 1 10); do
  provision "sysadmin-${i}"
done
provision "emergency-p1"
provision "master"
provision "valkey-password"
