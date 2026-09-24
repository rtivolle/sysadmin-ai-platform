/**
 * Admin console for the multi-user harness gateway.
 *
 * Authorization model: the operator holds `backend/config/keys/master.key` —
 * the same secret the backend accepts as the `sysadmin-admin` bearer. The
 * console verifies it with a constant-time compare against the key file and a
 * bearer call to a backend admin route, then issues its own file-persisted
 * HttpOnly cookie (`sysadmin_admin`, 12 h). No backend auth or role changes are
 * involved, and neither the master token nor any user key is ever echoed back
 * or written to a state file.
 *
 * Every `/api/admin/*` mutation additionally requires a JSON content type, the
 * `X-Sysadmin-Admin: 1` header and a same-origin `Origin` when the browser
 * sends one, which keeps cross-site requests out of a SameSite=Lax cookie.
 */
import { spawn } from 'node:child_process'
import { createHash, timingSafeEqual } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { connect as netConnect } from 'node:net'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { readBody, sendJson, sendText } from './http-util.js'
import { assertSafeUserId } from './instance-manager.js'
import { parseCookies } from './session-store.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const ADMIN_USER = 'sysadmin-admin'
const MAX_ADMIN_BODY_BYTES = 8 * 1024
const PROBE_TIMEOUT_MS = 2500
const SERVICE_RESTART_TIMEOUT_MS = 30_000
const MAX_LOG_TAIL_BYTES = 200 * 1024
const MAX_AUDIT_EVENTS = 500
const BACKEND_TIMEOUT_MS = 10_000

/** Explicit static routes: no path parsing, no traversal surface. */
const STATIC_FILES = {
  '/assets/mila-logo.png': { file: join(HERE, 'assets', 'mila-logo.png'), type: 'image/png' },
  '/assets/brand.css': { file: join(HERE, 'assets', 'brand.css'), type: 'text/css; charset=utf-8' },
  '/admin-ui.js': { file: join(HERE, 'admin-ui.js'), type: 'text/javascript; charset=utf-8' },
  '/admin-ui.css': { file: join(HERE, 'admin-ui.css'), type: 'text/css; charset=utf-8' },
  '/favicon.ico': { file: join(HERE, 'assets', 'mila-logo.png'), type: 'image/png' },
}

/**
 * @param {object} options
 * @param {ReturnType<import('./config.js').loadConfig>} options.config
 * @param {import('./session-store.js').SessionStore} options.sessions
 * @param {import('./session-store.js').SessionStore} options.adminSessions
 * @param {import('./instance-manager.js').InstanceManager} options.instances
 * @param {(message: string, meta?: unknown) => void} [options.logger]
 */
export function createAdminConsole({ config, sessions, adminSessions, instances, logger = () => {} }) {
  const log = logger
  /** @type {Map<string, Promise<unknown>>} */
  const locks = new Map()

  /**
   * @param {import('node:http').IncomingMessage} req
   * @param {import('node:http').ServerResponse} res
   * @param {URL} url
   * @returns {Promise<boolean>} true when the request was handled here
   */
  async function handle(req, res, url) {
    const { pathname } = url

    if (req.method === 'GET' && STATIC_FILES[pathname]) {
      const asset = STATIC_FILES[pathname]
      try {
        const body = readFileSync(asset.file)
        res.writeHead(200, {
          'content-type': asset.type,
          'content-length': body.length,
          'cache-control': 'no-store',
        })
        res.end(body)
      } catch {
        sendJson(res, 404, { detail: 'asset missing' })
      }
      return true
    }

    if (pathname === '/admin' || pathname === '/admin/') {
      return serveAdminPage(req, res)
    }

    if (!pathname.startsWith('/api/admin/')) return false

    // Unauthenticated probe used by the console to choose login vs console.
    if (pathname === '/api/admin/session' && req.method === 'GET') {
      const record = adminRecord(req)
      return sendJson(res, 200, {
        authenticated: Boolean(record),
        user: record ? ADMIN_USER : null,
        gateway: { port: config.port, backendUrl: config.agentUrl },
      })
    }

    if (pathname === '/api/admin/login' && req.method === 'POST') {
      await handleLogin(req, res)
      return true
    }

    const session = adminRecord(req)
    if (!session) {
      sendJson(res, 401, { detail: 'Admin authentication required' })
      return true
    }

    if (pathname === '/api/admin/logout' && req.method === 'POST') {
      handleLogout(req, res)
      return true
    }

    if (req.method === 'POST' && !requireMutation(req, res)) return true

    try {
      const handled = await route(req, res, url)
      if (!handled) sendJson(res, 404, { detail: 'Unknown admin endpoint' })
    } catch (error) {
      log(`admin request ${pathname} failed: ${error instanceof Error ? error.stack ?? error.message : String(error)}`)
      if (!res.headersSent) sendJson(res, 500, { detail: 'admin endpoint failed' })
      else res.end()
    }
    return true
  }

  /**
   * @param {import('node:http').IncomingMessage} req
   * @param {import('node:http').ServerResponse} res
   * @param {URL} url
   * @returns {Promise<boolean>}
   */
  async function route(req, res, url) {
    const { pathname } = url

    if (req.method === 'GET' && pathname === '/api/admin/overview') {
      sendJson(res, 200, await buildOverview())
      return true
    }

    if (req.method === 'GET' && pathname === '/api/admin/sessions') {
      sendJson(res, 200, { sessions: listSessions() })
      return true
    }

    let match = pathname.match(/^\/api\/admin\/sessions\/([A-Za-z0-9._-]+)\/revoke$/)
    if (req.method === 'POST' && match) {
      await revokeSession(req, res, match[1])
      return true
    }

    if (req.method === 'GET' && pathname === '/api/admin/instances') {
      sendJson(res, 200, { instances: instances.listStatus() })
      return true
    }

    if (req.method === 'GET' && pathname === '/api/admin/quotas') {
      await proxyQuotas(req, res)
      return true
    }

    const quotaUser = pathname.match(/^\/api\/admin\/quotas\/([A-Za-z0-9._-]+)$/)
    if (req.method === 'POST' && quotaUser) {
      await proxyQuotas(req, res, quotaUser[1])
      return true
    }

    if (req.method === 'GET' && pathname === '/api/admin/models') {
      await listModels(res)
      return true
    }

    if (req.method === 'GET' && pathname === '/api/admin/survey') {
      await proxySurvey(res, url)
      return true
    }

    if (req.method === 'GET' && pathname === '/api/admin/local-models') {
      await proxyLocalModels(req, res, '/api/v1/models', 'GET')
      return true
    }

    if (req.method === 'POST' && pathname === '/api/admin/local-models') {
      await proxyLocalModels(req, res, '/api/v1/models', 'POST')
      return true
    }

    match = pathname.match(/^\/api\/admin\/local-models\/([A-Za-z0-9._-]+)\/(download|start|stop|restart|delete)$/)
    if (req.method === 'POST' && match) {
      const [, name, action] = match
      const query = action === 'delete' && url.searchParams.get('delete_files') === '1' ? '?delete_files=1' : ''
      const agentPath = action === 'delete'
        ? `/api/v1/models/${encodeURIComponent(name)}${query}`
        : `/api/v1/models/${encodeURIComponent(name)}/${action}`
      await proxyLocalModels(req, res, agentPath, action === 'delete' ? 'DELETE' : 'POST')
      return true
    }

    match = pathname.match(/^\/api\/admin\/local-models\/([A-Za-z0-9._-]+)\/logs$/)
    if (req.method === 'GET' && match) {
      const tail = url.searchParams.get('tail')
      const suffix = tail ? `?tail=${encodeURIComponent(tail)}` : ''
      await proxyLocalModels(req, res, `/api/v1/models/${encodeURIComponent(match[1])}/logs${suffix}`, 'GET')
      return true
    }

    match = pathname.match(/^\/api\/admin\/instances\/([A-Za-z0-9._-]+)\/logs$/)
    if (req.method === 'GET' && match) {
      handleInstanceLogs(res, url, match[1])
      return true
    }

    match = pathname.match(/^\/api\/admin\/instances\/([A-Za-z0-9._-]+)\/(start|stop|restart)$/)
    if (req.method === 'POST' && match) {
      await handleInstanceAction(req, res, match[1], match[2])
      return true
    }

    if (req.method === 'GET' && pathname === '/api/admin/users') {
      sendJson(res, 200, { users: listUsers() })
      return true
    }

    match = pathname.match(/^\/api\/admin\/users\/([A-Za-z0-9._-]+)\/rotate-password$/)
    if (req.method === 'POST' && match) {
      await handleRotatePassword(req, res, match[1])
      return true
    }

    if (req.method === 'GET' && pathname === '/api/admin/approvals') {
      await proxyApprovals(req, res)
      return true
    }

    if (req.method === 'POST' && pathname === '/api/admin/approvals/decide') {
      await proxyApprovalDecision(req, res)
      return true
    }

    if (req.method === 'GET' && pathname === '/api/admin/audit') {
      await handleAudit(res, url)
      return true
    }

    if (req.method === 'GET' && pathname === '/api/admin/services') {
      sendJson(res, 200, { services: listServices() })
      return true
    }

    match = pathname.match(/^\/api\/admin\/services\/([A-Za-z0-9._-]+)\/(start|stop|restart)$/)
    if (req.method === 'POST' && match) {
      await handleServiceAction(req, res, match[1], match[2])
      return true
    }

    return false
  }

  // ── Page and static serving ───────────────────────────────────────────────

  async function serveAdminPage(req, res) {
    try {
      const body = readFileSync(join(HERE, 'admin-ui.html'), 'utf8')
      sendText(res, 200, body, 'text/html; charset=utf-8', { 'cache-control': 'no-store' })
    } catch (error) {
      sendJson(res, 500, { detail: `admin UI missing: ${error instanceof Error ? error.message : String(error)}` })
    }
  }

  // ── Authentication ────────────────────────────────────────────────────────

  /** @param {import('node:http').IncomingMessage} req */
  function adminRecord(req) {
    const cookies = parseCookies(req.headers.cookie)
    const record = adminSessions.get(cookies[config.adminCookieName])
    if (!record || record.userId !== ADMIN_USER) return null
    return record
  }

  /**
   * CSRF hardening for mutations: JSON content type, an explicit custom header
   * no cross-site form can set, and a same-origin `Origin` when present.
   *
   * @param {import('node:http').IncomingMessage} req
   * @param {import('node:http').ServerResponse} res
   */
  function requireMutation(req, res) {
    if (!String(req.headers['content-type'] ?? '').toLowerCase().includes('application/json')) {
      sendJson(res, 415, { detail: 'admin requests must be application/json' })
      return false
    }
    if (req.headers['x-sysadmin-admin'] !== '1') {
      sendJson(res, 403, { detail: 'missing X-Sysadmin-Admin header' })
      return false
    }
    const origin = req.headers.origin
    if (origin) {
      let sameOrigin = false
      try {
        sameOrigin = new URL(origin).host === req.headers.host
      } catch {
        sameOrigin = false
      }
      if (!sameOrigin) {
        sendJson(res, 403, { detail: 'cross-origin admin request rejected' })
        return false
      }
    }
    return true
  }

  /** @param {string} candidate */
  function verifyMasterToken(candidate) {
    let expected = ''
    try {
      expected = readFileSync(config.masterKeyFile, 'utf8').trim()
    } catch {
      log(`master key file unreadable: ${config.masterKeyFile}`)
      return false
    }
    if (!expected || !candidate) return false
    const left = Buffer.from(candidate)
    const right = Buffer.from(expected)
    return left.length === right.length && timingSafeEqual(left, right)
  }

  /**
   * Confirm the token is accepted as an administrator by the backend. An
   * explicit 401/403 rejects the login; an unreachable backend is allowed with
   * a warning, because the local key file is the authority and the console must
   * stay usable exactly when services are down.
   *
   * @param {string} token
   * @returns {Promise<{ denied: boolean, verified: boolean, note?: string }>}
   */
  async function probeAdminBackend(token) {
    try {
      const response = await fetch(`${config.agentUrl}/api/approvals/pending`, {
        headers: { authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(5000),
      })
      if (response.status === 401 || response.status === 403) {
        await response.text().catch(() => '')
        return { denied: true, verified: false }
      }
      return { denied: false, verified: response.ok }
    } catch (error) {
      return {
        denied: false,
        verified: false,
        note: `backend unreachable: ${error instanceof Error ? error.message : String(error)}`,
      }
    }
  }

  async function handleLogin(req, res) {
    if (!requireMutation(req, res)) return
    let raw
    try {
      raw = await readBody(req, MAX_ADMIN_BODY_BYTES)
    } catch {
      return sendJson(res, 413, { detail: 'request body too large' })
    }
    let token = ''
    try {
      token = String(JSON.parse(raw)?.token ?? '')
    } catch {
      return sendJson(res, 400, { detail: 'Invalid JSON body' })
    }
    if (!verifyMasterToken(token)) {
      log('admin login rejected: token does not match the master key file')
      return sendJson(res, 401, { detail: 'Invalid master token' })
    }
    const backend = await probeAdminBackend(token)
    if (backend.denied) {
      log('admin login rejected: backend refused the master token')
      return sendJson(res, 401, { detail: 'Master token rejected by the backend' })
    }
    if (backend.note) log(`admin login accepted without backend verification (${backend.note})`)
    const sessionId = adminSessions.create(ADMIN_USER)
    res.setHeader('set-cookie', cookieHeader(sessionId))
    log('admin login accepted')
    return sendJson(res, 200, {
      status: 'authenticated',
      user: ADMIN_USER,
      backendVerified: backend.verified,
      ttlMs: config.adminTtlMs,
    })
  }

  /** @param {string} sessionId */
  function cookieHeader(sessionId) {
    const maxAge = Math.max(1, Math.floor(config.adminTtlMs / 1000))
    return `${config.adminCookieName}=${sessionId}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAge}`
  }

  function handleLogout(req, res) {
    const cookies = parseCookies(req.headers.cookie)
    adminSessions.delete(cookies[config.adminCookieName])
    res.setHeader('set-cookie', `${config.adminCookieName}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0`)
    log('admin logout')
    return sendJson(res, 200, { status: 'logged_out' })
  }

  // ── Overview ──────────────────────────────────────────────────────────────

  async function buildOverview() {
    const probes = serviceProbes()
    const services = await Promise.all(probes.map(probeService))
    services.push({ name: 'harness_gateway', status: 'up', latencyMs: 0, detail: `this process on port ${config.port}` })
    return {
      status: 'ok',
      gateway: {
        port: config.port,
        backendUrl: config.agentUrl,
        uptimeMs: Date.now() - startedAt,
        startedAt,
        sessions: sessions.list().length,
        adminSessions: adminSessions.list().length,
        activeInstances: instances.activeUsers().length,
        knownInstances: instances.listStatus().length,
        persistence: {
          sessionsFile: config.sessionsFile,
          registryDir: config.instanceRegistryDir,
          logDir: config.instanceLogDir,
        },
      },
      services,
    }
  }

  const startedAt = Date.now()

  function serviceProbes() {
    const litellmBase = config.litellmUrl.replace(/\/v1\/?$/, '')
    return [
      { name: 'auth_gateway', url: `${config.authUrl}/health` },
      { name: 'agent_tools', url: `${config.agentUrl}/health` },
      { name: 'litellm', url: `${litellmBase}/health/liveliness` },
      { name: 'victorialogs', url: `${config.victoriaLogsUrl}/health` },
      { name: 'traefik', url: `http://127.0.0.1:${config.services.traefik.port}/ping` },
      { name: 'inference', url: `http://127.0.0.1:${config.services.inference.port}/health` },
      { name: 'seaweedfs', url: `http://127.0.0.1:${config.services.seaweedfs.masterPort ?? 9333}/cluster/status` },
      { name: 'valkey', tcp: { host: '127.0.0.1', port: config.services.valkey.port } },
    ]
  }

  /** @param {{ name: string, url?: string, tcp?: { host: string, port: number } }} definition */
  async function probeService(definition) {
    const started = Date.now()
    if (definition.tcp) {
      const reachable = await tcpCheck(definition.tcp.host, definition.tcp.port)
      return {
        name: definition.name,
        status: reachable ? 'up' : 'down',
        latencyMs: Date.now() - started,
        detail: reachable ? `tcp ${definition.tcp.host}:${definition.tcp.port}` : 'connection refused',
      }
    }
    try {
      const response = await fetch(definition.url, { signal: AbortSignal.timeout(PROBE_TIMEOUT_MS), redirect: 'manual' })
      return {
        name: definition.name,
        status: response.status < 500 ? 'up' : 'degraded',
        httpStatus: response.status,
        latencyMs: Date.now() - started,
        detail: `HTTP ${response.status}`,
        url: definition.url,
      }
    } catch (error) {
      return {
        name: definition.name,
        status: 'down',
        latencyMs: Date.now() - started,
        detail: error instanceof Error ? error.message : String(error),
        url: definition.url,
      }
    }
  }

  /**
   * @param {string} host
   * @param {number} port
   * @returns {Promise<boolean>}
   */
  function tcpCheck(host, port) {
    return new Promise((resolve) => {
      const socket = netConnect({ host, port })
      const done = (result) => {
        socket.destroy()
        resolve(result)
      }
      socket.setTimeout(2000, () => done(false))
      socket.once('connect', () => done(true))
      socket.once('error', () => done(false))
    })
  }

  // ── Sessions ──────────────────────────────────────────────────────────────

  /** Opaque, non-reversible handle for a browser cookie value. */
  function shortId(sessionId) {
    return createHash('sha256').update(sessionId).digest('hex').slice(0, 16)
  }

  function listSessions() {
    return sessions.list().map((record) => ({
      id: shortId(record.id),
      userId: record.userId,
      createdAt: record.createdAt,
      lastSeenAt: record.lastSeenAt,
      expiresAt: record.expiresAt,
      instance: instances.statusFor(record.userId),
    }))
  }

  async function revokeSession(req, res, handle) {
    const body = await readJsonBody(req, res)
    if (body === undefined) return
    const record = sessions.list().find((candidate) => shortId(candidate.id) === handle)
    if (!record) return sendJson(res, 404, { detail: 'session not found' })
    sessions.delete(record.id)
    let stoppedInstance = false
    if (body.stopInstance === true) {
      stoppedInstance = await instances.stopUser(record.userId)
    }
    log(`admin revoked session for ${record.userId} (stopInstance=${stoppedInstance})`)
    return sendJson(res, 200, { status: 'revoked', user: record.userId, stoppedInstance })
  }

  // ── Instances ─────────────────────────────────────────────────────────────

  async function handleInstanceAction(req, res, userId, action) {
    try {
      assertSafeUserId(userId)
    } catch (error) {
      return sendJson(res, 400, { detail: error instanceof Error ? error.message : 'invalid user id' })
    }
    return withLock(`instance:${userId}`, async () => {
      try {
        if (action === 'start') await instances.ensure(userId)
        else if (action === 'stop') await instances.stopUser(userId)
        else await instances.restart(userId)
      } catch (error) {
        return sendJson(res, 502, {
          detail: `instance ${action} failed: ${error instanceof Error ? error.message : String(error)}`,
          instance: instances.statusFor(userId),
        })
      }
      const statusLabel = { start: 'started', stop: 'stopped', restart: 'restarted' }[action] ?? action
      log(`admin ${action} harness for ${userId}`)
      return sendJson(res, 200, { status: statusLabel, instance: instances.statusFor(userId) })
    })
  }

  function handleInstanceLogs(res, url, userId) {
    try {
      assertSafeUserId(userId)
    } catch (error) {
      return sendJson(res, 400, { detail: error instanceof Error ? error.message : 'invalid user id' })
    }
    const requested = Number.parseInt(url.searchParams.get('tail') ?? '', 10)
    const tailBytes = Number.isFinite(requested)
      ? Math.min(Math.max(requested, 1024), MAX_LOG_TAIL_BYTES)
      : 32 * 1024
    const text = instances.readLog(userId, tailBytes)
    return sendText(res, 200, text || `(no log output for ${userId})`, 'text/plain; charset=utf-8', {
      'cache-control': 'no-store',
    })
  }

  // ── Users ─────────────────────────────────────────────────────────────────

  function listUsers() {
    let credentials = {}
    try {
      credentials = JSON.parse(readFileSync(config.credentialsFile, 'utf8'))
    } catch {
      credentials = {}
    }
    return Object.keys(credentials).sort().map((userId) => ({
      userId,
      hasKey: existsSync(join(config.keysDir, `${userId}.key`)),
      activeSessions: sessions.list().filter((record) => record.userId === userId).length,
      instance: instances.statusFor(userId),
    }))
  }

  async function handleRotatePassword(req, res, userId) {
    const users = listUsers()
    if (!users.some((user) => user.userId === userId)) {
      return sendJson(res, 404, { detail: `unknown user ${userId}` })
    }
    if (!existsSync(config.provisionLoginsScript)) {
      return sendJson(res, 501, { detail: `provisioning script not found: ${config.provisionLoginsScript}` })
    }
    // The body is optional on this endpoint; drain it so keep-alive stays sane.
    if (Number(req.headers['content-length'] ?? 0) > 0) {
      try {
        await readBody(req, MAX_ADMIN_BODY_BYTES)
      } catch {
        return sendJson(res, 413, { detail: 'request body too large' })
      }
    }
    return withLock('users', async () => {
      const result = await runCommand(
        config.pythonBin,
        [config.provisionLoginsScript, '--user', userId, '--rotate'],
        { cwd: dirname(config.provisionLoginsScript), timeoutMs: SERVICE_RESTART_TIMEOUT_MS },
      )
      const output = tail(result.output, 4000)
      if (result.exitCode !== 0) {
        return sendJson(res, 500, { detail: `rotation failed for ${userId}`, output })
      }
      log(`admin rotated the login password for ${userId}`)
      return sendJson(res, 200, {
        status: 'rotated',
        user: userId,
        passwordFile: config.initialPasswordsFile,
        note: 'deliver the new password securely, then remove the plaintext file',
        output,
      })
    })
  }

  // ── Approvals ─────────────────────────────────────────────────────────────

  async function proxyApprovals(req, res) {
    const token = masterToken()
    if (!token) return sendJson(res, 503, { detail: 'master key file unavailable', note: 'approvals need the backend admin bearer' })
    try {
      const response = await fetch(`${config.agentUrl}/api/approvals/pending`, {
        headers: { authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(BACKEND_TIMEOUT_MS),
      })
      const body = await response.json().catch(() => ({}))
      return sendJson(res, response.status, body)
    } catch (error) {
      return sendJson(res, 503, {
        detail: `agent platform unreachable: ${error instanceof Error ? error.message : String(error)}`,
        note: 'approvals fail closed while the backend is down',
      })
    }
  }

  async function proxyApprovalDecision(req, res) {
    const token = masterToken()
    if (!token) return sendJson(res, 503, { detail: 'master key file unavailable' })
    let raw
    try {
      raw = await readBody(req, MAX_ADMIN_BODY_BYTES)
    } catch {
      return sendJson(res, 413, { detail: 'request body too large' })
    }
    let decision
    try {
      decision = JSON.parse(raw)
    } catch {
      return sendJson(res, 400, { detail: 'Invalid JSON body' })
    }
    if (typeof decision?.approval_id !== 'string' || typeof decision?.approved !== 'boolean') {
      return sendJson(res, 400, { detail: 'approval_id (string) and approved (boolean) are required' })
    }
    try {
      const response = await fetch(`${config.agentUrl}/api/approvals/decide`, {
        method: 'POST',
        headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
        body: JSON.stringify({ approval_id: decision.approval_id, approved: decision.approved }),
        signal: AbortSignal.timeout(BACKEND_TIMEOUT_MS),
      })
      const body = await response.json().catch(() => ({}))
      log(`admin decided approval ${decision.approval_id}: approved=${decision.approved} (HTTP ${response.status})`)
      return sendJson(res, response.status, body)
    } catch (error) {
      return sendJson(res, 503, {
        detail: `agent platform unreachable: ${error instanceof Error ? error.message : String(error)}`,
      })
    }
  }

  // ── Audit ─────────────────────────────────────────────────────────────────

  async function proxyQuotas(req, res, userId) {
    const token = masterToken()
    if (!token) return sendJson(res, 503, { detail: 'master key file unavailable' })
    const body = userId ? await readJsonBody(req, res) : undefined
    if (userId && body === undefined) return
    try {
      const response = await fetch(`${config.authUrl}/api/v1/admin/quotas${userId ? `/${encodeURIComponent(userId)}` : ''}`, {
        method: userId ? 'POST' : 'GET',
        headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
        body: userId ? JSON.stringify(body) : undefined,
        redirect: 'error',
        signal: AbortSignal.timeout(BACKEND_TIMEOUT_MS),
      })
      const result = await response.json()
      return sendJson(res, response.status, result)
    } catch {
      return sendJson(res, 503, { detail: 'Quota service unavailable; limits were not confirmed. Refresh before retrying.' })
    }
  }

  async function listModels(res) {
    const token = masterToken()
    if (!token) return sendJson(res, 503, { detail: 'master key file unavailable' })
    try {
      const response = await fetch(`${config.litellmUrl.replace(/\/+$/, '')}/models`, {
        headers: { authorization: `Bearer ${token}` }, redirect: 'error',
        signal: AbortSignal.timeout(BACKEND_TIMEOUT_MS),
      })
      if (!response.ok) return sendJson(res, 503, { detail: `LiteLLM model catalog unavailable (HTTP ${response.status})` })
      const body = await response.json()
      if (!Array.isArray(body.data)) throw new Error('Invalid model catalog')
      let inferenceMode = 'unknown'
      try {
        const health = await fetch(`http://127.0.0.1:${config.services.inference.port}/health`, {
          signal: AbortSignal.timeout(PROBE_TIMEOUT_MS), redirect: 'error',
        })
        if (health.ok) {
          const status = await health.json()
          if (status.upstream_vllm === 'local-simulated') inferenceMode = 'simulated'
          else if (typeof status.upstream_vllm === 'string' && status.upstream_vllm) inferenceMode = 'vllm-proxy'
        }
      } catch { /* Catalog remains usable if the inference health probe fails. */ }
      // Never proxy provider configuration or credentials into the browser.
      const models = body.data.filter((model) => typeof model?.id === 'string').map((model) => ({
        id: model.id, owned_by: typeof model.owned_by === 'string' ? model.owned_by : '',
      }))
      return sendJson(res, 200, { models, inferenceMode, source: 'LiteLLM /models' })
    } catch {
      return sendJson(res, 503, { detail: 'LiteLLM model catalog unreachable' })
    }
  }

  async function proxySurvey(res, url) {
    const token = masterToken()
    if (!token) return sendJson(res, 503, { detail: 'master key file unavailable' })
    const refresh = url.searchParams.get('refresh') === '1' ? '?refresh=1' : ''
    try {
      const response = await fetch(`${config.agentUrl}/api/v1/survey${refresh}`, {
        headers: { authorization: `Bearer ${token}` }, redirect: 'error',
        signal: AbortSignal.timeout(SERVICE_RESTART_TIMEOUT_MS),
      })
      const text = await response.text()
      let result
      try { result = text ? JSON.parse(text) : {} } catch { result = { detail: tail(text, 500) } }
      return sendJson(res, response.status, result)
    } catch {
      return sendJson(res, 503, { detail: 'Hardware survey unavailable' })
    }
  }

  async function proxyLocalModels(req, res, agentPath, method) {
    const token = masterToken()
    if (!token) return sendJson(res, 503, { detail: 'master key file unavailable' })
    const needsBody = method === 'POST' && agentPath === '/api/v1/models'
    let body
    if (needsBody) {
      body = await readJsonBody(req, res)
      if (body === undefined) return
    }
    try {
      const response = await fetch(`${config.agentUrl}${agentPath}`, {
        method,
        headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
        body: needsBody ? JSON.stringify(body) : undefined,
        redirect: 'error',
        // stop() waits for vLLM to exit; allow the 30 s supervision budget.
        signal: AbortSignal.timeout(SERVICE_RESTART_TIMEOUT_MS),
      })
      const text = await response.text()
      let result
      try { result = text ? JSON.parse(text) : {} } catch { result = { detail: tail(text, 500) } }
      return sendJson(res, response.status, result)
    } catch {
      return sendJson(res, 503, { detail: 'Agent platform model API unreachable' })
    }
  }

  async function handleAudit(res, url) {
    const query = (url.searchParams.get('query') ?? '*').trim() || '*'
    const requested = Number.parseInt(url.searchParams.get('limit') ?? '', 10)
    const limit = Number.isFinite(requested)
      ? Math.min(Math.max(requested, 1), MAX_AUDIT_EVENTS)
      : 100
    const target = new URL('/select/logsql/query', config.victoriaLogsUrl)
    target.searchParams.set('query', query)
    target.searchParams.set('limit', String(limit))
    try {
      const response = await fetch(target, { signal: AbortSignal.timeout(BACKEND_TIMEOUT_MS) })
      if (!response.ok) {
        const body = await response.text().catch(() => '')
        return sendJson(res, 503, {
          detail: `VictoriaLogs query failed (HTTP ${response.status})`,
          note: 'audit events remain in the local outbox while the collector is down',
          body: tail(body, 500),
        })
      }
      const text = await response.text()
      const events = []
      for (const line of text.split('\n')) {
        if (!line.trim()) continue
        try {
          events.push(JSON.parse(line))
        } catch {
          /* skip malformed lines */
        }
      }
      return sendJson(res, 200, { query, limit, count: events.length, events })
    } catch (error) {
      return sendJson(res, 503, {
        detail: `VictoriaLogs unreachable: ${error instanceof Error ? error.message : String(error)}`,
        note: 'audit events remain in the local outbox while the collector is down',
      })
    }
  }

  // ── Services ──────────────────────────────────────────────────────────────

  function listServices() {
    return Object.entries(config.services).map(([name, meta]) => {
      const pid = readPid(name)
      return {
        name,
        port: meta.port,
        pidFile: join(config.backendRoot, 'run', `${name}.pid`),
        running: pid !== null,
        pid,
      }
    })
  }

  /** @param {string} name */
  function readPid(name) {
    try {
      const pid = Number.parseInt(readFileSync(join(config.backendRoot, 'run', `${name}.pid`), 'utf8').trim(), 10)
      if (!Number.isInteger(pid) || pid <= 1) return null
      try {
        process.kill(pid, 0)
        return pid
      } catch (error) {
        return error?.code === 'EPERM' ? pid : null
      }
    } catch {
      return null
    }
  }

  async function handleServiceAction(req, res, name, action) {
    if (!Object.hasOwn(config.services, name)) {
      return sendJson(res, 404, { detail: `unknown service ${name}` })
    }
    if (name === 'harness_gateway') {
      return sendJson(res, 409, { detail: 'Manage the admin gateway itself from platform.sh on the host' })
    }
    if (!existsSync(config.platformSh)) {
      return sendJson(res, 501, { detail: `platform.sh not found: ${config.platformSh}` })
    }
    // Drain an optional body so the connection stays reusable.
    if (Number(req.headers['content-length'] ?? 0) > 0) {
      try {
        await readBody(req, MAX_ADMIN_BODY_BYTES)
      } catch {
        return sendJson(res, 413, { detail: 'request body too large' })
      }
    }
    return withLock(`service:${name}`, async () => {
      const result = await runCommand(config.platformSh, ['service', name, action], {
        cwd: config.backendRoot,
        timeoutMs: SERVICE_RESTART_TIMEOUT_MS,
        env: { ...process.env, SYSADMIN_AGENT_PORT: String(config.services.agent_tools.port) },
      })
      const output = tail(result.output, 4000)
      const status = result.exitCode === 0 ? 200 : 500
      log(`admin ${action} service ${name} (exit=${result.exitCode})`)
      return sendJson(res, status, {
        status: result.exitCode === 0 ? { start: 'started', stop: 'stopped', restart: 'restarted' }[action] : 'failed',
        service: name,
        exitCode: result.exitCode,
        timedOut: result.timedOut,
        output,
      })
    })
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  /** @param {import('node:http').IncomingMessage} req @param {import('node:http').ServerResponse} res */
  async function readJsonBody(req, res) {
    let raw
    try {
      raw = await readBody(req, MAX_ADMIN_BODY_BYTES)
    } catch {
      sendJson(res, 413, { detail: 'request body too large' })
      return undefined
    }
    try {
      return raw.trim() === '' ? {} : JSON.parse(raw)
    } catch {
      sendJson(res, 400, { detail: 'Invalid JSON body' })
      return undefined
    }
  }

  /** @returns {string} */
  function masterToken() {
    try {
      return readFileSync(config.masterKeyFile, 'utf8').trim()
    } catch {
      return ''
    }
  }

  /**
   * Run a child process with a hard timeout, capturing combined output.
   *
   * @param {string} command
   * @param {string[]} args
   * @param {{ cwd?: string, timeoutMs: number, env?: NodeJS.ProcessEnv }} options
   * @returns {Promise<{ exitCode: number, output: string, timedOut: boolean }>}
   */
  function runCommand(command, args, { cwd, timeoutMs, env }) {
    return new Promise((resolve) => {
      let output = ''
      let settled = false
      let timedOut = false
      const child = spawn(command, args, { cwd, env, stdio: ['ignore', 'pipe', 'pipe'] })
      const timer = setTimeout(() => {
        timedOut = true
        child.kill('SIGKILL')
      }, timeoutMs)
      const finish = (exitCode) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        resolve({ exitCode, output, timedOut })
      }
      child.stdout?.on('data', (chunk) => { output = tail(output + String(chunk), 16 * 1024) })
      child.stderr?.on('data', (chunk) => { output = tail(output + String(chunk), 16 * 1024) })
      child.on('error', (error) => {
        output = tail(`${output}\n${error instanceof Error ? error.message : String(error)}`, 16 * 1024)
        finish(-1)
      })
      child.on('close', (code) => finish(code ?? -1))
    })
  }

  /**
   * @template T
   * @param {string} key
   * @param {() => Promise<T>} fn
   * @returns {Promise<T>}
   */
  function withLock(key, fn) {
    const previous = locks.get(key) ?? Promise.resolve()
    const run = previous.then(() => fn())
    locks.set(key, run.catch(() => {}))
    return run
  }

  return {
    handle,
    verifyMasterToken,
    shortId,
    listUsers,
    listServices,
    serviceProbes,
  }
}

/** @param {string} text @param {number} bytes */
function tail(text, bytes) {
  return text.length <= bytes ? text : text.slice(text.length - bytes)
}
