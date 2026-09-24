/**
 * Gateway configuration.
 *
 * The gateway is the multi-user front door for the harness: it authenticates a
 * sysadmin against the platform's auth gateway and then routes that browser to a
 * harness instance owned by the same identity. Every path here can be overridden
 * by environment so the same code runs in a scratch test home and in production.
 */
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(HERE, '..', '..', '..')

/**
 * @param {NodeJS.ProcessEnv} [env]
 */
export function loadConfig(env = process.env) {
  const backendRoot = env.SYSADMIN_BACKEND_ROOT ?? join(REPO_ROOT, 'backend')
  const stateRoot = env.SYSADMIN_HARNESS_STATE ?? join(backendRoot, 'data', 'harness')
  const keysDir = env.SYSADMIN_KEYS_DIR ?? join(backendRoot, 'config', 'keys')
  const agentUrl = (env.SYSADMIN_BACKEND_URL ?? 'http://127.0.0.1:3080').replace(/\/+$/, '')
  const authUrl = (env.SYSADMIN_AUTH_URL ?? 'http://127.0.0.1:3081').replace(/\/+$/, '')
  const litellmUrl = env.SYSADMIN_LITELLM_URL ?? 'http://127.0.0.1:4000/v1'
  const victoriaLogsUrl = env.VICTORIALOGS_URL ?? 'http://127.0.0.1:9428'

  return {
    host: env.SYSADMIN_GATEWAY_HOST ?? '127.0.0.1',
    port: toInt(env.SYSADMIN_GATEWAY_PORT, 3085),

    authUrl,
    agentUrl,
    litellmUrl,
    victoriaLogsUrl,

    keysDir,
    workspaceRoot: env.SYSADMIN_WORKSPACE_ROOT ?? join(backendRoot, 'data', 'workspaces'),
    stateRoot,
    dshHomeRoot: env.SYSADMIN_DSH_HOME_ROOT ?? join(stateRoot, 'homes'),
    sessionsFile: env.SYSADMIN_SESSIONS_FILE ?? join(stateRoot, 'sessions.jsonl'),
    adminSessionsFile: env.SYSADMIN_ADMIN_SESSIONS_FILE ?? join(stateRoot, 'admin-sessions.jsonl'),
    instanceRegistryDir: env.SYSADMIN_INSTANCE_REGISTRY_DIR ?? join(stateRoot, 'instances'),
    instanceLogDir: env.SYSADMIN_INSTANCE_LOG_DIR ?? join(stateRoot, 'logs'),

    dshBin: env.DSH_BIN ?? 'dsh',
    profileName: env.SYSADMIN_PROFILE ?? 'sysadmin',
    profileSource: env.SYSADMIN_PROFILE_SOURCE ?? resolve(HERE, '..', 'profile'),
    pluginSource: env.SYSADMIN_PLUGIN_SOURCE ?? resolve(HERE, '..', 'dsh-plugin-sysadmin'),

    instanceHost: '127.0.0.1',
    instancePortStart: toInt(env.SYSADMIN_INSTANCE_PORT_START, 3180),
    instancePortEnd: toInt(env.SYSADMIN_INSTANCE_PORT_END, 3280),
    instanceReadyTimeoutMs: toInt(env.SYSADMIN_INSTANCE_READY_TIMEOUT_MS, 60000),
    instanceIdleTtlMs: toInt(env.SYSADMIN_INSTANCE_IDLE_TTL_MS, 0),
    restartMaxAttempts: toInt(env.SYSADMIN_RESTART_MAX_ATTEMPTS, 3),
    restartBackoffMs: toInt(env.SYSADMIN_RESTART_BACKOFF_MS, 1000),

    sessionTtlMs: toInt(env.SYSADMIN_SESSION_TTL_MS, 86_400_000),
    cookieName: env.SYSADMIN_GATEWAY_COOKIE ?? 'sysadmin_gateway',
    adminTtlMs: toInt(env.SYSADMIN_ADMIN_TTL_MS, 12 * 3_600_000),
    adminCookieName: env.SYSADMIN_ADMIN_COOKIE ?? 'sysadmin_admin',

    masterKeyFile: env.SYSADMIN_MASTER_KEY_FILE ?? join(keysDir, 'master.key'),
    credentialsFile: env.SYSADMIN_LOGIN_CREDENTIALS_FILE ?? join(keysDir, 'login-credentials.json'),
    initialPasswordsFile: env.SYSADMIN_INITIAL_PASSWORDS_FILE ?? join(keysDir, 'initial-passwords.txt'),
    provisionLoginsScript: env.SYSADMIN_PROVISION_LOGINS ?? join(keysDir, 'provision-logins.py'),
    pythonBin: env.SYSADMIN_PYTHON ?? join(backendRoot, '.venv', 'bin', 'python3'),
    platformSh: env.SYSADMIN_PLATFORM_SH ?? join(REPO_ROOT, 'platform.sh'),
    backendRoot,

    services: {
      traefik: { port: toInt(env.SYSADMIN_TRAEFIK_PORT, 8080) },
      litellm: { port: urlPort(litellmUrl, 4000) },
      agent_tools: { port: urlPort(agentUrl, 3080) },
      auth_gateway: { port: urlPort(authUrl, 3081) },
      inference: { port: toInt(env.SYSADMIN_INFERENCE_PORT, 8000) },
      seaweedfs: {
        port: toInt(env.SYSADMIN_SEAWEEDFS_PORT, 8333),
        masterPort: toInt(env.SYSADMIN_SEAWEEDFS_MASTER_PORT, 9333),
      },
      audit_outbox: { port: null },
      victorialogs: { port: urlPort(victoriaLogsUrl, 9428) },
      valkey: { port: toInt(env.SYSADMIN_VALKEY_PORT, 6379) },
      harness_gateway: { port: toInt(env.SYSADMIN_GATEWAY_PORT, 3085) },
    },
  }
}

/**
 * @param {string} url
 * @param {number} fallback
 * @returns {number}
 */
function urlPort(url, fallback) {
  try {
    const parsed = new URL(url)
    return parsed.port ? Number.parseInt(parsed.port, 10) : fallback
  } catch {
    return fallback
  }
}

/**
 * @param {string|undefined} value
 * @param {number} fallback
 * @returns {number}
 */
export function toInt(value, fallback) {
  const parsed = Number.parseInt(String(value ?? ''), 10)
  return Number.isFinite(parsed) ? parsed : fallback
}
