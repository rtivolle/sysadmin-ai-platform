#!/usr/bin/env bash
# ==============================================================================
# Sysadmin AI Platform - local PostgreSQL control store (native, zero Docker)
#
# Owns one private cluster under backend/data/postgres: the durable store behind
# LiteLLM's database_url and the platform's API-key / token-ledger tables.
# Nothing outside the repository is touched, and no package is ever installed:
# when the PostgreSQL binaries are absent this script fails closed (exit 3) with
# the operator command to install them.
#
#   backend/config/postgres/postgres.sh check       # are the binaries present?
#   backend/config/postgres/postgres.sh provision   # initdb + role + database
#   backend/config/postgres/postgres.sh start|stop|status|health
#   backend/config/postgres/postgres.sh dsn         # prints the DSN, password redacted
#   backend/config/postgres/postgres.sh psql        # operator shell
#   backend/config/postgres/postgres.sh backup --out FILE / restore --from FILE
#   backend/config/postgres/postgres.sh destroy --yes
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PG_DATA_DIR="${SYSADMIN_POSTGRES_DATA_DIR:-${BACKEND_DIR}/data/postgres}"
PG_RUN_DIR="${SYSADMIN_POSTGRES_RUN_DIR:-${BACKEND_DIR}/run/postgres}"
PG_LOG_FILE="${SYSADMIN_POSTGRES_LOG_FILE:-${BACKEND_DIR}/logs/postgres.log}"
PG_PASSWORD_FILE="${SYSADMIN_POSTGRES_PASSWORD_FILE:-${BACKEND_DIR}/config/keys/postgres-password.key}"
PG_PORT="${SYSADMIN_POSTGRES_PORT:-5433}"
PG_BIND="${SYSADMIN_POSTGRES_BIND:-127.0.0.1}"
PG_USER="${SYSADMIN_POSTGRES_USER:-sysadmin_control}"
PG_DB="${SYSADMIN_POSTGRES_DB:-sysadmin_control}"

# Homebrew keeps PostgreSQL outside the default PATH on macOS development hosts.
# PATH is only extended when the caller did not already provide the server tools,
# so an operator-provided (or test) PATH always wins.
if [ -z "$(command -v initdb 2>/dev/null)" ]; then
  for prefix in /opt/homebrew/opt/postgresql@17/bin /opt/homebrew/opt/postgresql@16/bin /usr/local/opt/postgresql@17/bin; do
    if [ -x "$prefix/initdb" ]; then
      PATH="$prefix:$PATH"
      break
    fi
  done
fi
export PATH

EXIT_OK=0
EXIT_CONFIG=2
EXIT_UNAVAILABLE=3
EXIT_USAGE=64

log()  { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }
die()  { local code="$1"; shift; warn "$*"; exit "$code"; }

bin_path() { command -v "$1" 2>/dev/null || true; }

# Non-secret view of the connection, safe for logs and status output.
dsn_redacted() {
  printf 'postgresql://%s:***@%s:%s/%s' "$PG_USER" "$PG_BIND" "$PG_PORT" "$PG_DB"
}

install_guidance() {
  warn "Debian/Ubuntu:  sudo apt-get install -y postgresql"
  warn "RHEL/Fedora:    sudo dnf install -y postgresql-server"
  warn "macOS (dev):    brew install postgresql@17"
  warn "This script never installs packages; install the server, then re-run 'provision'."
}

require_binaries() {
  local missing=()
  for tool in initdb pg_ctl pg_isready psql; do
    [ -n "$(bin_path "$tool")" ] || missing+=("$tool")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    warn "PostgreSQL is not installed: missing ${missing[*]}"
    install_guidance
    exit "$EXIT_UNAVAILABLE"
  fi
}

password_value() {
  [ -s "$PG_PASSWORD_FILE" ] || die "$EXIT_CONFIG" "Missing password file ${PG_PASSWORD_FILE}; run 'provision'."
  cat "$PG_PASSWORD_FILE"
}

is_running() {
  pg_ctl -D "$PG_DATA_DIR" status >/dev/null 2>&1
}

# One launch line keeps the port/bind/socket choices in a single place instead of
# a rendered config file that can drift from this script.
pg_options() {
  printf -- "-p %s -c listen_addresses='%s' -c unix_socket_directories='%s' -c password_encryption=scram-sha-256 -c log_min_messages=warning" \
    "$PG_PORT" "$PG_BIND" "$PG_RUN_DIR"
}

run_psql() {
  PGPASSWORD="$(password_value)" psql -h "$PG_RUN_DIR" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" "$@"
}

cmd_check() {
  local status=0
  for tool in initdb pg_ctl pg_isready psql pg_dump pg_restore; do
    local path
    path="$(bin_path "$tool")"
    if [ -n "$path" ]; then
      printf '  %-12s %s\n' "$tool" "$path"
    else
      printf '  %-12s MISSING\n' "$tool"
      status=1
    fi
  done
  if [ -d "$PG_DATA_DIR" ]; then
    printf '  %-12s %s\n' "cluster" "$PG_DATA_DIR"
  else
    printf '  %-12s %s (not provisioned)\n' "cluster" "$PG_DATA_DIR"
  fi
  if [ "$status" -ne 0 ]; then
    install_guidance
    return "$EXIT_UNAVAILABLE"
  fi
  return "$EXIT_OK"
}

cmd_provision() {
  local force=0
  [ "${1:-}" = "--force" ] && force=1
  require_binaries
  if is_running; then
    die "$EXIT_CONFIG" "Cluster is running; stop it before provisioning."
  fi
  if [ -s "${PG_DATA_DIR}/PG_VERSION" ] && [ "$force" -ne 1 ]; then
    log "Cluster already initialised at ${PG_DATA_DIR} (use --force to reinitialise)."
    return "$EXIT_OK"
  fi
  if [ "$force" -eq 1 ] && [ -d "$PG_DATA_DIR" ]; then
    warn "Reinitialising ${PG_DATA_DIR}: existing data is removed."
    rm -rf "$PG_DATA_DIR"
  fi
  mkdir -p "$PG_DATA_DIR" "$PG_RUN_DIR" "$(dirname "$PG_LOG_FILE")" "$(dirname "$PG_PASSWORD_FILE")"
  chmod 700 "$PG_DATA_DIR" "$PG_RUN_DIR"

  if [ ! -s "$PG_PASSWORD_FILE" ]; then
    umask 077
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$PG_PASSWORD_FILE"
    chmod 600 "$PG_PASSWORD_FILE"
    log "Generated ${PG_PASSWORD_FILE} (mode 0600)."
  fi

  # --pwfile sets the superuser password during initdb, so the cluster never
  # exists for even a moment without authentication configured.
  initdb -D "$PG_DATA_DIR" -U "$PG_USER" --pwfile="$PG_PASSWORD_FILE" \
    --auth-local=peer --auth-host=scram-sha-256 --encoding=UTF8 --no-instructions >/dev/null
  log "Initialised cluster at ${PG_DATA_DIR} (user ${PG_USER})."

  cmd_start
  if ! run_psql -tAc "SELECT 1 FROM pg_database WHERE datname = '${PG_DB}'" | grep -q 1; then
    run_psql -c "CREATE DATABASE \"${PG_DB}\" OWNER \"${PG_USER}\"" >/dev/null
    log "Created database ${PG_DB}."
  fi
  log "Provisioned: $(dsn_redacted)"
}

cmd_start() {
  require_binaries
  if is_running; then
    log "PostgreSQL is already running (pid $(head -1 "${PG_DATA_DIR}/postmaster.pid" 2>/dev/null || echo '?'))."
    return "$EXIT_OK"
  fi
  [ -s "${PG_DATA_DIR}/PG_VERSION" ] || die "$EXIT_CONFIG" "No cluster at ${PG_DATA_DIR}; run 'provision'."
  mkdir -p "$PG_RUN_DIR" "$(dirname "$PG_LOG_FILE")"
  # shellcheck disable=SC2046
  pg_ctl -D "$PG_DATA_DIR" -l "$PG_LOG_FILE" -o "$(pg_options)" -w -t 30 start >/dev/null
  if ! pg_isready -h "$PG_RUN_DIR" -p "$PG_PORT" -q; then
    die "$EXIT_UNAVAILABLE" "PostgreSQL did not become ready; see ${PG_LOG_FILE}."
  fi
  log "PostgreSQL RUNNING on ${PG_BIND}:${PG_PORT} (socket ${PG_RUN_DIR})."
}

cmd_stop() {
  require_binaries
  if ! is_running; then
    log "PostgreSQL is not running."
    return "$EXIT_OK"
  fi
  pg_ctl -D "$PG_DATA_DIR" -m fast -w -t 30 stop >/dev/null
  log "PostgreSQL STOPPED."
}

cmd_status() {
  require_binaries
  if is_running; then
    local pid
    pid="$(head -1 "${PG_DATA_DIR}/postmaster.pid" 2>/dev/null || echo '?')"
    printf '  %-14s \033[32mRUNNING\033[0m (pid %s | %s:%s | data %s)\n' "postgres" "$pid" "$PG_BIND" "$PG_PORT" "$PG_DATA_DIR"
    return "$EXIT_OK"
  fi
  printf '  %-14s STOPPED (%s)\n' "postgres" "$PG_DATA_DIR"
  return 1
}

cmd_health() {
  require_binaries
  if pg_isready -h "$PG_RUN_DIR" -p "$PG_PORT" -q; then
    log "healthy: $(dsn_redacted)"
    return "$EXIT_OK"
  fi
  warn "unavailable: $(dsn_redacted)"
  return "$EXIT_UNAVAILABLE"
}

cmd_psql() {
  require_binaries
  is_running || die "$EXIT_UNAVAILABLE" "PostgreSQL is not running."
  run_psql "$@"
}

cmd_backup() {
  local out=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --out) out="${2:-}"; shift 2 ;;
      *) die "$EXIT_USAGE" "Unknown argument: $1" ;;
    esac
  done
  [ -n "$out" ] || die "$EXIT_USAGE" "backup requires --out FILE"
  require_binaries
  is_running || die "$EXIT_UNAVAILABLE" "PostgreSQL is not running; refusing an inconsistent dump."
  mkdir -p "$(dirname "$out")"
  umask 077
  PGPASSWORD="$(password_value)" pg_dump -h "$PG_RUN_DIR" -p "$PG_PORT" -U "$PG_USER" \
    -d "$PG_DB" --format=custom --no-owner --file="$out"
  chmod 600 "$out"
  log "Wrote $($(command -v stat >/dev/null && stat -f%z "$out" 2>/dev/null || stat -c%s "$out") bytes) to ${out} (mode 0600)."
}

cmd_restore() {
  local from="" clean=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --from) from="${2:-}"; shift 2 ;;
      --clean) clean=1; shift ;;
      *) die "$EXIT_USAGE" "Unknown argument: $1" ;;
    esac
  done
  [ -n "$from" ] || die "$EXIT_USAGE" "restore requires --from FILE"
  [ -s "$from" ] || die "$EXIT_CONFIG" "Backup file ${from} is missing or empty."
  require_binaries
  is_running || die "$EXIT_UNAVAILABLE" "PostgreSQL is not running."
  local args=(--no-owner --dbname "$PG_DB")
  [ "$clean" -eq 1 ] && args+=(--clean --if-exists)
  PGPASSWORD="$(password_value)" pg_restore -h "$PG_RUN_DIR" -p "$PG_PORT" -U "$PG_USER" "${args[@]}" "$from"
  log "Restored ${from} into ${PG_DB}."
}

cmd_destroy() {
  [ "${1:-}" = "--yes" ] || die "$EXIT_USAGE" "destroy removes ${PG_DATA_DIR}; re-run with --yes"
  require_binaries
  is_running && cmd_stop
  rm -rf "$PG_DATA_DIR" "$PG_RUN_DIR"
  log "Removed the cluster at ${PG_DATA_DIR}."
}

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

main() {
  local command="${1:-}"
  [ "$#" -gt 0 ] && shift || true
  case "$command" in
    check)     cmd_check "$@" ;;
    provision) cmd_provision "$@" ;;
    start)     cmd_start "$@" ;;
    stop)      cmd_stop "$@" ;;
    restart)   cmd_stop; cmd_start ;;
    status)    cmd_status "$@" ;;
    health)    cmd_health "$@" ;;
    dsn)       log "$(dsn_redacted)" ;;
    psql)      cmd_psql "$@" ;;
    backup)    cmd_backup "$@" ;;
    restore)   cmd_restore "$@" ;;
    destroy)   cmd_destroy "$@" ;;
    ""|-h|--help|help) usage ;;
    *) die "$EXIT_USAGE" "Unknown command: ${command} (try --help)" ;;
  esac
}

main "$@"
