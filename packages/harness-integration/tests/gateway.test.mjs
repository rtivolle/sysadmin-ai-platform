/**
 * Gateway tests: multi-user session handling, login against the auth gateway,
 * identity-scoped instance selection, and request proxying.
 *
 * These run with stub auth/upstream servers and an injected instance manager, so
 * they exercise the gateway's own contract without spawning real harness
 * processes.
 */
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { after, before, test } from 'node:test'

import { isBareAuthority, loadConfig, trustedHostList } from '../gateway/config.js'
import {
  assertSafeUserId,
  findFreePort,
  portIsFree,
  provisionProfile,
  readUserToken,
} from '../gateway/instance-manager.js'
import { browserProxyHeaders, createGateway, parseCredentials } from '../gateway/server.js'
import { parseCookies, SessionStore } from '../gateway/session-store.js'

const scratch = mkdtempSync(join(tmpdir(), 'dsh-gateway-'))
after(() => rmSync(scratch, { recursive: true, force: true }))

/** @returns {Promise<{server: import('node:http').Server, port: number, url: string}>} */
function listen(handler) {
  return new Promise((resolve) => {
    const server = createServer(handler)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      resolve({ server, port: address.port, url: `http://127.0.0.1:${address.port}` })
    })
  })
}

function close(server) {
  server.closeAllConnections?.()
  return new Promise((resolve) => server.close(() => resolve()))
}

test('session store issues, resolves, expires and deletes sessions', () => {
  let now = 1000
  const store = new SessionStore({ ttlMs: 100, now: () => now })

  const id = store.create('sysadmin-01')
  assert.equal(store.get(id).userId, 'sysadmin-01')

  now = 1100
  assert.equal(store.get(id), null, 'session must expire at its TTL')
})

test('cookie parsing tolerates junk and decodes values', () => {
  const cookies = parseCookies('a=1; sysadmin_gateway=abc%20def; bad; b=2')
  assert.equal(cookies.a, '1')
  assert.equal(cookies.b, '2')
  assert.equal(cookies.sysadmin_gateway, 'abc def')
})

test('proxy keeps the browser Host instead of the loopback instance port', () => {
  const headers = browserProxyHeaders({
    host: '192.168.14.159:3085',
    origin: 'http://192.168.14.159:3085',
    connection: 'keep-alive',
    upgrade: 'websocket',
  }, { host: '127.0.0.1', port: 3180 })
  assert.equal(headers.host, '192.168.14.159:3085')
  assert.equal(headers.origin, 'http://192.168.14.159:3085')
  assert.equal(headers.connection, undefined)
  assert.equal(headers.upgrade, undefined)
  assert.equal(browserProxyHeaders({}, { host: '127.0.0.1', port: 3180 }).host, '127.0.0.1:3180')
})

test('trusted hosts are bare authorities and include this host LAN addresses', () => {
  assert.equal(isBareAuthority('192.168.14.159'), true)
  assert.equal(isBareAuthority('192.168.14.159:3085'), true)
  assert.equal(isBareAuthority('app.internal/path'), false)
  assert.equal(isBareAuthority('user@host'), false)
  const listed = trustedHostList({ SYSADMIN_TRUSTED_HOSTS: 'mila.example, not a host' })
  assert.ok(listed.includes('mila.example'))
  assert.equal(listed.includes('not a host'), false)
  for (const entry of listed) assert.equal(isBareAuthority(entry), true)
})

test('login body parsing accepts JSON and form encodings', () => {
  assert.deepEqual(
    parseCredentials('{"username":"sysadmin-01","password":"pw"}', 'application/json'),
    { username: 'sysadmin-01', password: 'pw' },
  )
  assert.deepEqual(
    parseCredentials('username=sysadmin-02&password=secret', 'application/x-www-form-urlencoded'),
    { username: 'sysadmin-02', password: 'secret' },
  )
})

test('rejects user ids that could escape the per-user state directory', () => {
  assert.throws(() => assertSafeUserId('../etc'))
  assert.throws(() => assertSafeUserId('a/b'))
  assert.throws(() => assertSafeUserId(''))
  assert.doesNotThrow(() => assertSafeUserId('sysadmin-01'))
  assert.doesNotThrow(() => assertSafeUserId('emergency-p1-oncall'))
})

test('finds a free port and reports a bound one as taken', async () => {
  const { server, port } = await listen((_req, res) => res.end('x'))
  assert.equal(await portIsFree('127.0.0.1', port), false)
  await close(server)
  assert.equal(await portIsFree('127.0.0.1', port), true)
  const allocated = await findFreePort('127.0.0.1', 39000, 39100)
  assert.ok(allocated >= 39000 && allocated <= 39100)
})

test('provisions a per-user profile with the plugin resolvable in node_modules', () => {
  const home = join(scratch, 'home-sysadmin-01')
  const profileDir = provisionProfile({
    home,
    profileName: 'sysadmin',
    profileSource: join(import.meta.dirname, '..', 'profile'),
    pluginSource: join(import.meta.dirname, '..', 'dsh-plugin-sysadmin'),
  })

  const manifest = JSON.parse(readFileSync(join(profileDir, 'package.json'), 'utf8'))
  assert.ok(manifest.dsh.profile.bundles.includes('dsh-plugin-sysadmin'))
  assert.ok(readFileSync(join(profileDir, 'cordis.patch.yml'), 'utf8').includes('llm-pi-ai'))
  assert.ok(readFileSync(join(profileDir, 'node_modules', 'dsh-plugin-sysadmin', 'index.js'), 'utf8').includes('sysadmin-harness'))
})

test('reads a per-user token and fails loudly when it is missing', () => {
  const keysDir = join(scratch, 'keys')
  provisionProfile({
    home: join(scratch, 'home-token'),
    profileName: 'sysadmin',
    profileSource: join(import.meta.dirname, '..', 'profile'),
    pluginSource: join(import.meta.dirname, '..', 'dsh-plugin-sysadmin'),
  })
  rmSync(keysDir, { recursive: true, force: true })
  writeFileSync(join(scratch, 'placeholder'), '')
  assert.throws(() => readUserToken(keysDir, 'sysadmin-01'), /missing key file/)
})

test('gateway authenticates a user, scopes their instance, and proxies to it', async () => {
  const ensured = []
  const auth = await listen((req, res) => {
    let body = ''
    req.on('data', (chunk) => { body += chunk })
    req.on('end', () => {
      if (req.url === '/api/v1/auth/login' && body.includes('sysadmin-01') && body.includes('correct-horse')) {
        res.writeHead(200, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ status: 'authenticated', user: 'sysadmin-01', role: 'sysadmin' }))
      } else {
        res.writeHead(401, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ detail: 'Invalid sysadmin credentials' }))
      }
    })
  })

  const upstream = await listen((req, res) => {
    res.writeHead(200, { 'content-type': 'text/plain' })
    res.end(`UPSTREAM ${req.url}`)
  })

  const config = {
    ...loadConfig({}),
    authUrl: auth.url,
    instanceHost: '127.0.0.1',
    instancePortStart: upstream.port,
    instancePortEnd: upstream.port,
  }
  const touched = []
  const instances = {
    async ensure(userId) {
      ensured.push(userId)
      return { userId, port: upstream.port, launchToken: 'launch-token-1', url: upstream.url, adopted: false }
    },
    activeUsers: () => ensured,
    touch(userId) {
      touched.push(userId)
    },
  }

  const gateway = createGateway({ config, instances })
  const { url } = await gateway.start(0)

  try {
    // 1. Bad credentials are rejected with the backend's status.
    const bad = await fetch(`${url}/api/gateway/login`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ username: 'sysadmin-01', password: 'wrong' }),
    })
    assert.equal(bad.status, 401)

    // 2. Unauthenticated browser navigation is redirected to the login page.
    const anonymous = await fetch(`${url}/`, { redirect: 'manual', headers: { accept: 'text/html' } })
    assert.equal(anonymous.status, 302)
    assert.equal(anonymous.headers.get('location'), '/api/gateway/login')

    // 2b. An unauthenticated API call is refused instead of redirected.
    const anonymousApi = await fetch(`${url}/api/v1/agent/sessions`, { redirect: 'manual' })
    assert.equal(anonymousApi.status, 401)

    // 3. Good credentials return a session cookie and the user's identity.
    const login = await fetch(`${url}/api/gateway/login`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ username: 'sysadmin-01', password: 'correct-horse' }),
    })
    assert.equal(login.status, 200)
    const loginBody = await login.json()
    assert.equal(loginBody.user, 'sysadmin-01')
    assert.equal(loginBody.redirect, '/')
    const cookie = login.headers.get('set-cookie')
    assert.match(cookie, /sysadmin_gateway=/)
    assert.deepEqual(ensured, ['sysadmin-01'], 'login must start the user-scoped instance')

    const sessionCookie = cookie.split(';')[0]

    // 4. A first navigation is redirected through the launch token once.
    const launched = await fetch(`${url}/`, {
      headers: { cookie: sessionCookie },
      redirect: 'manual',
    })
    assert.equal(launched.status, 302)
    assert.equal(launched.headers.get('location'), '/?token=launch-token-1')

    // 5. Afterwards requests are proxied to that user's instance.
    const proxied = await fetch(`${url}/api/v1/agent/sessions`, {
      headers: { cookie: `${sessionCookie}; sysadmin_launch_done=1` },
    })
    assert.equal(proxied.status, 200)
    assert.equal(await proxied.text(), 'UPSTREAM /api/v1/agent/sessions')
    assert.ok(
      touched.length >= 1 && touched.every((userId) => userId === 'sysadmin-01'),
      'proxied requests must touch the user instance for idle eviction',
    )

    // 6. Logout invalidates the session.
    const logout = await fetch(`${url}/api/gateway/logout`, {
      method: 'POST',
      headers: { cookie: sessionCookie },
    })
    assert.equal(logout.status, 200)
    const afterLogout = await fetch(`${url}/api/v1/agent/sessions`, {
      headers: { cookie: `${sessionCookie}; sysadmin_launch_done=1` },
    })
    assert.equal(afterLogout.status, 401)
  } finally {
    await gateway.stop()
    await close(auth.server)
    await close(upstream.server)
  }
})

/** Auth stub accepting one user/password pair. */
function stubAuth(user, password) {
  return listen((req, res) => {
    let body = ''
    req.on('data', (chunk) => { body += chunk })
    req.on('end', () => {
      if (req.url === '/api/v1/auth/login' && body.includes(user) && body.includes(password)) {
        res.writeHead(200, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ status: 'authenticated', user, role: 'sysadmin' }))
      } else {
        res.writeHead(401, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ detail: 'Invalid sysadmin credentials' }))
      }
    })
  })
}

test('gateway recovers instances and re-ensures persisted sessions at boot', async () => {
  const calls = { recover: 0, reEnsure: 0, sweep: 0, preStarted: [] }
  const sessions = new SessionStore({ ttlMs: 60_000 })
  const adminSessions = new SessionStore({ ttlMs: 60_000 })
  sessions.create('sysadmin-01')

  const instances = {
    async recover() {
      calls.recover += 1
      return { adopted: ['sysadmin-01'], dropped: ['sysadmin-09'], unknownOwner: [] }
    },
    reEnsure(store) {
      calls.reEnsure += 1
      calls.preStarted = store.list().map((record) => record.userId)
    },
    startIdleSweeper() {
      calls.sweep += 1
    },
    activeUsers: () => ['sysadmin-01'],
  }

  const gateway = createGateway({
    config: { ...loadConfig({}), instanceRegistryDir: join(scratch, 'instances'), sessionsFile: null },
    sessions,
    adminSessions,
    instances,
  })
  await gateway.start(0)
  try {
    assert.equal(calls.recover, 1)
    assert.equal(calls.reEnsure, 1)
    assert.equal(calls.sweep, 1)
    assert.deepEqual(calls.preStarted, ['sysadmin-01'], 'boot must eagerly re-ensure users with persisted sessions')
  } finally {
    await gateway.stop()
  }
})

test('gateway sessions survive a gateway restart on the same state root', async () => {
  const auth = await stubAuth('sysadmin-01', 'correct-horse')
  const upstream = await listen((req, res) => {
    res.writeHead(200, { 'content-type': 'text/plain' })
    res.end(`UPSTREAM ${req.url}`)
  })
  const stateRoot = join(scratch, 'restart-state')
  const config = {
    ...loadConfig({}),
    authUrl: auth.url,
    stateRoot,
    sessionsFile: join(stateRoot, 'sessions.jsonl'),
    adminSessionsFile: join(stateRoot, 'admin-sessions.jsonl'),
    instanceRegistryDir: join(stateRoot, 'instances'),
    instanceLogDir: join(stateRoot, 'logs'),
    instanceHost: '127.0.0.1',
    instancePortStart: upstream.port,
    instancePortEnd: upstream.port,
  }
  const instances = {
    async ensure(userId) {
      return { userId, port: upstream.port, launchToken: 'restart-token', url: upstream.url, adopted: false }
    },
    activeUsers: () => ['sysadmin-01'],
    async stopAll() {},
  }

  const gateway1 = createGateway({ config, instances })
  const first = await gateway1.start(0)
  const login = await fetch(`${first.url}/api/gateway/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ username: 'sysadmin-01', password: 'correct-horse' }),
  })
  assert.equal(login.status, 200)
  const sessionCookie = login.headers.get('set-cookie').split(';')[0]
  await gateway1.stop({ stopInstances: false })

  const gateway2 = createGateway({ config, instances })
  const second = await gateway2.start(0)
  try {
    assert.equal(gateway2.sessions.get(sessionCookie.split('=')[1])?.userId, 'sysadmin-01', 'session must reload from disk')
    const proxied = await fetch(`${second.url}/api/v1/agent/sessions`, {
      headers: { cookie: `${sessionCookie}; sysadmin_launch_done=1` },
    })
    assert.equal(proxied.status, 200, 'a browser with a persisted cookie must not need to log in again')
    assert.equal(await proxied.text(), 'UPSTREAM /api/v1/agent/sessions')
  } finally {
    await gateway2.stop()
    await close(auth.server)
    await close(upstream.server)
  }
})
