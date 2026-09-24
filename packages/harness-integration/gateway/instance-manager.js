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
 *
 * Persistence and supervision live here too:
 *   - a per-user registry file (`instances/<user>.json`, 0600) records the port,
 *     pid, start time and home so a gateway restart re-adopts live instances on
 *     the same port instead of cold-starting them;
 *   - per-user output is appended to `logs/<user>.log` (size-rotated, stderr
 *     included), which the admin console tails;
 *   - an unexpected exit restarts the harness with exponential backoff
 *     (`SYSADMIN_RESTART_MAX_ATTEMPTS`, default 3) and then marks it `failed`
 *     with the tail of its output.
 */
import { spawn } from 'node:child_process'
import {
  appendFileSync, chmodSync, cpSync, existsSync, mkdirSync, readdirSync, readFileSync,
  renameSync, rmSync, statSync,
} from 'node:fs'
import { createServer } from 'node:net'
import { join } from 'node:path'
import { trustedHostList } from './config.js'
import { readJsonFile, removeFile, writeFileAtomic } from './state-file.js'

const LAUNCH_TOKEN_PATTERN = /https?:\/\/[^\s"'<>]*[?&]token=([A-Za-z0-9._~-]+)/
const MAX_INSTANCE_LOG_BYTES = 2 * 1024 * 1024
const MAX_OUTPUT_BUFFER = 64 * 1024
const MAX_STDERR_TAIL = 4096
const STOP_GRACE_MS = 5000

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
   * @param {import('node:child_process').ChildProcess|null} [options.child] owned child, null for adopted instances
   * @param {number|null} [options.pid] recorded pid (adopted instances)
   * @param {string} options.home
   * @param {string} options.workspace
   * @param {string} options.outboxPath
   * @param {boolean} [options.adopted]
   * @param {number} [options.startedAt]
   */
  constructor({ userId, port, child = null, pid = null, home, workspace, outboxPath, adopted = false, startedAt = Date.now() }) {
    this.userId = userId
    this.port = port
    this.child = child
    this.pid = child?.pid ?? pid
    this.home = home
    this.workspace = workspace
    this.outboxPath = outboxPath
    this.adopted = adopted
    this.launchToken = null
    this.startedAt = startedAt
    this.lastActivityAt = startedAt
    this.stdout = ''
    this.stderrTail = ''
    this.exitCode = null
    this.exitSignal = null
    /** @type {'starting'|'running'|'restarting'|'failed'|'stopped'} */
    this.state = 'starting'
    this.restartAttempts = 0
    this.failureReason = null
    this.stopping = false
    /** @type {NodeJS.Timeout|null} */
    this.restartTimer = null
  }

  get url() {
    return `http://127.0.0.1:${this.port}`
  }

  get running() {
    if (this.child) return this.child.exitCode === null && this.child.signalCode === null
    if (this.pid !== null) return processAlive(this.pid)
    return false
  }

  /** @param {import('node:child_process').ChildProcess} child */
  attachChild(child) {
    this.child = child
    this.pid = child.pid ?? null
    this.exitCode = null
    this.exitSignal = null
    this.state = 'starting'
  }

  /** Record process output so readiness can extract the launch token. */
  observe(chunk) {
    const text = String(chunk)
    this.stdout = (this.stdout + text).slice(-MAX_OUTPUT_BUFFER)
    this.stderrTail = (this.stderrTail + text).slice(-MAX_STDERR_TAIL)
    const match = this.stdout.match(LAUNCH_TOKEN_PATTERN)
    if (match) this.launchToken = match[1]
  }

  stop(signal = 'SIGTERM') {
    this.stopping = true
    if (this.child) {
      try {
        this.child.kill(signal)
      } catch {
        /* already gone */
      }
      return
    }
    if (this.pid !== null) {
      try {
        process.kill(this.pid, signal)
      } catch {
        /* already gone */
      }
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
    /** @type {Map<string, Promise<unknown>>} */
    this.userLocks = new Map()
    this.stopping = false
    this.idleTimer = null
    /** @type {Map<string, number>} */
    this.logSizes = new Map()
    this.registryDir = config.instanceRegistryDir ?? join(config.stateRoot, 'instances')
    this.logDir = config.instanceLogDir ?? join(config.stateRoot, 'logs')
  }

  /**
   * Return the user's live instance. A `failed` or `stopped` entry is not
   * returned: the next request starts a fresh process.
   *
   * @returns {HarnessInstance|undefined}
   */
  get(userId) {
    const instance = this.instances.get(userId)
    if (!instance) return undefined
    if (instance.state === 'running') return instance.running ? instance : undefined
    if (instance.state === 'starting') return instance
    return undefined
  }

  /** @returns {string[]} */
  activeUsers() {
    return [...this.instances.keys()].filter((userId) => this.get(userId) !== undefined)
  }

  /** Mark the instance as recently used so idle eviction leaves it alone. */
  touch(userId) {
    const instance = this.instances.get(userId)
    if (instance) instance.lastActivityAt = Date.now()
  }

  /**
   * Return the user's running instance, starting one if needed. Concurrent
   * calls for one user share a single startup.
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

    const starting = this.#withUserLock(userId, () => this.#ensureLocked(userId))
      .finally(() => this.pending.delete(userId))
    this.pending.set(userId, starting)
    return starting
  }

  /**
   * @param {string} userId
   * @returns {Promise<HarnessInstance>}
   */
  async #ensureLocked(userId) {
    const existing = this.get(userId)
    if (existing) return existing
    const restarting = this.instances.get(userId)
    if (restarting?.state === 'restarting' && restarting.restartTimer) {
      return this.#awaitRestart(restarting)
    }
    return this.#start(userId)
  }

  /**
   * Stop and start a user's instance; used by the admin console. The port is
   * reused when it is still free so the user's URL is stable.
   *
   * @param {string} userId
   * @returns {Promise<HarnessInstance>}
   */
  async restart(userId) {
    return this.#withUserLock(userId, async () => {
      // Capture the port before the stop clears the registry entry; the
      // restarted process should keep the user's URL whenever it is free.
      const previousPort = this.instances.get(userId)?.port ?? null
      await this.#stopLocked(userId)
      return this.#start(userId, { preferredPort: previousPort })
    })
  }

  /**
   * Stop one user's instance and drop its registry entry.
   *
   * @param {string} userId
   * @returns {Promise<boolean>} true when something was stopped or cleared
   */
  async stopUser(userId) {
    return this.#withUserLock(userId, () => this.#stopLocked(userId))
  }

  /**
   * @param {string} userId
   * @returns {Promise<boolean>}
   */
  async #stopLocked(userId) {
    const instance = this.instances.get(userId)
    this.#removeRegistry(userId)
    if (!instance) return false
    if (instance.restartTimer) {
      clearTimeout(instance.restartTimer)
      instance.restartTimer = null
    }
    instance.stopping = true
    await this.#terminate(instance)
    instance.state = 'stopped'
    this.instances.delete(userId)
    return true
  }

  /**
   * Stop every instance. `keepRegistry` leaves registry entries in place so a
   * restarted gateway can still report them; it is used when the process is
   * intentionally leaving instances running for re-adoption.
   *
   * @param {object} [options]
   * @param {boolean} [options.stopInstances] false = leave children running
   * @param {boolean} [options.keepRegistry]
   */
  async stopAll({ stopInstances = true, keepRegistry = false } = {}) {
    this.stopping = true
    if (this.idleTimer) {
      clearInterval(this.idleTimer)
      this.idleTimer = null
    }
    const instances = [...this.instances.values()]
    this.instances.clear()
    if (!stopInstances) return
    await Promise.all(instances.map(async (instance) => {
      if (instance.restartTimer) {
        clearTimeout(instance.restartTimer)
        instance.restartTimer = null
      }
      instance.stopping = true
      await this.#terminate(instance)
      instance.state = 'stopped'
      if (!keepRegistry) this.#removeRegistry(instance.userId)
    }))
  }

  /**
   * Re-adopt harness instances that survived a gateway restart.
   *
   * For every registry entry: pid alive AND port answering → adopt it as-is;
   * otherwise drop the entry (never guessing ownership of a port). A port that
   * answers with a dead pid is logged as an unknown owner and left alone.
   *
   * @returns {Promise<{ adopted: string[], dropped: string[], unknownOwner: string[] }>}
   */
  async recover() {
    const result = { adopted: [], dropped: [], unknownOwner: [] }
    for (const [userId, record] of this.readRegistry().entries()) {
      const alive = record.pid !== null && processAlive(record.pid)
      const answering = await isPortAnswering(this.config.instanceHost, record.port)
      if (alive && answering) {
        const instance = new HarnessInstance({
          userId,
          port: record.port,
          pid: record.pid,
          home: record.home || join(this.config.dshHomeRoot, userId),
          workspace: record.workspace || join(this.config.workspaceRoot, userId),
          outboxPath: join(this.config.stateRoot, 'audit', `${userId}-outbox.jsonl`),
          adopted: true,
          startedAt: record.startedAt,
        })
        instance.state = 'running'
        this.instances.set(userId, instance)
        result.adopted.push(userId)
        this.log(`adopted harness for ${userId} on port ${record.port} (pid ${record.pid})`)
      } else if (!alive && answering) {
        result.unknownOwner.push(userId)
        this.log(`port ${record.port} for ${userId} answers but pid ${record.pid} is gone; leaving it alone and starting fresh on demand`)
        this.#removeRegistry(userId)
      } else {
        result.dropped.push(userId)
        if (alive && !answering) {
          this.log(`pid ${record.pid} for ${userId} is alive but port ${record.port} does not answer; dropping the registry entry`)
        } else {
          this.log(`dropping stale registry entry for ${userId}`)
        }
        this.#removeRegistry(userId)
      }
    }
    return result
  }

  /**
   * Fire-and-forget restart of every instance a persisted session needs. Used
   * once at boot so users come back to a warm harness.
   *
   * @param {{ list: () => Array<{ userId: string }> }} sessions
   */
  reEnsure(sessions) {
    const users = new Set(sessions.list().map((record) => record.userId))
    for (const userId of users) {
      if (this.get(userId)) continue
      this.ensure(userId).then(
        (instance) => this.log(`pre-started harness for persisted session user ${userId} on port ${instance.port}`),
        (error) => this.log(`pre-start for ${userId} failed: ${error instanceof Error ? error.message : String(error)}`),
      )
    }
  }

  /**
   * Periodically stop instances that have had no activity for the configured
   * idle TTL and hold no live session. Disabled when the TTL is 0.
   *
   * @param {(userId: string) => boolean} [isUserActive]
   */
  startIdleSweeper(isUserActive = () => false) {
    const ttl = this.config.instanceIdleTtlMs
    if (!ttl || ttl <= 0) return
    const interval = Math.max(5000, Math.min(60_000, ttl))
    this.idleTimer = setInterval(() => {
      for (const instance of [...this.instances.values()]) {
        if (instance.state !== 'running') continue
        if (isUserActive(instance.userId)) continue
        if (Date.now() - instance.lastActivityAt < ttl) continue
        this.log(`stopping idle harness for ${instance.userId} (idle > ${ttl}ms)`)
        this.stopUser(instance.userId).catch((error) => {
          this.log(`idle stop for ${instance.userId} failed: ${error instanceof Error ? error.message : String(error)}`)
        })
      }
    }, interval)
    this.idleTimer.unref?.()
  }

  /** @returns {Array<Record<string, unknown>>} */
  listStatus() {
    return [...this.instances.values()].map((instance) => ({
      userId: instance.userId,
      port: instance.port,
      pid: instance.pid,
      state: instance.state,
      running: instance.running,
      adopted: instance.adopted,
      startedAt: instance.startedAt,
      lastActivityAt: instance.lastActivityAt,
      restarts: instance.restartAttempts,
      failureReason: instance.failureReason,
    }))
  }

  /** @returns {Record<string, unknown>|null} */
  statusFor(userId) {
    const instance = this.instances.get(userId)
    if (!instance) return null
    return {
      userId: instance.userId,
      port: instance.port,
      pid: instance.pid,
      state: instance.state,
      running: instance.running,
      adopted: instance.adopted,
      startedAt: instance.startedAt,
      lastActivityAt: instance.lastActivityAt,
      restarts: instance.restartAttempts,
      failureReason: instance.failureReason,
    }
  }

  /**
   * Tail a per-user log file (stdout + stderr, rotated at 2 MiB).
   *
   * @param {string} userId
   * @param {number} [tailBytes]
   * @returns {string}
   */
  readLog(userId, tailBytes = MAX_OUTPUT_BUFFER) {
    assertSafeUserId(userId)
    const path = join(this.logDir, `${userId}.log`)
    try {
      const data = readFileSync(path)
      return data.subarray(Math.max(0, data.length - tailBytes)).toString('utf8')
    } catch {
      return ''
    }
  }

  /** @returns {Map<string, Record<string, unknown>>} */
  readRegistry() {
    /** @type {Map<string, Record<string, unknown>>} */
    const entries = new Map()
    let names
    try {
      names = readdirSync(this.registryDir)
    } catch {
      return entries
    }
    for (const name of names) {
      if (!name.endsWith('.json')) continue
      const userId = name.slice(0, -'.json'.length)
      try {
        assertSafeUserId(userId)
      } catch {
        this.log(`ignoring unsafe registry entry ${name}`)
        continue
      }
      const record = readJsonFile(join(this.registryDir, name))
      if (!record || typeof record !== 'object') {
        this.log(`ignoring unreadable registry entry ${name}`)
        continue
      }
      const port = Number(record.port)
      const pid = Number(record.pid)
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        this.log(`ignoring registry entry ${name} with invalid port`)
        continue
      }
      entries.set(userId, {
        userId,
        port,
        pid: Number.isInteger(pid) && pid > 0 ? pid : null,
        startedAt: Number(record.startedAt) || Date.now(),
        home: typeof record.home === 'string' ? record.home : '',
        workspace: typeof record.workspace === 'string' ? record.workspace : '',
      })
    }
    return entries
  }

  /** @param {string} userId */
  registryPath(userId) {
    return join(this.registryDir, `${userId}.json`)
  }

  /**
   * @param {string} userId
   * @param {object} [options]
   * @param {number|null} [options.preferredPort] port to reuse when still free
   * @returns {Promise<HarnessInstance>}
   */
  async #start(userId, { preferredPort = null } = {}) {
    const { config } = this
    assertSafeUserId(userId)

    const home = join(config.dshHomeRoot, userId)
    const workspace = join(config.workspaceRoot, userId)
    const outboxPath = join(config.stateRoot, 'audit', `${userId}-outbox.jsonl`)
    // Fail before spawning when the user has no provisioned key.
    readUserToken(config.keysDir, userId)

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

    const port = await this.#choosePort(userId, preferredPort)
    const instance = new HarnessInstance({ userId, port, home, workspace, outboxPath })
    this.instances.set(userId, instance)

    this.log(`starting harness for ${userId} on port ${port}`)
    try {
      await this.#spawnInto(instance)
      await waitForReady(instance, config.instanceReadyTimeoutMs)
    } catch (error) {
      if (instance.child) instance.child.kill('SIGKILL')
      instance.state = 'failed'
      this.instances.delete(userId)
      throw error
    }
    instance.state = 'running'
    this.#writeRegistry(instance)
    return instance
  }

  /**
   * Spawn the harness into an existing instance record (initial start or
   * supervised restart) and wire its output.
   *
   * @param {HarnessInstance} instance
   * @returns {Promise<import('node:child_process').ChildProcess>}
   */
  async #spawnInto(instance) {
    const { config } = this
    const token = readUserToken(config.keysDir, instance.userId)
    const trustedHosts = trustedHostList(process.env)
    const env = {
      ...process.env,
      DSH_HOME: instance.home,
      DSH_TELEMETRY_DISABLED: '1',
      SYSADMIN_USER: instance.userId,
      SYSADMIN_TOKEN: token,
      SYSADMIN_LITELLM_KEY: token,
      SYSADMIN_LITELLM_URL: config.litellmUrl,
      SYSADMIN_BACKEND_URL: config.agentUrl,
      SYSADMIN_AUTH_URL: config.authUrl,
      VICTORIALOGS_URL: config.victoriaLogsUrl,
      SYSADMIN_AUDIT_OUTBOX: instance.outboxPath,
      SYSADMIN_HARNESS_HOST: config.instanceHost,
      SYSADMIN_HARNESS_PORT: String(instance.port),
      // Port-less LAN addresses plus SYSADMIN_TRUSTED_HOSTS. The profile
      // hands these to the harness Origin fence so a browser on the gateway
      // port is not treated as a cross-origin stranger.
      SYSADMIN_TRUSTED_HOSTS: trustedHosts.join(','),
    }
    this.log(`harness for ${instance.userId} trusts ${trustedHosts.length > 0 ? trustedHosts.join(',') : 'loopback only'}`)
    const child = spawn(
      config.dshBin,
      ['--profile', config.profileName, '--no-open', '--port', String(instance.port)],
      { cwd: instance.workspace, env, stdio: ['ignore', 'pipe', 'pipe'] },
    )
    instance.attachChild(child)

    const onData = (chunk) => {
      instance.observe(chunk)
      this.#appendLog(instance.userId, String(chunk))
    }
    child.stdout?.setEncoding('utf8')
    child.stderr?.setEncoding('utf8')
    child.stdout?.on('data', onData)
    child.stderr?.on('data', onData)
    child.on('error', (error) => {
      onData(`spawn error: ${error instanceof Error ? error.message : String(error)}\n`)
    })
    child.on('exit', (code, signal) => this.#onExit(instance, child, code, signal))
    return child
  }

  /**
   * @param {HarnessInstance} instance
   * @param {import('node:child_process').ChildProcess} child
   * @param {number|null} code
   * @param {NodeJS.Signals|null} signal
   */
  #onExit(instance, child, code, signal) {
    if (instance.child !== child) return
    instance.child = null
    instance.exitCode = code
    instance.exitSignal = signal
    this.log(`harness for ${instance.userId} exited (code=${code} signal=${signal})`)
    if (this.stopping || instance.stopping || instance.state === 'stopped') {
      instance.state = 'stopped'
      return
    }
    if (instance.restartAttempts >= this.config.restartMaxAttempts) {
      instance.state = 'failed'
      instance.failureReason = `exited (code=${code} signal=${signal}); last output: ${instance.stderrTail.slice(-400).trim()}`
      this.log(`harness for ${instance.userId} failed after ${instance.restartAttempts} restart(s)`)
      return
    }
    this.#scheduleRestart(instance)
  }

  /** @param {HarnessInstance} instance */
  #scheduleRestart(instance) {
    if (instance.restartTimer || this.stopping) return
    instance.restartAttempts += 1
    instance.state = 'restarting'
    const delayMs = Math.min(this.config.restartBackoffMs * 2 ** (instance.restartAttempts - 1), 30_000)
    this.log(`restarting harness for ${instance.userId} in ${delayMs}ms (attempt ${instance.restartAttempts}/${this.config.restartMaxAttempts})`)
    instance.restartTimer = setTimeout(() => {
      instance.restartTimer = null
      this.#restart(instance).catch((error) => {
        this.log(`restart for ${instance.userId} crashed: ${error instanceof Error ? error.message : String(error)}`)
      })
    }, delayMs)
    instance.restartTimer.unref?.()
  }

  /** @param {HarnessInstance} instance */
  async #restart(instance) {
    if (this.stopping || instance.stopping) return
    instance.launchToken = null
    instance.stdout = ''
    try {
      await this.#spawnInto(instance)
      await waitForReady(instance, this.config.instanceReadyTimeoutMs)
      instance.state = 'running'
      instance.failureReason = null
      this.#writeRegistry(instance)
      this.log(`harness for ${instance.userId} restarted on port ${instance.port} (attempt ${instance.restartAttempts})`)
    } catch (error) {
      if (instance.child) {
        instance.child.kill('SIGKILL')
        instance.child = null
      }
      const message = error instanceof Error ? error.message : String(error)
      this.log(`restart attempt ${instance.restartAttempts} for ${instance.userId} failed: ${message}`)
      if (instance.restartAttempts >= this.config.restartMaxAttempts) {
        instance.state = 'failed'
        instance.failureReason = message
      } else if (!instance.restartTimer) {
        this.#scheduleRestart(instance)
      }
    }
  }

  /**
   * Join an in-flight supervised restart, or start fresh when it gave up.
   *
   * @param {HarnessInstance} instance
   * @returns {Promise<HarnessInstance>}
   */
  async #awaitRestart(instance) {
    const deadline = Date.now() + this.config.instanceReadyTimeoutMs + 10_000
    while (Date.now() < deadline) {
      if (instance.state === 'running' && instance.running) return instance
      if (instance.state === 'failed' || instance.state === 'stopped') break
      await delay(200)
    }
    if (instance.state === 'running' && instance.running) return instance
    return this.#start(instance.userId)
  }

  /**
   * Reuse the requested or recorded port when it is still free, so a user's
   * URL survives a restart; otherwise allocate a new one.
   *
   * @param {string} userId
   * @param {number|null} [preferredPort]
   * @returns {Promise<number>}
   */
  async #choosePort(userId, preferredPort = null) {
    const { config } = this
    const inRange = (port) => (
      Number.isInteger(port) && port >= config.instancePortStart && port <= config.instancePortEnd
    )
    const candidates = []
    if (inRange(preferredPort)) candidates.push(preferredPort)
    const recorded = Number(readJsonFile(this.registryPath(userId))?.port)
    if (inRange(recorded) && recorded !== preferredPort) candidates.push(recorded)

    for (const port of candidates) {
      const held = [...this.instances.values()].some(
        (instance) => instance.userId !== userId && instance.running && instance.port === port,
      )
      if (!held && await portIsFree(config.instanceHost, port)) return port
    }
    return findFreePort(config.instanceHost, config.instancePortStart, config.instancePortEnd)
  }

  /** @param {HarnessInstance} instance */
  #writeRegistry(instance) {
    try {
      writeFileAtomic(this.registryPath(instance.userId), `${JSON.stringify({
        userId: instance.userId,
        port: instance.port,
        pid: instance.pid,
        startedAt: instance.startedAt,
        home: instance.home,
        workspace: instance.workspace,
      }, null, 2)}\n`)
    } catch (error) {
      this.log(`failed to persist registry entry for ${instance.userId}: ${error instanceof Error ? error.message : String(error)}`)
    }
  }

  /** @param {string} userId */
  #removeRegistry(userId) {
    removeFile(this.registryPath(userId))
  }

  /**
   * Graceful SIGTERM, then SIGKILL when the process ignores it.
   *
   * @param {HarnessInstance} instance
   */
  async #terminate(instance) {
    if (!instance.running) return
    instance.stop('SIGTERM')
    const deadline = Date.now() + STOP_GRACE_MS
    while (Date.now() < deadline && instance.running) await delay(100)
    if (instance.running) {
      this.log(`harness for ${instance.userId} ignored SIGTERM; sending SIGKILL`)
      instance.stop('SIGKILL')
      for (let i = 0; i < 20 && instance.running; i += 1) await delay(50)
    }
  }

  /**
   * Append to the per-user, size-rotated log file.
   *
   * @param {string} userId
   * @param {string} text
   */
  #appendLog(userId, text) {
    try {
      const path = join(this.logDir, `${userId}.log`)
      let size = this.logSizes.get(userId)
      if (size === undefined) {
        try {
          size = statSync(path).size
        } catch {
          size = 0
        }
      }
      if (size > MAX_INSTANCE_LOG_BYTES) {
        try {
          renameSync(path, `${path}.1`)
        } catch {
          /* rotation is best effort */
        }
        size = 0
      }
      mkdirSync(this.logDir, { recursive: true, mode: 0o700 })
      appendFileSync(path, text, { mode: 0o600 })
      this.logSizes.set(userId, size + Buffer.byteLength(text))
    } catch {
      /* logging must never break the harness */
    }
  }

  /**
   * Serialize operations per user so an admin restart and a proxy request
   * cannot spawn two processes for the same identity.
   *
   * @template T
   * @param {string} key
   * @param {() => Promise<T>} fn
   * @returns {Promise<T>}
   */
  #withUserLock(key, fn) {
    const previous = this.userLocks.get(key) ?? Promise.resolve()
    const run = previous.then(() => fn())
    this.userLocks.set(key, run.catch(() => {}))
    return run
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
 * True when an HTTP listener accepts a connection on this port, whatever the
 * status code. Connection errors and timeouts count as "not answering".
 *
 * @param {string} host
 * @param {number} port
 * @param {number} [timeoutMs]
 * @returns {Promise<boolean>}
 */
export async function isPortAnswering(host, port, timeoutMs = 1500) {
  try {
    await fetch(`http://${host}:${port}/`, { signal: AbortSignal.timeout(timeoutMs), redirect: 'manual' })
    return true
  } catch {
    return false
  }
}

/**
 * @param {number} pid
 * @returns {boolean}
 */
export function processAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 1) return false
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return error?.code === 'EPERM'
  }
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
        `harness for ${instance.userId} exited during startup (code=${instance.exitCode})\n${instance.stdout.slice(-2000)}`,
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
