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

  return {
    host: env.SYSADMIN_GATEWAY_HOST ?? '127.0.0.1',
    port: toInt(env.SYSADMIN_GATEWAY_PORT, 3085),

    authUrl: (env.SYSADMIN_AUTH_URL ?? 'http://127.0.0.1:3081').replace(/\/+$/, ''),
    agentUrl: (env.SYSADMIN_BACKEND_URL ?? 'http://127.0.0.1:3080').replace(/\/+$/, ''),
    litellmUrl: env.SYSADMIN_LITELLM_URL ?? 'http://127.0.0.1:4000/v1',
    victoriaLogsUrl: env.VICTORIALOGS_URL ?? 'http://127.0.0.1:9428',

    keysDir: env.SYSADMIN_KEYS_DIR ?? join(backendRoot, 'config', 'keys'),
    workspaceRoot: env.SYSADMIN_WORKSPACE_ROOT ?? join(backendRoot, 'data', 'workspaces'),
    stateRoot,
    dshHomeRoot: env.SYSADMIN_DSH_HOME_ROOT ?? join(stateRoot, 'homes'),

    dshBin: env.DSH_BIN ?? 'dsh',
    profileName: env.SYSADMIN_PROFILE ?? 'sysadmin',
    profileSource: env.SYSADMIN_PROFILE_SOURCE ?? resolve(HERE, '..', 'profile'),
    pluginSource: env.SYSADMIN_PLUGIN_SOURCE ?? resolve(HERE, '..', 'dsh-plugin-sysadmin'),

    instanceHost: '127.0.0.1',
    instancePortStart: toInt(env.SYSADMIN_INSTANCE_PORT_START, 3180),
    instancePortEnd: toInt(env.SYSADMIN_INSTANCE_PORT_END, 3280),
    instanceReadyTimeoutMs: toInt(env.SYSADMIN_INSTANCE_READY_TIMEOUT_MS, 60000),

    sessionTtlMs: toInt(env.SYSADMIN_SESSION_TTL_MS, 86_400_000),
    cookieName: env.SYSADMIN_GATEWAY_COOKIE ?? 'sysadmin_gateway',
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
