/**
 * Per-user harness instance manager.
 *
 * The shipped harness is single-tenant: one home, one credential set, one
 * workspace. Multi-user therefore means one `dsh --profile sysadmin` process per
 * authenticated sysadmin, each with its own:
 *
 *   - `DSH_HOME` (sessions, settings, storages),
 *   - profile + plugin copy,
 *   - workspace directory (the sandbox root and `cwd`),
 *   - `SYSADMIN_LITELLM_KEY` (the user's LiteLLM virtual key → per-user quota),
 *   - loopback port.
 *
 * The environment is the isolation boundary. A user's key is never written to
 * shared configuration.
 */
import { spawn } from 'node:child_process'
import { chmodSync, cpSync, existsSync, mkdirSync, readFileSync, rmSync } from 'node:fs'
import { createServer } from 'node:net'
import { join } from 'node:path'

const LAUNCH_TOKEN_PATTERN = /https?:\/\/[^\s"'<>]*[?&]token=([A-Za-z0-9._~-]+)/

export class InstanceError extends Error {
  constructor(message) {
    super(message)
    this.name = 'InstanceError'
  }
}

export class HarnessInstance {
  /**
   * @param {object} options
   * @param {string} options.userId
   * @param {number} options.port
   * @param {import('node:child_process').ChildProcess} options.child
   * @param {string} options.home
   * @param {string} options.workspace
   * @param {string} options.outboxPath
   */
  constructor({ userId, port, child, home, workspace, outboxPath }) {
    this.userId = userId
    this.port = port
    this.child = child
    this.home = home
    this.workspace = workspace
    this.outboxPath = outboxPath
    this.launchToken = null
    this.startedAt = Date.now()
    this.stdout = ''
  }

  get url() {
    return `http://127.0.0.1:${this.port}`
  }

  get running() {
    return this.child.exitCode === null && !this.child.killed
  }

  /** Record process output so readiness can extract the launch token. */
  observe(chunk) {
    this.stdout = (this.stdout + chunk).slice(-65536)
    const match = this.stdout.match(LAUNCH_TOKEN_PATTERN)
    if (match) this.launchToken = match[1]
  }

  stop(signal = 'SIGTERM') {
    if (!this.running) return
    try {
      this.child.kill(signal)
    } catch {
      /* already gone */
    }
  }
}

export class InstanceManager {
  /**
   * @param {object} options
   * @param {ReturnType<import('./config.js').loadConfig>} options.config
   * @param {(message: string, meta?: unknown) => void} [options.logger]
   */
  constructor({ config, logger = () => {} }) {
    this.config = config
    this.log = logger
    /** @type {Map<string, HarnessInstance>} */
    this.instances = new Map()
    /** @type {Map<string, Promise<HarnessInstance>>} */
    this.pending = new Map()
    this.stopping = false
  }

  /** @returns {HarnessInstance|undefined} */
  get(userId) {
    const instance = this.instances.get(userId)
    if (instance && !instance.running) {
      this.instances.delete(userId)
      return undefined
    }
    return instance
  }

  /** @returns {string[]} */
  activeUsers() {
    return [...this.instances.keys()].filter((userId) => this.get(userId) !== undefined)
  }

  /**
   * Return the user's running instance, starting one if needed.
   * Concurrent calls for one user share a single startup.
   *
   * @param {string} userId
   * @returns {Promise<HarnessInstance>}
   */
  async ensure(userId) {
    if (this.stopping) throw new InstanceError('gateway is shutting down')
    const existing = this.get(userId)
    if (existing) return existing

    const inflight = this.pending.get(userId)
    if (inflight) return inflight

    const starting = this.#start(userId).finally(() => this.pending.delete(userId))
    this.pending.set(userId, starting)
    return starting
  }

  /**
   * @param {string} userId
   * @returns {Promise<HarnessInstance>}
   */
  async #start(userId) {
    const { config } = this
    assertSafeUserId(userId)

    const home = join(config.dshHomeRoot, userId)
    const workspace = join(config.workspaceRoot, userId)
    const outboxPath = join(config.stateRoot, 'audit', `${userId}-outbox.jsonl`)
    const token = readUserToken(config.keysDir, userId)

    provisionProfile({
      home,
      profileName: config.profileName,
      profileSource: config.profileSource,
      pluginSource: config.pluginSource,
    })
    mkdirSync(workspace, { recursive: true, mode: 0o700 })
    try {
      chmodSync(workspace, 0o700)
    } catch {
      /* best effort on filesystems without POSIX modes */
    }
    mkdirSync(join(config.stateRoot, 'audit'), { recursive: true, mode: 0o700 })

    const port = await findFreePort(config.instanceHost, config.instancePortStart, config.instancePortEnd)

    const env = {
      ...process.env,
      DSH_HOME: home,
      DSH_TELEMETRY_DISABLED: '1',
      SYSADMIN_USER: userId,
      SYSADMIN_TOKEN: token,
      SYSADMIN_LITELLM_KEY: token,
      SYSADMIN_LITELLM_URL: config.litellmUrl,
      SYSADMIN_BACKEND_URL: config.agentUrl,
      SYSADMIN_AUTH_URL: config.authUrl,
      VICTORIALOGS_URL: config.victoriaLogsUrl,
      SYSADMIN_AUDIT_OUTBOX: outboxPath,
      SYSADMIN_HARNESS_HOST: config.instanceHost,
      SYSADMIN_HARNESS_PORT: String(port),
    }

    this.log(`starting harness for ${userId} on port ${port}`)
    const child = spawn(
      config.dshBin,
      ['--profile', config.profileName, '--no-open', '--port', String(port)],
      { cwd: workspace, env, stdio: ['ignore', 'pipe', 'pipe'] },
    )

    const instance = new HarnessInstance({ userId, port, child, home, workspace, outboxPath })
    child.stdout?.setEncoding('utf8')
    child.stderr?.setEncoding('utf8')
    child.stdout?.on('data', (chunk) => instance.observe(String(chunk)))
    child.stderr?.on('data', (chunk) => instance.observe(String(chunk)))
    child.on('exit', (code, signal) => {
      instance.exitCode = code
      instance.exitSignal = signal
      this.log(`harness for ${userId} exited (code=${code} signal=${signal})`)
      if (this.instances.get(userId) === instance) this.instances.delete(userId)
    })

    this.instances.set(userId, instance)

    try {
      await waitForReady(instance, config.instanceReadyTimeoutMs)
    } catch (error) {
      instance.stop('SIGKILL')
      this.instances.delete(userId)
      throw error
    }
    return instance
  }

  /** Stop every instance; called on gateway shutdown. */
  stopAll() {
    this.stopping = true
    for (const instance of this.instances.values()) instance.stop()
    this.instances.clear()
  }
}

/**
 * Copy the profile and plugin into a user's harness home. Idempotent: the plugin
 * is replaced on every start so an upgraded checkout takes effect, while the
 * home's sessions/storages are left untouched.
 */
export function provisionProfile({ home, profileName, profileSource, pluginSource }) {
  const profileDir = join(home, 'profiles', profileName)
  const nodeModules = join(profileDir, 'node_modules')
  mkdirSync(nodeModules, { recursive: true })

  cpSync(join(profileSource, 'package.json'), join(profileDir, 'package.json'))
  cpSync(join(profileSource, 'cordis.patch.yml'), join(profileDir, 'cordis.patch.yml'))

  const pluginTarget = join(nodeModules, 'dsh-plugin-sysadmin')
  rmSync(pluginTarget, { recursive: true, force: true })
  cpSync(pluginSource, pluginTarget, { recursive: true })
  return profileDir
}

/**
 * Read a user's bearer key. The same value authenticates backend calls and is
 * the user's LiteLLM virtual key, so quota is charged to the right identity.
 *
 * @param {string} keysDir
 * @param {string} userId
 * @returns {string}
 */
export function readUserToken(keysDir, userId) {
  const path = join(keysDir, `${userId}.key`)
  if (!existsSync(path)) {
    throw new InstanceError(`missing key file for ${userId}: ${path} (run ./install.sh)`)
  }
  const token = readFileSync(path, 'utf8').trim()
  if (!token) throw new InstanceError(`empty key file for ${userId}: ${path}`)
  return token
}

/** Reject identifiers that could escape the per-user state directory. */
export function assertSafeUserId(userId) {
  if (typeof userId !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(userId)) {
    throw new InstanceError(`unsafe user id: ${JSON.stringify(userId)}`)
  }
}

/**
 * Reserve a free loopback port inside the configured range.
 *
 * @param {string} host
 * @param {number} start
 * @param {number} end
 * @returns {Promise<number>}
 */
export async function findFreePort(host, start, end) {
  for (let port = start; port <= end; port += 1) {
    if (await portIsFree(host, port)) return port
  }
  throw new InstanceError(`no free harness port in ${host}:${start}-${end}`)
}

/** @returns {Promise<boolean>} */
export function portIsFree(host, port) {
  return new Promise((resolve) => {
    const server = createServer()
    server.unref()
    server.once('error', () => resolve(false))
    server.listen(port, host, () => {
      server.close(() => resolve(true))
    })
  })
}

/**
 * Poll the instance until the web surface answers, then give the launch-token
 * scan a moment to complete. Rejects as soon as the child exits.
 *
 * @param {HarnessInstance} instance
 * @param {number} timeoutMs
 */
export async function waitForReady(instance, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (!instance.running) {
      throw new InstanceError(
        `harness for ${instance.userId} exited during startup (code=${instance.child.exitCode})\n${instance.stdout.slice(-2000)}`,
      )
    }
    try {
      const response = await fetch(instance.url, { signal: AbortSignal.timeout(2000) })
      if (response.status > 0) {
        // Readiness is enough; the launch token is best-effort and may arrive later.
        return instance
      }
    } catch {
      /* not listening yet */
    }
    await delay(300)
  }
  throw new InstanceError(`harness for ${instance.userId} did not become ready within ${timeoutMs}ms`)
}

/** @param {number} ms */
export function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}
