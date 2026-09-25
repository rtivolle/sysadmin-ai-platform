/**
 * Admin console tests: Mila branding, master-token login + CSRF hardening,
 * and the operator surface (sessions, instances, users, approvals, audit,
 * services) against a real gateway on port 0 with stub backends and an
 * injected instance manager.
 */
import assert from 'node:assert/strict'
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { after, before, test } from 'node:test'
import { loadConfig } from '../gateway/config.js'
import { createGateway } from '../gateway/server.js'

const MASTER = 'test-master-token'
const scratch = mkdtempSync(join(tmpdir(), 'dsh-admin-'))
const keysDir = join(scratch, 'keys')
const commandLog = join(scratch, 'commands.log')
const stubState = { lastAuthorization: null, quotaBody: null, quotaStatus: 200, modelsStatus: 200, modelBody: null }

let gateway, gatewayUrl = '', adminCookie = '', agent, victoria, auth

/** @returns {Promise<{server: import('node:http').Server, port: number, url: string}>} */
function listen(handler) {
  return new Promise((resolve) => {
    const server = createServer(handler)
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      resolve({ server, port, url: `http://127.0.0.1:${port}` })
    })
  })
}
function close(server) {
  server.closeAllConnections?.()
  return new Promise((resolve) => server.close(() => resolve()))
}
function sendJson(res, status, payload) {
  const body = JSON.stringify(payload)
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) })
  res.end(body)
}
/** Executable stub that records its argv and exits 0. */
function writeStub(file, label) {
  writeFileSync(file, `#!/usr/bin/env bash\nprintf '%s\\n' "${label} $*" "agent-port=\${SYSADMIN_AGENT_PORT:-unset}" >> "${commandLog}"\necho "${label} $*"\n`)
  chmodSync(file, 0o755)
}
function adminFetch(path, { method = 'GET', body, cookie = adminCookie, headers = {} } = {}) {
  const requestHeaders = { 'content-type': 'application/json', 'x-sysadmin-admin': '1', ...headers }
  if (cookie) requestHeaders.cookie = cookie
  return fetch(`${gatewayUrl}${path}`, {
    method, headers: requestHeaders, redirect: 'manual',
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

const instanceCalls = []
const statusFor = (userId) => ({ userId, port: 3180, pid: 123, state: 'running', running: true, restarts: 0 })
const instances = {
  async ensure(userId) { instanceCalls.push(['ensure', userId]); return { userId, port: 3180, url: 'http://127.0.0.1:3180' } },
  async stopUser(userId) { instanceCalls.push(['stopUser', userId]); return true },
  async restart(userId) { instanceCalls.push(['restart', userId]); return statusFor(userId) },
  statusFor(userId) { return userId === 'sysadmin-01' ? statusFor(userId) : null },
  listStatus() { return [statusFor('sysadmin-01')] },
  readLog(userId, tailBytes) { instanceCalls.push(['readLog', userId, tailBytes]); return 'log line\n' },
  activeUsers() { return ['sysadmin-01'] },
  async stopAll() { instanceCalls.push(['stopAll']) },
}

before(async () => {
  mkdirSync(keysDir, { recursive: true, mode: 0o700 })
  mkdirSync(join(scratch, 'run'), { recursive: true })
  writeFileSync(join(keysDir, 'master.key'), `${MASTER}\n`, { mode: 0o600 })
  writeFileSync(join(keysDir, 'login-credentials.json'), JSON.stringify({ 'sysadmin-01': 'aa$bb', 'sysadmin-02': 'cc$dd' }))
  writeFileSync(join(keysDir, 'sysadmin-01.key'), 'user-token\n', { mode: 0o600 })
  writeFileSync(join(keysDir, 'provision-logins.py'), '# stub target for the stub python\n')
  writeStub(join(scratch, 'python'), 'python')
  writeStub(join(scratch, 'platform.sh'), 'platform')

  agent = await listen((req, res) => {
    let raw = ''
    req.on('data', (chunk) => { raw += chunk })
    req.on('end', () => {
      const bearer = req.headers.authorization ?? null
      if (req.url?.startsWith('/api/approvals/')) {
        stubState.lastAuthorization = bearer
        if (bearer !== `Bearer ${MASTER}`) return sendJson(res, 401, { detail: 'unauthorized' })
        if (req.method === 'GET' && req.url === '/api/approvals/pending') {
          return sendJson(res, 200, { pending_approvals: [{
            approval_id: 'APR-1', user_id: 'sysadmin-01', session_id: 's1', command: 'rm -rf /tmp/x',
            reason: 'destructive', expires_at: Math.floor(Date.now() / 1000) + 300,
          }] })
        }
        if (req.method === 'POST' && req.url === '/api/approvals/decide') {
          return sendJson(res, 200, { success: true, approval: { approval_id: 'APR-1', approved: true } })
        }
      }
      if (req.url?.startsWith('/api/v1/models')) {
        stubState.lastAuthorization = bearer
        if (bearer !== `Bearer ${MASTER}`) return sendJson(res, 401, { detail: 'unauthorized' })
        if (req.method === 'GET' && req.url === '/api/v1/models') {
          return sendJson(res, 200, { models: [{ name: 'local-1', hf_repo: 'org/local-1', status: 'running', server: { port: 8100, pid: 1 } }] })
        }
        if (req.method === 'POST' && req.url === '/api/v1/models') {
          stubState.modelBody = JSON.parse(raw || '{}')
          return sendJson(res, 201, { model: { name: 'local-1' } })
        }
        if (req.method === 'PATCH' && req.url === '/api/v1/models/local-1') {
          stubState.modelBody = JSON.parse(raw || '{}')
          return sendJson(res, 200, { model: { name: 'local-1', dtype: 'bfloat16' } })
        }
        if (req.method === 'POST' && req.url.endsWith('/start')) return sendJson(res, 202, { status: 'starting' })
        if (req.method === 'DELETE') return sendJson(res, 200, { status: 'deleted' })
        return sendJson(res, 200, { status: 'ok' })
      }
      if (req.url?.startsWith('/api/v1/survey')) {
        if (bearer !== `Bearer ${MASTER}`) return sendJson(res, 401, { detail: 'unauthorized' })
        return sendJson(res, 200, { hostname: 'stub-host', gpus: [], pci_accelerators: [], driver_stack: {}, software_versions: { python: '3' }, model_storage: { entries: [] } })
      }
      if (req.method === 'GET' && req.url === '/health') return sendJson(res, 200, { status: 'healthy' })
      if (req.method === 'GET' && req.url === '/v1/models') {
        assert.equal(bearer, `Bearer ${MASTER}`)
        return sendJson(res, stubState.modelsStatus, { data: [{ id: 'fast-model', owned_by: 'local', api_key: 'must-not-leak' }] })
      }
      return sendJson(res, 404, { detail: 'not found' })
    })
  })
  victoria = await listen((req, res) => {
    if (req.method === 'GET' && req.url?.startsWith('/select/logsql/query')) {
      res.writeHead(200, { 'content-type': 'application/json' })
      return res.end('{"_time":"t1","user_id":"sysadmin-01","action":"tool"}\n{"_time":"t2"}\n')
    }
    return sendJson(res, 404, { detail: 'not found' })
  })
  auth = await listen((req, res) => {
    if (req.url?.startsWith('/api/v1/admin/quotas')) {
      assert.equal(req.headers.authorization, `Bearer ${MASTER}`)
      let raw = ''
      req.on('data', (chunk) => { raw += chunk })
      req.on('end', () => {
        if (req.method === 'POST') stubState.quotaBody = JSON.parse(raw)
        sendJson(res, stubState.quotaStatus, stubState.quotaStatus === 200 ? { users: [], status: 'updated' } : { detail: 'store unavailable' })
      })
      return
    }
    return req.method === 'POST' && req.url === '/api/v1/auth/login'
      ? sendJson(res, 200, { user: 'sysadmin-01', role: 'sysadmin' })
      : sendJson(res, 404, { detail: 'not found' })
  })

  const config = {
    ...loadConfig({}),
    services: { ...loadConfig({}).services, agent_tools: { port: 3090 } },
    stateRoot: scratch,
    sessionsFile: join(scratch, 'sessions.jsonl'),
    adminSessionsFile: join(scratch, 'admin-sessions.jsonl'),
    keysDir,
    masterKeyFile: join(keysDir, 'master.key'),
    credentialsFile: join(keysDir, 'login-credentials.json'),
    initialPasswordsFile: join(keysDir, 'initial-passwords.txt'),
    provisionLoginsScript: join(keysDir, 'provision-logins.py'),
    pythonBin: join(scratch, 'python'),
    platformSh: join(scratch, 'platform.sh'),
    backendRoot: scratch,
    agentUrl: agent.url,
    litellmUrl: `${agent.url}/v1`,
    authUrl: auth.url,
    victoriaLogsUrl: victoria.url,
    instanceRegistryDir: join(scratch, 'instances'),
    instanceLogDir: join(scratch, 'logs'),
  }
  gateway = createGateway({ config, logger: () => {}, instances })
  ;({ url: gatewayUrl } = await gateway.start(0))
})

after(async () => {
  await gateway?.stop()
  await Promise.all([agent, victoria, auth].map((stub) => (stub ? close(stub.server) : undefined)))
  rmSync(scratch, { recursive: true, force: true })
})

test('serves the Mila-branded admin page, login page and assets', async () => {
  const page = await fetch(`${gatewayUrl}/admin`)
  assert.equal(page.status, 200)
  const html = await page.text()
  assert.match(html, /mila\.quebec/)
  assert.match(html, /\/assets\/mila-logo\.png/)
  assert.match(html, /[Cc]onsole/)

  const login = await fetch(`${gatewayUrl}/api/gateway/login`)
  assert.equal(login.status, 200)
  const loginHtml = await login.text()
  assert.match(loginHtml, /mila\.quebec/)
  assert.match(loginHtml, /\/assets\/mila-logo\.png/)

  const css = await fetch(`${gatewayUrl}/assets/brand.css`)
  assert.equal(css.status, 200)
  assert.match(css.headers.get('content-type'), /text\/css/)
  const logo = await fetch(`${gatewayUrl}/assets/mila-logo.png`)
  assert.equal(logo.status, 200)
  assert.match(logo.headers.get('content-type'), /image\/png/)
})

test('rejects bad logins and hardened mutations, then issues the admin cookie', async () => {
  const anonymous = await fetch(`${gatewayUrl}/api/admin/session`)
  assert.equal(anonymous.status, 200)
  assert.deepEqual(await anonymous.json(), {
    authenticated: false, user: null, gateway: { port: gateway.config.port, backendUrl: gateway.config.agentUrl },
  })

  assert.equal((await adminFetch('/api/admin/login', { method: 'POST', body: { token: 'wrong' } })).status, 401)

  const noHeader = await fetch(`${gatewayUrl}/api/admin/login`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ token: MASTER }),
  })
  assert.equal(noHeader.status, 403)
  assert.equal((await adminFetch('/api/admin/login', {
    method: 'POST', body: { token: MASTER }, headers: { origin: 'http://evil.example' },
  })).status, 403)
  assert.equal((await adminFetch('/api/admin/login', {
    method: 'POST', body: { token: MASTER }, headers: { 'content-type': 'text/plain' },
  })).status, 415)

  const login = await adminFetch('/api/admin/login', { method: 'POST', body: { token: MASTER } })
  assert.equal(login.status, 200)
  assert.match(login.headers.get('set-cookie'), /sysadmin_admin=/)
  adminCookie = login.headers.get('set-cookie').split(';')[0]
})

test('reports an authenticated admin session and an overview with the gateway', async () => {
  const session = await adminFetch('/api/admin/session')
  assert.equal(session.status, 200)
  const sessionBody = await session.json()
  assert.equal(sessionBody.authenticated, true)
  assert.equal(sessionBody.user, 'sysadmin-admin')

  gateway.sessions.create('sysadmin-01')
  const overview = await adminFetch('/api/admin/overview')
  assert.equal(overview.status, 200)
  const body = await overview.json()
  assert.ok(Array.isArray(body.services))
  assert.ok(body.services.some((service) => service.name === 'harness_gateway'))
  assert.ok(body.gateway.sessions >= 1)
})

test('serves admin surfaces to a browser holding a user session without proxying them', async () => {
  // A browser may hold both the gateway user cookie and the admin cookie. A
  // fall-through here used to send the admin request on to the user's harness
  // instance and crash the gateway with ERR_HTTP_HEADERS_SENT once the
  // instance answered (server.js proxyHttp).
  const userCookie = `${gateway.config.cookieName}=${gateway.sessions.create('sysadmin-01')}`
  instanceCalls.length = 0

  const page = await fetch(`${gatewayUrl}/admin`, {
    headers: { cookie: userCookie, accept: 'text/html' },
    redirect: 'manual',
  })
  assert.equal(page.status, 200)
  assert.match(await page.text(), /<title>Mila/)

  const probe = await fetch(`${gatewayUrl}/api/admin/session`, { headers: { cookie: userCookie } })
  assert.equal(probe.status, 200)
  assert.deepEqual(await probe.json(), {
    authenticated: false, user: null, gateway: { port: gateway.config.port, backendUrl: gateway.config.agentUrl },
  })

  assert.equal(instanceCalls.filter(([name]) => name === 'ensure').length, 0, 'admin requests must not reach instances.ensure()')
})

test('lists opaque session handles, revokes one and stops its instance', async () => {
  const rawId = gateway.sessions.create('sysadmin-01')
  const handle = gateway.admin.shortId(rawId)
  const listed = await adminFetch('/api/admin/sessions')
  assert.equal(listed.status, 200)
  const entry = (await listed.json()).sessions.find((session) => session.id === handle)
  assert.ok(entry, 'the created session must be listed under its opaque handle')
  assert.match(entry.id, /^[0-9a-f]{16}$/)
  assert.notEqual(entry.id, rawId)
  assert.equal(entry.userId, 'sysadmin-01')

  const revoked = await adminFetch(`/api/admin/sessions/${handle}/revoke`, { method: 'POST', body: { stopInstance: true } })
  assert.equal(revoked.status, 200)
  assert.equal(gateway.sessions.get(rawId), null)
  assert.ok(instanceCalls.some(([name, userId]) => name === 'stopUser' && userId === 'sysadmin-01'))
  assert.equal((await adminFetch('/api/admin/sessions/deadbeefdeadbeef/revoke', { method: 'POST', body: {} })).status, 404)
})

test('lists instances, restarts/stops one, tails logs and rejects unsafe ids', async () => {
  const listed = await adminFetch('/api/admin/instances')
  assert.equal(listed.status, 200)
  assert.deepEqual((await listed.json()).instances, [statusFor('sysadmin-01')])

  assert.equal((await adminFetch('/api/admin/instances/sysadmin-01/restart', { method: 'POST' })).status, 200)
  assert.ok(instanceCalls.some(([name, userId]) => name === 'restart' && userId === 'sysadmin-01'))
  assert.equal((await adminFetch('/api/admin/instances/sysadmin-01/stop', { method: 'POST' })).status, 200)
  assert.ok(instanceCalls.some(([name, userId]) => name === 'stopUser' && userId === 'sysadmin-01'))

  const logs = await adminFetch('/api/admin/instances/sysadmin-01/logs?tail=4096')
  assert.equal(logs.status, 200)
  assert.equal(await logs.text(), 'log line\n')

  // A route-matching but unsafe id reaches assertSafeUserId -> 400. Encoded
  // traversal never matches the route pattern and is rejected earlier (404).
  assert.equal((await adminFetch('/api/admin/instances/-bad/logs')).status, 400)
  assert.equal((await adminFetch('/api/admin/instances/-bad/restart', { method: 'POST' })).status, 400)
  assert.equal((await adminFetch('/api/admin/instances/..%2Fetc/logs')).status, 404)
})

test('lists users without credential material and rotates a password', async () => {
  const listed = await adminFetch('/api/admin/users')
  assert.equal(listed.status, 200)
  const text = await listed.text()
  const users = JSON.parse(text).users
  assert.ok(users.some((user) => user.userId === 'sysadmin-01' && user.hasKey === true))
  assert.ok(users.some((user) => user.userId === 'sysadmin-02' && user.hasKey === false))
  assert.ok(!text.includes('aa$bb') && !text.includes('cc$dd'), 'password hashes must never leave the server')

  const rotated = await adminFetch('/api/admin/users/sysadmin-01/rotate-password', { method: 'POST', body: {} })
  assert.equal(rotated.status, 200)
  assert.match((await rotated.json()).passwordFile, /initial-passwords/)
  assert.match(readFileSync(commandLog, 'utf8'), /--user sysadmin-01 --rotate/)
  assert.equal((await adminFetch('/api/admin/users/sysadmin-99/rotate-password', { method: 'POST', body: {} })).status, 404)
})

test('proxies approvals with the master bearer and validates decisions', async () => {
  const pending = await adminFetch('/api/admin/approvals')
  assert.equal(pending.status, 200)
  assert.equal((await pending.json()).pending_approvals[0].approval_id, 'APR-1')
  assert.equal(stubState.lastAuthorization, `Bearer ${MASTER}`)

  const decided = await adminFetch('/api/admin/approvals/decide', {
    method: 'POST', body: { approval_id: 'APR-1', approved: true },
  })
  assert.equal(decided.status, 200)
  assert.equal((await decided.json()).success, true)
  const malformed = await adminFetch('/api/admin/approvals/decide', { method: 'POST', body: { approval_id: 'APR-1' } })
  assert.equal(malformed.status, 400)
})

test('queries VictoriaLogs and parses JSONL audit events', async () => {
  const response = await adminFetch('/api/admin/audit?query=*&limit=5')
  assert.equal(response.status, 200)
  const body = await response.json()
  assert.equal(body.count, 2)
  assert.equal(body.events[0].user_id, 'sysadmin-01')
})

test('reports service pids and restarts a service through platform.sh', async () => {
  writeFileSync(join(scratch, 'run', 'audit_outbox.pid'), `${process.pid}\n`)

  const listed = await adminFetch('/api/admin/services')
  assert.equal(listed.status, 200)
  const services = (await listed.json()).services
  assert.equal(services.find((service) => service.name === 'audit_outbox').running, true)
  assert.ok(services.some((service) => service.name !== 'audit_outbox' && service.running === false))

  assert.equal((await adminFetch('/api/admin/services/audit_outbox/restart', { method: 'POST' })).status, 200)
  assert.match(readFileSync(commandLog, 'utf8'), /service audit_outbox restart/)
  assert.match(readFileSync(commandLog, 'utf8'), /agent-port=3090/)
  assert.equal((await adminFetch('/api/admin/services/nope/restart', { method: 'POST' })).status, 404)
})

test('quota controls require admin and CSRF protection and propagate store failures', async () => {
  assert.equal((await adminFetch('/api/admin/quotas', { cookie: '' })).status, 401)
  const path = '/api/admin/quotas/sysadmin-01'
  const body = { limits: { rpm: 12, daily_tokens: 5000 } }
  assert.equal((await adminFetch(path, { method: 'POST', body, headers: { origin: 'http://evil.example' } })).status, 403)
  assert.equal((await adminFetch(path, { method: 'POST', body })).status, 200)
  assert.deepEqual(stubState.quotaBody, body)
  assert.equal((await adminFetch('/api/admin/quotas')).status, 200)
  stubState.quotaStatus = 503
  try {
    assert.equal((await adminFetch(path, { method: 'POST', body })).status, 503)
    assert.equal((await adminFetch('/api/admin/quotas')).status, 503)
  } finally { stubState.quotaStatus = 200 }
})

test('model catalog is authenticated, sanitized and reports provider failure', async () => {
  assert.equal((await adminFetch('/api/admin/models', { cookie: '' })).status, 401)
  const response = await adminFetch('/api/admin/models')
  assert.equal(response.status, 200)
  const body = await response.json()
  assert.deepEqual(body.models, [{ id: 'fast-model', owned_by: 'local' }])
  assert.ok(!JSON.stringify(body).includes('must-not-leak'))
  stubState.modelsStatus = 503
  try {
    assert.equal((await adminFetch('/api/admin/models')).status, 503)
  } finally { stubState.modelsStatus = 200 }
})

test('local model management is authenticated, CSRF-guarded and proxied', async () => {
  assert.equal((await adminFetch('/api/admin/local-models', { cookie: '' })).status, 401)
  const listed = await adminFetch('/api/admin/local-models')
  assert.equal(listed.status, 200)
  assert.equal((await listed.json()).models[0].name, 'local-1')

  const registered = await adminFetch('/api/admin/local-models', { method: 'POST', body: { hf_repo: 'org/local-1' } })
  assert.equal(registered.status, 201)
  assert.deepEqual(stubState.modelBody, { hf_repo: 'org/local-1' })

  const patched = await adminFetch('/api/admin/local-models/local-1', {
    method: 'PATCH', body: { dtype: 'bfloat16', max_num_seqs: 32 },
  })
  assert.equal(patched.status, 200)
  assert.equal((await patched.json()).model.dtype, 'bfloat16')
  assert.deepEqual(stubState.modelBody, { dtype: 'bfloat16', max_num_seqs: 32 })

  assert.equal((await adminFetch('/api/admin/local-models/local-1/start', { method: 'POST', body: {} })).status, 202)
  assert.equal(
    (await adminFetch('/api/admin/local-models/local-1/start', { method: 'POST', body: {}, headers: { origin: 'http://evil.example' } })).status,
    403,
  )
  assert.equal((await adminFetch('/api/admin/local-models/local-1/delete?delete_files=1', { method: 'POST', body: {} })).status, 200)
})

test('hardware survey is authenticated and proxied to the agent platform', async () => {
  assert.equal((await adminFetch('/api/admin/survey', { cookie: '' })).status, 401)
  const response = await adminFetch('/api/admin/survey?refresh=1')
  assert.equal(response.status, 200)
  assert.equal((await response.json()).hostname, 'stub-host')
})

test('starts and stops allowlisted services without allowing self-termination', async () => {
  for (const action of ['start', 'stop']) {
    const response = await adminFetch(`/api/admin/services/audit_outbox/${action}`, { method: 'POST', body: {} })
    assert.equal(response.status, 200)
    assert.ok(readFileSync(commandLog, 'utf8').includes(`service audit_outbox ${action}`))
    assert.equal((await adminFetch(`/api/admin/services/nope/${action}`, { method: 'POST', body: {} })).status, 404)
    assert.equal((await adminFetch(`/api/admin/services/harness_gateway/${action}`, { method: 'POST', body: {} })).status, 409)
  }
})
