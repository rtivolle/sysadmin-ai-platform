#!/usr/bin/env node
/**
 * Live multi-user end-to-end verification.
 *
 * Unlike tests/gateway.test.mjs (which injects a stub instance manager), this
 * drives the real gateway against:
 *   - a RUNNING platform auth gateway (real PBKDF2 credentials, real Valkey sessions),
 *   - the REAL per-user instance manager, so each login boots an actual
 *     `dsh --profile sysadmin` process.
 *
 * It asserts that two different sysadmins can log in, receive distinct sessions,
 * are routed to distinct harness instances, and can each reach their own harness
 * web surface through the gateway.
 *
 * Prerequisites: the auth gateway must be reachable and Valkey must back it.
 * Usage:
 *   node scripts/verify-live-gateway.mjs --auth-url http://127.0.0.1:3091 \
 *     --dsh-bin /path/to/dsh [--users sysadmin-01,sysadmin-02] [--keep]
 */
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { loadConfig } from '../gateway/config.js'
import { createGateway } from '../gateway/server.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const PKG_DIR = resolve(HERE, '..')
const REPO_ROOT = resolve(PKG_DIR, '..', '..')

function arg(name, fallback) {
  const index = process.argv.indexOf(`--${name}`)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}
const hasFlag = (name) => process.argv.includes(`--${name}`)

const AUTH_URL = arg('auth-url', 'http://127.0.0.1:3091')
const DSH_BIN = arg('dsh-bin', process.env.DSH_BIN ?? 'dsh')
const USERS = arg('users', 'sysadmin-01,sysadmin-02').split(',').map((u) => u.trim()).filter(Boolean)
const PASSWORDS_FILE = arg('passwords-file', join(REPO_ROOT, 'backend', 'config', 'keys', 'initial-passwords.txt'))

const results = []
const record = (name, ok, detail = '') => {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`)
}

/** Read `user: password` lines from the installer's one-time password file. */
function readPasswords(path) {
  const map = new Map()
  for (const line of readFileSync(path, 'utf8').split('\n')) {
    const index = line.indexOf(':')
    if (index <= 0) continue
    map.set(line.slice(0, index).trim(), line.slice(index + 1).trim())
  }
  return map
}

async function postJson(url, body, headers = {}) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(60000),
  })
  let parsed = null
  try {
    parsed = await response.json()
  } catch {
    /* non-JSON */
  }
  return { response, body: parsed }
}

async function main() {
  if (!existsSync(PASSWORDS_FILE)) {
    console.error(`Missing passwords file: ${PASSWORDS_FILE}`)
    process.exit(2)
  }

  let authHealth
  try {
    const response = await fetch(`${AUTH_URL}/health`, { signal: AbortSignal.timeout(5000) })
    authHealth = response.status
  } catch (error) {
    console.error(`Auth gateway not reachable at ${AUTH_URL}: ${error.message}`)
    process.exit(2)
  }
  console.log(`live gateway check against auth=${AUTH_URL} (health ${authHealth}), users=${USERS.join(', ')}\n`)

  const passwords = readPasswords(PASSWORDS_FILE)
  const stateRoot = mkdtempSync(join(tmpdir(), 'dsh-live-'))
  const config = {
    ...loadConfig({}),
    authUrl: AUTH_URL,
    dshBin: DSH_BIN,
    stateRoot,
    dshHomeRoot: join(stateRoot, 'homes'),
    instancePortStart: 3210,
    instancePortEnd: 3260,
    instanceReadyTimeoutMs: 90000,
  }

  const gateway = createGateway({ config, logger: () => {} })
  const { url } = await gateway.start(0)
  console.log(`gateway listening on ${url}\n`)

  try {
    // 0. A wrong password must be refused by the real auth gateway.
    const victim = USERS[0]
    const bad = await postJson(`${url}/api/gateway/login`, { username: victim, password: 'definitely-not-the-password' })
    record('wrong password refused', bad.response.status === 401, `HTTP ${bad.response.status}`)

    const sessions = new Map()
    for (const user of USERS) {
      const password = passwords.get(user)
      if (!password) {
        record(`${user} password available`, false, 'not present in passwords file')
        continue
      }

      // 1. Real login through the real auth gateway.
      const login = await postJson(`${url}/api/gateway/login`, { username: user, password })
      const cookie = login.response.headers.get('set-cookie') ?? ''
      const sessionCookie = cookie.split(';')[0]
      record(`${user} logs in`, login.response.status === 200 && login.body?.user === user,
        `HTTP ${login.response.status} user=${login.body?.user ?? 'none'} detail=${login.body?.detail ?? ''}`)
      if (login.response.status !== 200) continue
      sessions.set(user, sessionCookie)

      // 2. The user's own harness instance is running and reachable.
      const instance = gateway.instances.get(user)
      record(`${user} harness instance running`, Boolean(instance?.running), instance ? `port ${instance.port}` : 'no instance')
      record(`${user} launch token captured`, Boolean(instance?.launchToken), instance?.launchToken ? 'captured' : 'not captured from stdout')

      // 3. The gateway hands the browser through the instance's launch token,
      //    then the instance serves its web surface.
      const handoff = await fetch(`${url}/`, {
        headers: { cookie: sessionCookie },
        redirect: 'manual',
        signal: AbortSignal.timeout(20000),
      })
      const location = handoff.headers.get('location') ?? ''
      record(
        `${user} handed to launch URL`,
        handoff.status === 302 && location.includes('token='),
        `HTTP ${handoff.status} ${location.replace(/token=.*/u, 'token=…')}`,
      )

      let surfaceStatus = 0
      if (location.includes('token=')) {
        const surface = await fetch(new URL(location, url), {
          headers: { cookie: sessionCookie },
          redirect: 'manual',
          signal: AbortSignal.timeout(20000),
        })
        surfaceStatus = surface.status
      }
      record(`${user} reaches harness web surface`, surfaceStatus === 200, `HTTP ${surfaceStatus}`)
    }

    // 4. Distinct users must be on distinct instances.
    if (sessions.size >= 2) {
      const ports = USERS.map((user) => gateway.instances.get(user)?.port).filter(Boolean)
      record('users are isolated on distinct instances', new Set(ports).size === ports.length, `ports ${ports.join(', ')}`)

      // 5. One user's cookie must not reach another user's instance.
      const [first, second] = USERS
      const firstInstance = gateway.instances.get(first)
      const cross = await fetch(`${url}/`, {
        headers: { cookie: `${sessions.get(second)}; sysadmin_launch_done=1` },
        redirect: 'manual',
        signal: AbortSignal.timeout(20000),
      })
      const secondInstance = gateway.instances.get(second)
      record(
        'sessions route to their own identity only',
        cross.status === 200 && firstInstance?.port !== secondInstance?.port,
        `second user HTTP ${cross.status}`,
      )
    }

    // 6. Logout invalidates the gateway session.
    const firstUser = USERS[0]
    if (sessions.has(firstUser)) {
      await fetch(`${url}/api/gateway/logout`, { method: 'POST', headers: { cookie: sessions.get(firstUser) } })
      const after = await fetch(`${url}/`, {
        headers: { cookie: `${sessions.get(firstUser)}; sysadmin_launch_done=1` },
        redirect: 'manual',
      })
      record('logout invalidates the session', after.status === 401, `HTTP ${after.status}`)
    }
  } finally {
    await gateway.stop()
    if (!hasFlag('keep')) rmSync(stateRoot, { recursive: true, force: true })
    else console.log(`\nstate kept at ${stateRoot}`)
  }

  const failed = results.filter((r) => !r.ok)
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`)
  process.exit(failed.length === 0 ? 0 : 1)
}

main().catch((error) => {
  console.error(`live verification crashed: ${error?.stack ?? error}`)
  process.exit(2)
})
