/**
 * Multi-user login gateway for the sysadmin harness.
 *
 * Flow:
 *   1. The browser POSTs credentials to `/api/gateway/login`.
 *   2. The gateway authenticates them against the platform auth gateway
 *      (`POST /api/v1/auth/login`) and stores an opaque gateway session cookie.
 *   3. Every other request is routed to the harness instance owned by that
 *      identity, starting it on first use.
 *
 * The gateway never reads or stores the sysadmin password beyond the login
 * exchange, and it never derives identity from a client header.
 */
import { createServer as createHttpServer, request as httpRequest } from 'node:http'
import { connect as netConnect } from 'node:net'
import { pathToFileURL } from 'node:url'
import { loadConfig } from './config.js'
import { InstanceError, InstanceManager } from './instance-manager.js'
import { parseCookies, SessionStore } from './session-store.js'

const HOP_BY_HOP = new Set([
  'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailers', 'transfer-encoding', 'upgrade',
])

const MAX_LOGIN_BODY_BYTES = 16 * 1024
const LOGIN_PAGE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Sysadmin AI Platform — Sign in</title>
<style>body{font-family:system-ui,sans-serif;max-width:26rem;margin:12vh auto;padding:0 1rem}
input,button{width:100%;padding:.6rem;margin:.3rem 0;box-sizing:border-box}
p.err{color:#b00}</style></head>
<body><h1>Sysadmin AI Platform</h1>
<form method="post" action="/api/gateway/login" enctype="application/x-www-form-urlencoded">
<input name="username" placeholder="sysadmin-01" autocomplete="username" required>
<input name="password" type="password" placeholder="Password" autocomplete="current-password" required>
<button type="submit">Sign in</button></form></body></html>`

/**
 * @param {object} [options]
 * @param {ReturnType<typeof loadConfig>} [options.config]
 * @param {(message: string, meta?: unknown) => void} [options.logger]
 * @param {SessionStore} [options.sessions] inject a store (tests)
 * @param {InstanceManager} [options.instances] inject a manager (tests)
 */
export function createGateway({
  config = loadConfig(),
  logger = () => {},
  sessions: injectedSessions,
  instances: injectedInstances,
} = {}) {
  const sessions = injectedSessions ?? new SessionStore({ ttlMs: config.sessionTtlMs })
  const instances = injectedInstances ?? new InstanceManager({ config, logger })

  const log = logger

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
        sessions: sessions.sessions.size,
      })
    }

    if (url.pathname === '/api/gateway/login') {
      if (req.method === 'GET') {
        res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
        return res.end(LOGIN_PAGE)
      }
      return handleLogin(req, res)
    }

    if (url.pathname === '/api/gateway/logout') {
      const cookies = parseCookies(req.headers.cookie)
      sessions.delete(cookies[config.cookieName])
      res.setHeader('set-cookie', `${config.cookieName}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0`)
      return sendJson(res, 200, { status: 'logged_out' })
    }

    const session = currentSession(req)
    if (!session) {
      if (req.method === 'GET' && acceptsHtml(req)) {
        res.writeHead(302, { location: '/api/gateway/login' })
        return res.end()
      }
      return sendJson(res, 401, { detail: 'Authentication required' })
    }

    const instance = await instances.ensure(session.userId)

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

    return proxyHttp(req, res, instance)
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
   */
  function proxyHttp(req, res, instance) {
    const headers = { ...req.headers }
    for (const name of Object.keys(headers)) {
      if (HOP_BY_HOP.has(name.toLowerCase())) delete headers[name]
    }
    headers.host = `${config.instanceHost}:${instance.port}`

    const upstream = httpRequest(
      {
        host: config.instanceHost,
        port: instance.port,
        method: req.method,
        path: req.url,
        headers,
      },
      (upstreamResponse) => {
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
   * @param {import('node:http').IncomingMessage} req
   * @param {import('node:stream').Duplex} socket
   * @param {Buffer} head
   */
  async function handleUpgrade(req, socket, head) {
    const session = currentSession(req)
    if (!session) return socket.destroy()

    const instance = await instances.ensure(session.userId)
    const upstream = netConnect(instance.port, config.instanceHost, () => {
      const headers = { ...req.headers, host: `${config.instanceHost}:${instance.port}` }
      delete headers.upgrade
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
    instances,
    config,
    /**
     * @param {number} [port]
     * @returns {Promise<{ port: number, url: string }>}
     */
    start(port = config.port) {
      return new Promise((resolve, reject) => {
        server.once('error', reject)
        server.listen(port, config.host, () => {
          const address = server.address()
          const boundPort = typeof address === 'object' && address ? address.port : port
          resolve({ port: boundPort, url: `http://${config.host}:${boundPort}` })
        })
      })
    },
    async stop() {
      instances.stopAll?.()
      // Undici keeps connections alive; without this, close() waits for them.
      server.closeAllConnections?.()
      await new Promise((resolve) => server.close(() => resolve()))
    },
  }
}

/**
 * @param {import('node:http').IncomingMessage} req
 * @param {number} limit
 * @returns {Promise<string>}
 */
export function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > limit) {
        reject(new Error('request body too large'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
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

/** @param {import('node:http').ServerResponse} res */
export function sendJson(res, status, payload) {
  const body = JSON.stringify(payload)
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) })
  res.end(body)
}

async function safeJson(response) {
  try {
    return await response.json()
  } catch {
    return null
  }
}

function acceptsHtml(req) {
  return String(req.headers.accept ?? '').includes('text/html')
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

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const gateway = createGateway({
    logger: (message) => console.log(`[gateway] ${message}`),
  })
  const { url } = await gateway.start()
  console.log(`[gateway] sysadmin harness gateway listening on ${url}`)
  const shutdown = async () => {
    await gateway.stop()
    process.exit(0)
  }
  process.on('SIGINT', shutdown)
  process.on('SIGTERM', shutdown)
}
