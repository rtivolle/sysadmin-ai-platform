/**
 * Multi-user login gateway for the sysadmin harness, with the Mila-branded
 * admin console folded in.
 *
 * Flow:
 *   1. The browser POSTs credentials to `/api/gateway/login` (the login page is
 *      the Mila-branded HTML served from `/api/gateway/login`).
 *   2. The gateway authenticates them against the platform auth gateway
 *      (`POST /api/v1/auth/login`) and stores an opaque gateway session cookie.
 *      Sessions persist to `SYSADMIN_SESSIONS_FILE`, so a gateway restart does
 *      not log anyone out.
 *   3. Every other request is routed to the harness instance owned by that
 *      identity, starting it on first use; instances are recorded in the
 *      registry and re-adopted on the same port after a restart.
 *   4. `/admin` serves the Mila-branded admin console (see admin.js), which
 *      authenticates with the master key and manages users, sessions, harness
 *      instances, approvals, audit and backend services.
 *
 * The gateway never reads or stores the sysadmin password beyond the login
 * exchange, and it never derives identity from a client header.
 */
import { createServer as createHttpServer, request as httpRequest } from 'node:http'
import { connect as netConnect } from 'node:net'
import { pathToFileURL } from 'node:url'
import { createAdminConsole } from './admin.js'
import { loadConfig } from './config.js'
import { acceptsHtml, readBody, sendJson } from './http-util.js'
import { InstanceError, InstanceManager } from './instance-manager.js'
import { parseCookies, SessionStore } from './session-store.js'

const HOP_BY_HOP = new Set([
  'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailers', 'transfer-encoding', 'upgrade',
])

/**
 * Headers forwarded to a user's harness instance.
 *
 * The browser Host is preserved. The harness binds its session cookie and its
 * Origin check to that header; rewriting it to the loopback instance port
 * makes every API call 403, so the UI stays stuck behind login.
 *
 * @param {import('node:http').IncomingHttpHeaders} headers
 * @param {{ host: string, port: number }} instance
 * @returns {import('node:http').OutgoingHttpHeaders}
 */
export function browserProxyHeaders(headers, instance) {
  /** @type {import('node:http').OutgoingHttpHeaders} */
  const next = { ...headers }
  for (const name of Object.keys(next)) {
    if (HOP_BY_HOP.has(name.toLowerCase())) delete next[name]
  }
  if (!next.host) next.host = `${instance.host}:${instance.port}`
  return next
}

const MAX_LOGIN_BODY_BYTES = 16 * 1024
const TOKEN_RECOVERY_COOLDOWN_MS = 60_000

export const LOGIN_PAGE = `<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mila — Plateforme IA Sysadmin — Connexion</title>
<link rel="icon" href="/assets/mila-logo.png">
<link rel="stylesheet" href="/assets/brand.css">
<style>input{margin-bottom:.55rem}button{margin-top:.4rem;width:100%}</style></head>
<body class="mila">
<div class="mila-login">
  <div class="mila-card" style="text-align:center">
    <a href="https://mila.quebec/" target="_blank" rel="noopener"><img src="/assets/mila-logo.png" alt="Mila" style="height:44px;margin-bottom:.75rem"></a>
    <h1 style="font-size:1.15rem;margin:0 0 .2rem">Plateforme IA Sysadmin</h1>
    <p class="subtitle">Connectez-vous avec vos identifiants administrateur système.</p>
    <form method="post" action="/api/gateway/login" enctype="application/x-www-form-urlencoded" style="text-align:left">
      <label class="mila-muted" for="username">Utilisateur</label>
      <input class="mila-input" id="username" name="username" placeholder="sysadmin-01" autocomplete="username" required>
      <label class="mila-muted" for="password">Mot de passe</label>
      <input class="mila-input" id="password" name="password" type="password" placeholder="Mot de passe" autocomplete="current-password" required>
      <button class="mila-button" type="submit">Se connecter</button>
    </form>
  </div>
</div>
<footer class="mila-footer"><a href="https://mila.quebec/" target="_blank" rel="noopener">Mila — Institut québécois d'intelligence artificielle</a></footer>
</body></html>`

/**
 * @param {object} [options]
 * @param {ReturnType<typeof loadConfig>} [options.config]
 * @param {(message: string, meta?: unknown) => void} [options.logger]
 * @param {SessionStore} [options.sessions] inject a store (tests)
 * @param {SessionStore} [options.adminSessions] inject the admin store (tests)
 * @param {InstanceManager} [options.instances] inject a manager (tests)
 */
export function createGateway({
  config = loadConfig(),
  logger = () => {},
  sessions: injectedSessions,
  adminSessions: injectedAdminSessions,
  instances: injectedInstances,
} = {}) {
  const log = logger
  const sessions = injectedSessions ?? new SessionStore({
    ttlMs: config.sessionTtlMs,
    path: config.sessionsFile,
    logger: (message) => log(`sessions: ${message}`),
  })
  const adminSessions = injectedAdminSessions ?? new SessionStore({
    ttlMs: config.adminTtlMs,
    path: config.adminSessionsFile,
    logger: (message) => log(`admin sessions: ${message}`),
  })
  const instances = injectedInstances ?? new InstanceManager({ config, logger })

  const loaded = sessions.load()
  if (loaded > 0) log(`loaded ${loaded} persisted browser session(s)`)
  const loadedAdmin = adminSessions.load()
  if (loadedAdmin > 0) log(`loaded ${loadedAdmin} persisted admin session(s)`)

  const admin = createAdminConsole({
    config,
    sessions,
    adminSessions,
    instances,
    logger: (message) => log(message),
  })

  const startedAt = Date.now()

  const server = createHttpServer((req, res) => {
    handleRequest(req, res).catch((error) => {
      log(`gateway request failed: ${error instanceof Error ? error.message : String(error)}`)
      if (!res.headersSent) sendJson(res, 500, { detail: 'gateway error' })
      else res.end()
    })
  })

  server.on('upgrade', (req, socket, head) => {
    handleUpgrade(req, socket, head).catch(() => socket.destroy())
  })

  /**
   * @param {import('node:http').IncomingMessage} req
   * @param {import('node:http').ServerResponse} res
   */
  async function handleRequest(req, res) {
    const url = new URL(req.url ?? '/', `http://${req.headers.host ?? '127.0.0.1'}`)

    if (url.pathname === '/api/gateway/health') {
      return sendJson(res, 200, {
        status: 'healthy',
        service: 'sysadmin_harness_gateway',
        active_users: instances.activeUsers(),
        sessions: sessions.list().length,
        instances: instances.listStatus().length,
        uptime_ms: Date.now() - startedAt,
      })
    }

    if (url.pathname === '/api/gateway/login') {
      if (req.method === 'GET') {
        res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' })
        return res.end(LOGIN_PAGE)
      }
      return handleLogin(req, res)
    }

    if (url.pathname === '/api/gateway/logout' && req.method === 'POST') {
      const cookies = parseCookies(req.headers.cookie)
      sessions.delete(cookies[config.cookieName])
      res.setHeader('set-cookie', `${config.cookieName}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0`)
      return sendJson(res, 200, { status: 'logged_out' })
    }

    // Admin console and its assets are gateway-owned surfaces.
    if (await admin.handle(req, res, url)) return

    const session = currentSession(req)
    if (!session) {
      if (req.method === 'GET' && acceptsHtml(req)) {
        res.writeHead(302, { location: '/api/gateway/login' })
        return res.end()
      }
      return sendJson(res, 401, { detail: 'Authentication required' })
    }

    const instance = await instances.ensure(session.userId)
    instances.touch?.(session.userId)

    // A freshly started instance prints an authenticated launch URL. Hand the
    // browser through it once so the harness can establish its own cookie,
    // then proxy normally.
    const cookies = parseCookies(req.headers.cookie)
    if (!url.searchParams.has('token') && !cookies.sysadmin_launch_done) {
      const token = await waitForLaunchToken(instance, 5000)
      if (token) {
        res.writeHead(302, {
          location: `/?token=${encodeURIComponent(token)}`,
          'set-cookie': 'sysadmin_launch_done=1; Path=/; HttpOnly; SameSite=Lax',
        })
        return res.end()
      }
    }

    return proxyHttp(req, res, instance, url)
  }

  /**
   * @param {import('node:http').IncomingMessage} req
   * @param {import('node:http').ServerResponse} res
   */
  async function handleLogin(req, res) {
    let raw
    try {
      raw = await readBody(req, MAX_LOGIN_BODY_BYTES)
    } catch (error) {
      return sendJson(res, 413, { detail: error instanceof Error ? error.message : 'body too large' })
    }

    let credentials
    try {
      credentials = parseCredentials(raw, req.headers['content-type'] ?? '')
    } catch {
      return sendJson(res, 400, { detail: 'Invalid login body' })
    }
    if (!credentials.username || !credentials.password) {
      return sendJson(res, 400, { detail: 'username and password are required' })
    }

    let authResponse
    try {
      authResponse = await fetch(`${config.authUrl}/api/v1/auth/login`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(credentials),
        signal: AbortSignal.timeout(10000),
      })
    } catch (error) {
      return sendJson(res, 503, { detail: `auth gateway unreachable: ${error instanceof Error ? error.message : String(error)}` })
    }

    const body = await safeJson(authResponse)
    if (!authResponse.ok) {
      return sendJson(res, authResponse.status, { detail: body?.detail ?? 'Authentication failed' })
    }

    const userId = typeof body?.user === 'string' ? body.user : ''
    if (!userId) return sendJson(res, 502, { detail: 'auth gateway returned no identity' })

    const sessionId = sessions.create(userId)
    res.setHeader('set-cookie', `${config.cookieName}=${sessionId}; Path=/; HttpOnly; SameSite=Lax`)

    // Start the user's instance eagerly so the first page load is not a cold start.
    try {
      await instances.ensure(userId)
    } catch (error) {
      log(`failed to start harness for ${userId}: ${error instanceof Error ? error.message : String(error)}`)
      return sendJson(res, 503, { detail: `harness unavailable: ${error instanceof Error ? error.message : String(error)}` })
    }

    // The branded HTML form posts url-encoded and expects to land in the app;
    // JSON callers get the JSON contract (used by tests and scripts).
    if (!String(req.headers['content-type'] ?? '').includes('application/json')) {
      res.writeHead(302, { location: '/' })
      return res.end()
    }

    return sendJson(res, 200, {
      status: 'authenticated',
      user: userId,
      role: typeof body.role === 'string' ? body.role : 'sysadmin',
      redirect: '/',
    })
  }

  /**
   * @param {import('node:http').IncomingMessage} req
   * @returns {{ userId: string }|null}
   */
  function currentSession(req) {
    const cookies = parseCookies(req.headers.cookie)
    return sessions.get(cookies[config.cookieName])
  }

  /**
   * @param {import('node:http').IncomingMessage} req
   * @param {import('node:http').ServerResponse} res
   * @param {import('./instance-manager.js').HarnessInstance} instance
   * @param {URL} url
   */
  function proxyHttp(req, res, instance, url) {
    const headers = browserProxyHeaders(req.headers, { host: config.instanceHost, port: instance.port })

    const upstream = httpRequest(
      {
        host: config.instanceHost,
        port: instance.port,
        method: req.method,
        path: req.url,
        headers,
      },
      (upstreamResponse) => {
        // An adopted instance has no launch token in memory. If its browser
        // cookie is gone too, the harness answers 401 on `/`; mint a fresh
        // launch token by restarting the instance once, then reload through it.
        if (
          upstreamResponse.statusCode === 401
          && req.method === 'GET'
          && url.pathname === '/'
          && acceptsHtml(req)
          && recoverLaunchToken(instance)
        ) {
          res.writeHead(302, {
            location: '/',
            'set-cookie': 'sysadmin_launch_done=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0',
          })
          upstreamResponse.resume()
          return res.end()
        }
        const responseHeaders = { ...upstreamResponse.headers }
        for (const name of Object.keys(responseHeaders)) {
          if (HOP_BY_HOP.has(name.toLowerCase())) delete responseHeaders[name]
        }
        res.writeHead(upstreamResponse.statusCode ?? 502, responseHeaders)
        upstreamResponse.pipe(res)
      },
    )

    upstream.on('error', (error) => {
      log(`proxy error for ${instance.userId}: ${error.message}`)
      if (!res.headersSent) sendJson(res, 502, { detail: 'harness instance unreachable' })
      else res.destroy()
    })

    req.pipe(upstream)
  }

  /**
   * Restart an instance whose browser session expired so a new single-use
   * launch token is printed. Rate-limited per instance to avoid restart loops.
   *
   * @param {import('./instance-manager.js').HarnessInstance} instance
   * @returns {boolean} true when a recovery restart was started
   */
  function recoverLaunchToken(instance) {
    const now = Date.now()
    if (instance.tokenRecoveryAt && now - instance.tokenRecoveryAt < TOKEN_RECOVERY_COOLDOWN_MS) return false
    instance.tokenRecoveryAt = now
    log(`harness for ${instance.userId} refused a browser navigation; restarting it to mint a fresh launch token`)
    instances.restart(instance.userId).then(
      (fresh) => log(`launch-token recovery for ${instance.userId} finished on port ${fresh.port}`),
      (error) => log(`launch-token recovery for ${instance.userId} failed: ${error instanceof Error ? error.message : String(error)}`),
    )
    return true
  }

  /**
   * @param {import('node:http').IncomingMessage} req
   * @param {import('node:stream').Duplex} socket
   * @param {Buffer} head
   */
  async function handleUpgrade(req, socket, head) {
    const session = currentSession(req)
    if (!session) return socket.destroy()

    const instance = await instances.ensure(session.userId)
    instances.touch?.(session.userId)
    const upstream = netConnect(instance.port, config.instanceHost, () => {
      const headers = browserProxyHeaders(req.headers, { host: config.instanceHost, port: instance.port })
      const lines = [`${req.method} ${req.url} HTTP/1.1`]
      for (const [name, value] of Object.entries(headers)) {
        if (value === undefined) continue
        lines.push(`${name}: ${Array.isArray(value) ? value.join(', ') : value}`)
      }
      lines.push('Connection: Upgrade', 'Upgrade: websocket', '', '')
      upstream.write(lines.join('\r\n'))
      if (head?.length) upstream.write(head)
      upstream.pipe(socket)
      socket.pipe(upstream)
    })

    upstream.on('error', () => socket.destroy())
    socket.on('error', () => upstream.destroy())
    socket.on('close', () => upstream.destroy())
  }

  return {
    server,
    sessions,
    adminSessions,
    instances,
    admin,
    config,
    /**
     * @param {number} [port]
     * @returns {Promise<{ port: number, url: string }>}
     */
    async start(port = config.port) {
      const recovery = await instances.recover?.()
      if (recovery?.adopted?.length > 0) log(`re-adopted ${recovery.adopted.length} running harness instance(s): ${recovery.adopted.join(', ')}`)
      if (recovery?.dropped?.length > 0) log(`dropped ${recovery.dropped.length} stale instance record(s): ${recovery.dropped.join(', ')}`)
      if (recovery?.unknownOwner?.length > 0) log(`left ${recovery.unknownOwner.length} unknown-owner port(s) alone: ${recovery.unknownOwner.join(', ')}`)
      instances.reEnsure?.(sessions)
      instances.startIdleSweeper?.((userId) => sessions.hasUser(userId))
      return new Promise((resolve, reject) => {
        server.once('error', reject)
        server.listen(port, config.host, () => {
          const address = server.address()
          const boundPort = typeof address === 'object' && address ? address.port : port
          resolve({ port: boundPort, url: `http://${config.host}:${boundPort}` })
        })
      })
    },
    /**
     * Stop the HTTP surface and flush persisted state.
     *
     * @param {object} [options]
     * @param {boolean} [options.stopInstances] false = leave harness children
     *   running so a restarted gateway re-adopts them (browser sessions keep
     *   working); platform.sh reaps them on a full platform stop.
     * @param {boolean} [options.keepRegistry]
     */
    async stop({ stopInstances = true, keepRegistry = false } = {}) {
      sessions.flush()
      adminSessions.flush()
      await instances.stopAll?.({ stopInstances, keepRegistry: keepRegistry || !stopInstances })
      // Undici keeps connections alive; without this, close() waits for them.
      server.closeAllConnections?.()
      await new Promise((resolve) => server.close(() => resolve()))
    },
  }
}

/**
 * Accept either JSON or form-encoded login bodies.
 *
 * @param {string} raw
 * @param {string} contentType
 * @returns {{ username: string, password: string }}
 */
export function parseCredentials(raw, contentType) {
  if (contentType.includes('application/json')) {
    const parsed = JSON.parse(raw)
    return {
      username: String(parsed?.username ?? '').trim(),
      password: String(parsed?.password ?? '').trim(),
    }
  }
  const params = new URLSearchParams(raw)
  return {
    username: (params.get('username') ?? '').trim(),
    password: (params.get('password') ?? '').trim(),
  }
}

async function safeJson(response) {
  try {
    return await response.json()
  } catch {
    return null
  }
}

/**
 * Wait briefly for the instance's launch token to appear in its output.
 *
 * @param {import('./instance-manager.js').HarnessInstance} instance
 * @param {number} timeoutMs
 * @returns {Promise<string|null>}
 */
export async function waitForLaunchToken(instance, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (instance.launchToken) return instance.launchToken
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  return instance.launchToken
}

export { InstanceError }
export { readBody, sendJson }

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const gateway = createGateway({
    logger: (message) => console.log(`[gateway] ${message}`),
  })
  const { url } = await gateway.start()
  console.log(`[gateway] sysadmin harness gateway listening on ${url}`)
  console.log(`[gateway] Mila admin console: ${url}/admin`)
  const stopOnExit = process.env.SYSADMIN_INSTANCE_STOP_ON_EXIT === '1'
  let shuttingDown = false
  const shutdown = async () => {
    if (shuttingDown) return
    shuttingDown = true
    console.log(`[gateway] shutting down (instances: ${stopOnExit ? 'stopping' : 'left running for re-adoption'})`)
    await gateway.stop({ stopInstances: stopOnExit })
    process.exit(0)
  }
  process.on('SIGINT', shutdown)
  process.on('SIGTERM', shutdown)
}
