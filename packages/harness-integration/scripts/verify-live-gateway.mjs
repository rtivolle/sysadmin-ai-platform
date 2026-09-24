#!/usr/bin/env node
/**
 * Live multi-user end-to-end verification.
 *
 * Unlike tests/gateway.test.mjs (which injects a stub instance manager), this
 * drives the real gateway against:
 *   - a RUNNING platform auth gateway (real PBKDF2 credentials, real Valkey sessions),
 *   - the REAL per-user instance manager, so each login boots an actual
 *     `dsh --profile sysadmin` process,
 *   - the Mila admin console (master-key login, overview, instances, approvals,
 *     audit, services and session revocation),
 *   - the live agent platform for a real approval round-trip.
 *
 * It asserts that two different sysadmins can log in, receive distinct sessions,
 * are routed to distinct harness instances, and can each reach their own harness
 * web surface through the gateway. It then exercises the admin console and
 * verifies that gateway sessions and adopted harness instances survive a gateway
 * restart on the same state root.
 *
 * Prerequisites: the auth gateway, the agent platform and the installer's key
 * files must be in place. A VictoriaLogs or shared-store outage is reported as a
 * FAIL for the affected check, not a crash. The script never starts a service
 * and exits 2 when a prerequisite is missing.
 *
 * Usage:
 *   node scripts/verify-live-gateway.mjs --auth-url http://127.0.0.1:3081 \
 *     --backend-url http://127.0.0.1:3080 --dsh-bin /path/to/dsh \
 *     [--users sysadmin-01,sysadmin-02] [--revoke-user sysadmin-03] \
 *     [--master-key-file backend/config/keys/master.key] [--admin-token …] [--keep]
 *
 * Secrets (master token, passwords, cookies) are never printed; they appear as
 * token=… in the output.
 */
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { loadConfig } from '../gateway/config.js'
import { createGateway, waitForLaunchToken } from '../gateway/server.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const PKG_DIR = resolve(HERE, '..')
const REPO_ROOT = resolve(PKG_DIR, '..', '..')

function arg(name, fallback) {
  const index = process.argv.indexOf(`--${name}`)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}
const hasFlag = (name) => process.argv.includes(`--${name}`)

const AUTH_URL = arg('auth-url', 'http://127.0.0.1:3091')
const BACKEND_URL = arg('backend-url', process.env.SYSADMIN_BACKEND_URL ?? 'http://127.0.0.1:3080').replace(/\/+$/, '')
const DSH_BIN = arg('dsh-bin', process.env.DSH_BIN ?? 'dsh')
const USERS = arg('users', 'sysadmin-01,sysadmin-02').split(',').map((u) => u.trim()).filter(Boolean)
const PASSWORDS_FILE = arg('passwords-file', join(REPO_ROOT, 'backend', 'config', 'keys', 'initial-passwords.txt'))
const MASTER_KEY_FILE = arg('master-key-file', join(REPO_ROOT, 'backend', 'config', 'keys', 'master.key'))
const REVOKE_USER = arg('revoke-user', 'sysadmin-03')
const ADMIN_TOKEN_OVERRIDE = arg('admin-token', '')

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

async function postJson(url, body, headers = {}, timeoutMs = 60000) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  })
  let parsed = null
  try {
    parsed = await response.json()
  } catch {
    /* non-JSON */
  }
  return { response, body: parsed }
}

/** postJson that reports transport errors instead of throwing. */
async function tryPostJson(url, body, headers = {}, timeoutMs = 60000) {
  try {
    return await postJson(url, body, headers, timeoutMs)
  } catch (error) {
    return { response: null, body: null, error }
  }
}

async function safeFetch(url, options = {}) {
  try {
    return { response: await fetch(url, options), error: null }
  } catch (error) {
    return { response: null, error }
  }
}

async function jsonOf(response) {
  if (!response) return null
  try {
    return await response.json()
  } catch {
    return null
  }
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * Set-Cookie values as attribute-free `name=value` pairs. Never printed: they
 * are bearer material.
 *
 * @param {Response|null|undefined} response
 * @returns {string[]}
 */
function setCookiePairs(response) {
  if (!response) return []
  const values = typeof response.headers.getSetCookie === 'function'
    ? response.headers.getSetCookie()
    : [response.headers.get('set-cookie') ?? '']
  return values.map((value) => String(value).split(';')[0]).filter((pair) => pair.includes('=') && !pair.startsWith('='))
}

/**
 * Keep every harness cookie a response issues, keyed by cookie name.
 *
 * @param {string[]} store mutable list of `name=value` pairs
 * @param {Response|null|undefined} response
 */
function mergeHarnessCookies(store, response) {
  for (const pair of setCookiePairs(response)) {
    const name = pair.slice(0, pair.indexOf('='))
    if (name === 'sysadmin_launch_done' || name.startsWith('sysadmin_')) continue
    const index = store.findIndex((cookie) => cookie.startsWith(`${name}=`))
    if (index >= 0) store[index] = pair
    else store.push(pair)
  }
}

/** Browsing cookie header: gateway session, launch-done flag, harness cookies. */
function cookieHeader(sessionCookie, harnessCookies = []) {
  return [sessionCookie, 'sysadmin_launch_done=1', ...harnessCookies].join('; ')
}

/**
 * Walk redirects the way a browser would, collecting harness cookies, until a
 * non-redirect response arrives. Returns the final Response (or null).
 *
 * @param {URL|string} startUrl
 * @param {string} sessionCookie
 * @param {string[]} harnessCookies
 * @param {number} [maxHops]
 */
async function followToSurface(startUrl, sessionCookie, harnessCookies, maxHops = 5) {
  let url = new URL(startUrl)
  let response = null
  for (let hop = 0; hop < maxHops; hop += 1) {
    const current = await safeFetch(url, {
      headers: { cookie: cookieHeader(sessionCookie, harnessCookies) },
      redirect: 'manual',
      signal: AbortSignal.timeout(20000),
    })
    response = current.response
    if (!response) return null
    mergeHarnessCookies(harnessCookies, response)
    if (response.status >= 300 && response.status < 400) {
      const location = response.headers.get('location')
      if (!location) break
      url = new URL(location, url)
      continue
    }
    break
  }
  return response
}

async function main() {
  // ── Prerequisites: fail fast (exit 2), never start a service. ─────────────
  if (!existsSync(PASSWORDS_FILE)) {
    console.error(`Missing passwords file: ${PASSWORDS_FILE}`)
    process.exit(2)
  }
  if (!existsSync(MASTER_KEY_FILE)) {
    console.error(`Missing master key file: ${MASTER_KEY_FILE} (the gateway verifies admin logins against it)`)
    process.exit(2)
  }
  let adminToken = ADMIN_TOKEN_OVERRIDE
  let adminTokenSource = '--admin-token'
  if (!adminToken) {
    try {
      adminToken = readFileSync(MASTER_KEY_FILE, 'utf8').trim()
    } catch (error) {
      console.error(`Unreadable master key file: ${MASTER_KEY_FILE}: ${error.message}`)
      process.exit(2)
    }
    adminTokenSource = MASTER_KEY_FILE
    if (!adminToken) {
      console.error(`Empty master key file: ${MASTER_KEY_FILE}`)
      process.exit(2)
    }
  }

  let authHealth
  try {
    const response = await fetch(`${AUTH_URL}/health`, { signal: AbortSignal.timeout(5000) })
    authHealth = response.status
  } catch (error) {
    console.error(`Auth gateway not reachable at ${AUTH_URL}: ${error.message}`)
    process.exit(2)
  }

  let backendProbe = 'unreachable'
  try {
    const response = await fetch(`${BACKEND_URL}/health`, { signal: AbortSignal.timeout(5000) })
    backendProbe = `HTTP ${response.status}`
  } catch {
    /* backend-dependent checks record their own FAIL below */
  }

  console.log(`live gateway check against auth=${AUTH_URL} (health ${authHealth}), backend=${BACKEND_URL} (${backendProbe})`)
  console.log(`users=${USERS.join(', ')}, revoke-user=${REVOKE_USER}, master token=… (from ${adminTokenSource})\n`)

  const passwords = readPasswords(PASSWORDS_FILE)
  const stateRoot = mkdtempSync(join(tmpdir(), 'dsh-live-'))
  const config = {
    ...loadConfig({}),
    authUrl: AUTH_URL,
    agentUrl: BACKEND_URL,
    masterKeyFile: MASTER_KEY_FILE,
    dshBin: DSH_BIN,
    stateRoot,
    dshHomeRoot: join(stateRoot, 'homes'),
    sessionsFile: join(stateRoot, 'sessions.jsonl'),
    adminSessionsFile: join(stateRoot, 'admin-sessions.jsonl'),
    instanceRegistryDir: join(stateRoot, 'instances'),
    instanceLogDir: join(stateRoot, 'logs'),
    instancePortStart: 3210,
    instancePortEnd: 3260,
    instanceReadyTimeoutMs: 90000,
  }

  const gateway = createGateway({ config, logger: () => {} })
  const firstGateway = await gateway.start(0)
  let currentUrl = firstGateway.url
  let activeGateway = gateway
  console.log(`gateway listening on ${currentUrl}\n`)

  const sessions = new Map()
  const harnessCookies = new Map()
  let adminCookie = ''

  try {
    // 0. A wrong password must be refused by the real auth gateway.
    const victim = USERS[0]
    const bad = await postJson(`${currentUrl}/api/gateway/login`, { username: victim, password: 'definitely-not-the-password' })
    record('wrong password refused', bad.response.status === 401, `HTTP ${bad.response.status}`)

    for (const user of USERS) {
      const password = passwords.get(user)
      if (!password) {
        record(`${user} password available`, false, 'not present in passwords file')
        continue
      }

      // 1. Real login through the real auth gateway.
      const login = await postJson(`${currentUrl}/api/gateway/login`, { username: user, password })
      const cookie = login.response.headers.get('set-cookie') ?? ''
      const sessionCookie = cookie.split(';')[0]
      record(`${user} logs in`, login.response.status === 200 && login.body?.user === user,
        `HTTP ${login.response.status} user=${login.body?.user ?? 'none'} detail=${login.body?.detail ?? ''}`)
      if (login.response.status !== 200) continue
      sessions.set(user, sessionCookie)
      harnessCookies.set(user, [])

      // 2. The user's own harness instance is running and reachable.
      const instance = gateway.instances.get(user)
      record(`${user} harness instance running`, Boolean(instance?.running), instance ? `port ${instance.port}` : 'no instance')
      // The launch token is scanned from process output and may arrive a moment
      // after readiness.
      const launchToken = instance ? await waitForLaunchToken(instance, 10000) : null
      record(`${user} launch token captured`, Boolean(launchToken), launchToken ? 'captured' : 'not captured from stdout')

      // 3. The gateway hands the browser through the instance's launch token,
      //    then the instance serves its web surface (following the harness's own
      //    303 redirect while collecting its cookie, like a browser).
      const handoff = await fetch(`${currentUrl}/`, {
        headers: { cookie: sessionCookie },
        redirect: 'manual',
        signal: AbortSignal.timeout(20000),
      })
      const location = handoff.headers.get('location') ?? ''
      record(
        `${user} handed to launch URL`,
        (handoff.status === 302 || handoff.status === 303) && location.includes('token='),
        `HTTP ${handoff.status} ${location.replace(/token=.*/u, 'token=…')}`,
      )

      let surfaceStatus = 0
      if (location.includes('token=')) {
        const surface = await followToSurface(new URL(location, currentUrl), sessionCookie, harnessCookies.get(user))
        surfaceStatus = surface?.status ?? 0
      }
      record(`${user} reaches harness web surface`, surfaceStatus === 200, `HTTP ${surfaceStatus}`)
    }

    // 4. Distinct users must be on distinct instances.
    if (sessions.size >= 2) {
      const ports = USERS.map((user) => gateway.instances.get(user)?.port).filter(Boolean)
      record('users are isolated on distinct instances', new Set(ports).size === ports.length, `ports ${ports.join(', ')}`)

      // 5. Each session routes to its own user's instance.
      const [first, second] = USERS
      const firstInstance = gateway.instances.get(first)
      const secondInstance = gateway.instances.get(second)
      const cross = await followToSurface(`${currentUrl}/`, sessions.get(second), harnessCookies.get(second))
      record(
        'sessions route to their own identity only',
        cross?.status === 200 && firstInstance?.port !== secondInstance?.port,
        `second user HTTP ${cross?.status ?? 'unreachable'}`,
      )
    }

    // ── Admin console, approvals, audit and services ────────────────────────

    // 6. Mila branding on both unauthenticated pages.
    const loginPage = await safeFetch(`${currentUrl}/api/gateway/login`, { signal: AbortSignal.timeout(10000) })
    if (loginPage.error) {
      record('login page carries Mila branding', false, `unreachable: ${loginPage.error.message}`)
    } else {
      const html = await loginPage.response.text()
      record(
        'login page carries Mila branding',
        loginPage.response.status === 200 && html.includes('mila.quebec') && html.includes('/assets/mila-logo.png'),
        `HTTP ${loginPage.response.status}`,
      )
    }

    const adminPage = await safeFetch(`${currentUrl}/admin`, { signal: AbortSignal.timeout(10000) })
    if (adminPage.error) {
      record('admin page carries Mila branding', false, `unreachable: ${adminPage.error.message}`)
    } else {
      const html = await adminPage.response.text()
      record(
        'admin page carries Mila branding',
        adminPage.response.status === 200 && html.includes('mila.quebec') && html.includes('/assets/mila-logo.png'),
        `HTTP ${adminPage.response.status}`,
      )
    }

    // 7. Admin console authentication with the master token.
    const adminLogin = await tryPostJson(`${currentUrl}/api/admin/login`, { token: adminToken }, { 'x-sysadmin-admin': '1' })
    adminCookie = (adminLogin.response?.headers.get('set-cookie') ?? '').split(';')[0]
    record(
      'admin login accepted with the master token',
      adminLogin.response?.status === 200 && adminCookie.startsWith(`${config.adminCookieName}=`),
      adminLogin.error ? `unreachable: ${adminLogin.error.message}` : `HTTP ${adminLogin.response.status} ${config.adminCookieName}=… token=…`,
    )
    const wrongToken = await tryPostJson(`${currentUrl}/api/admin/login`, { token: 'not-the-master-token' }, { 'x-sysadmin-admin': '1' })
    record(
      'admin login rejects a wrong token',
      wrongToken.response?.status === 401,
      wrongToken.error ? `unreachable: ${wrongToken.error.message}` : `HTTP ${wrongToken.response.status} token=…`,
    )

    // 8. Overview probe with the admin cookie.
    const overview = await safeFetch(`${currentUrl}/api/admin/overview`, {
      headers: { cookie: adminCookie },
      signal: AbortSignal.timeout(15000),
    })
    const overviewBody = await jsonOf(overview.response)
    if (overview.error || !Array.isArray(overviewBody?.services)) {
      record('admin overview lists services', false,
        overview.error ? `unreachable: ${overview.error.message}` : `HTTP ${overview.response?.status} no services array`)
    } else {
      const up = overviewBody.services.filter((service) => service.status === 'up').length
      const degraded = overviewBody.services.filter((service) => service.status === 'degraded').length
      record('admin overview lists services', true,
        `HTTP ${overview.response.status} up=${up} down=${overviewBody.services.length - up - degraded} degraded=${degraded}`)
    }

    // 9. Admin-driven instance restart keeps the user's port.
    const restartUser = USERS[0]
    if (!adminCookie) {
      record(`${restartUser} instance listed by the admin console`, false, 'no admin cookie')
      record(`admin restart of ${restartUser} accepted`, false, 'no admin cookie')
      record(`${restartUser} active after admin restart`, false, 'no admin cookie')
      record(`admin restart reuses ${restartUser} port`, false, 'no admin cookie')
    } else {
      const listed = await safeFetch(`${currentUrl}/api/admin/instances`, {
        headers: { cookie: adminCookie },
        signal: AbortSignal.timeout(10000),
      })
      const listedBody = await jsonOf(listed.response)
      const entry = listedBody?.instances?.find((instance) => instance.userId === restartUser)
      record(`${restartUser} instance listed by the admin console`, Boolean(entry),
        entry ? `port ${entry.port} state=${entry.state}` : `HTTP ${listed.response?.status ?? 'unreachable'} instance not listed`)

      const portBefore = entry?.port ?? gateway.instances.get(restartUser)?.port ?? null
      const restart = await tryPostJson(`${currentUrl}/api/admin/instances/${encodeURIComponent(restartUser)}/restart`, {},
        { 'x-sysadmin-admin': '1', cookie: adminCookie }, 120000)
      record(`admin restart of ${restartUser} accepted`, restart.response?.status === 200,
        restart.error
          ? `unreachable: ${restart.error.message}`
          : `HTTP ${restart.response.status} detail=${restart.body?.detail ?? ''}`)

      let active = false
      const deadline = Date.now() + 60000
      while (Date.now() < deadline && !active) {
        const health = await safeFetch(`${currentUrl}/api/gateway/health`, { signal: AbortSignal.timeout(5000) })
        const healthBody = await jsonOf(health.response)
        active = Array.isArray(healthBody?.active_users) && healthBody.active_users.includes(restartUser)
        if (!active) await delay(1000)
      }
      record(`${restartUser} active after admin restart`, active,
        active ? 'present in active_users within 60s' : 'not in active_users after 60s')

      const portAfter = gateway.instances.get(restartUser)?.port ?? restart.body?.instance?.port ?? null
      record(`admin restart reuses ${restartUser} port`, portBefore !== null && portAfter === portBefore,
        `port ${portBefore} -> ${portAfter}`)
    }

    // 10. Approval round-trip: backend request → admin console → decision.
    const approvalUser = USERS[0]
    let approvalId = ''
    const userKeyPath = join(config.keysDir, `${approvalUser}.key`)
    if (!existsSync(userKeyPath)) {
      record('backend requests approval', false, `missing key file ${userKeyPath}`)
    } else {
      const userToken = readFileSync(userKeyPath, 'utf8').trim()
      const execute = await safeFetch(`${BACKEND_URL}/api/tools/execute`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: `Bearer ${userToken}` },
        body: JSON.stringify({
          name: 'sandboxed_bash',
          // A mutating but non-destructive command: the policy gate classifies
          // it APPROVAL_REQUIRED (202), while `rm -rf` is BLOCKED (403).
          parameters: { command: 'touch /tmp/dsh-verify-approval' },
          session_id: 'verify-live',
        }),
        signal: AbortSignal.timeout(30000),
      })
      const executeBody = await jsonOf(execute.response)
      if (execute.error) {
        record('backend requests approval', false, `unreachable: ${execute.error.message}`)
      } else if (execute.response.status === 202 && typeof executeBody?.approval_id === 'string') {
        approvalId = executeBody.approval_id
        record('backend requests approval', true, `HTTP 202 approval_id=${approvalId}`)
      } else {
        record('backend requests approval', false,
          `HTTP ${execute.response.status} detail=${executeBody?.detail ?? 'no detail'}`)
      }
    }

    if (!approvalId) {
      record('approval visible in the admin console', false, 'no approval_id requested')
      record('admin console decides the approval', false, 'no approval_id requested')
    } else {
      const pending = await safeFetch(`${currentUrl}/api/admin/approvals`, {
        headers: { cookie: adminCookie },
        signal: AbortSignal.timeout(15000),
      })
      const pendingBody = await jsonOf(pending.response)
      const visible = pending.response?.status === 200
        && Array.isArray(pendingBody?.pending_approvals)
        && pendingBody.pending_approvals.some((approval) => approval?.approval_id === approvalId)
      record('approval visible in the admin console', visible,
        `HTTP ${pending.response?.status ?? 'unreachable'} listed=${visible}`)

      const decide = await tryPostJson(`${currentUrl}/api/admin/approvals/decide`,
        { approval_id: approvalId, approved: true }, { 'x-sysadmin-admin': '1', cookie: adminCookie }, 30000)
      record('admin console decides the approval', decide.response?.status === 200,
        decide.error
          ? `unreachable: ${decide.error.message}`
          : `HTTP ${decide.response.status} detail=${decide.body?.detail ?? ''}`)
    }

    // 11. Audit query through the admin console. A VictoriaLogs outage surfaces
    //     as 503 and is reported, not treated as a crash.
    const audit = await safeFetch(`${currentUrl}/api/admin/audit?query=*&limit=5`, {
      headers: { cookie: adminCookie },
      signal: AbortSignal.timeout(15000),
    })
    const auditBody = await jsonOf(audit.response)
    if (audit.error) {
      record('admin audit query returns events', false, `unreachable: ${audit.error.message}`)
    } else if (audit.response.status === 503) {
      record('admin audit query returns events', false,
        `HTTP 503 VictoriaLogs unavailable: ${auditBody?.detail ?? 'no detail'}`)
    } else {
      const events = Array.isArray(auditBody?.events)
      record('admin audit query returns events', audit.response.status === 200 && events,
        `HTTP ${audit.response.status} count=${auditBody?.count ?? (events ? auditBody.events.length : 'none')}`)
    }

    // 12. Service inventory and a restart of the portless outbox worker.
    const serviceList = await safeFetch(`${currentUrl}/api/admin/services`, {
      headers: { cookie: adminCookie },
      signal: AbortSignal.timeout(10000),
    })
    const serviceListBody = await jsonOf(serviceList.response)
    record('admin lists backend services',
      serviceList.response?.status === 200 && Array.isArray(serviceListBody?.services),
      `HTTP ${serviceList.response?.status ?? 'unreachable'} count=${serviceListBody?.services?.length ?? 'none'}`)

    const serviceRestart = await tryPostJson(`${currentUrl}/api/admin/services/audit_outbox/restart`, {},
      { 'x-sysadmin-admin': '1', cookie: adminCookie }, 60000)
    record('admin restarts audit_outbox', serviceRestart.response?.status === 200,
      serviceRestart.error
        ? `unreachable: ${serviceRestart.error.message}`
        : `HTTP ${serviceRestart.response.status} status=${serviceRestart.body?.status ?? ''} exit=${serviceRestart.body?.exitCode ?? ''}`)

    // 13. Session revocation from the admin console.
    const revokePassword = passwords.get(REVOKE_USER)
    if (!revokePassword) {
      record(`session revocation for ${REVOKE_USER}`, false, 'password not present in passwords file')
    } else if (!adminCookie) {
      record(`session revocation for ${REVOKE_USER}`, false, 'no admin cookie')
    } else {
      const healthBefore = await safeFetch(`${currentUrl}/api/gateway/health`, { signal: AbortSignal.timeout(5000) })
      const healthBeforeBody = await jsonOf(healthBefore.response)
      const sessionsBefore = await safeFetch(`${currentUrl}/api/admin/sessions`, {
        headers: { cookie: adminCookie },
        signal: AbortSignal.timeout(10000),
      })
      const sessionsBeforeBody = await jsonOf(sessionsBefore.response)
      const beforeIds = new Set((sessionsBeforeBody?.sessions ?? []).map((row) => row.id))

      const revokeLogin = await tryPostJson(`${currentUrl}/api/gateway/login`, { username: REVOKE_USER, password: revokePassword })
      const revokeCookie = (revokeLogin.response?.headers.get('set-cookie') ?? '').split(';')[0]
      record(`${REVOKE_USER} logs in for revocation`, revokeLogin.response?.status === 200,
        revokeLogin.error ? `unreachable: ${revokeLogin.error.message}` : `HTTP ${revokeLogin.response?.status ?? 'error'}`)

      const healthAfter = await safeFetch(`${currentUrl}/api/gateway/health`, { signal: AbortSignal.timeout(5000) })
      const healthAfterBody = await jsonOf(healthAfter.response)
      const grew = Number.isInteger(healthBeforeBody?.sessions)
        && Number.isInteger(healthAfterBody?.sessions)
        && healthAfterBody.sessions > healthBeforeBody.sessions
      record('gateway session count grows on login', grew,
        `sessions ${healthBeforeBody?.sessions ?? '?'} -> ${healthAfterBody?.sessions ?? '?'}`)

      const sessionsAfter = await safeFetch(`${currentUrl}/api/admin/sessions`, {
        headers: { cookie: adminCookie },
        signal: AbortSignal.timeout(10000),
      })
      const sessionsAfterBody = await jsonOf(sessionsAfter.response)
      const row = (sessionsAfterBody?.sessions ?? []).find((candidate) => candidate.userId === REVOKE_USER && !beforeIds.has(candidate.id))
      if (!row) {
        record(`admin revokes ${REVOKE_USER} session`, false, `new session row for ${REVOKE_USER} not found`)
      } else {
        const revoke = await tryPostJson(`${currentUrl}/api/admin/sessions/${encodeURIComponent(row.id)}/revoke`,
          { stopInstance: false }, { 'x-sysadmin-admin': '1', cookie: adminCookie }, 30000)
        record(`admin revokes ${REVOKE_USER} session`, revoke.response?.status === 200,
          revoke.error
            ? `unreachable: ${revoke.error.message}`
            : `HTTP ${revoke.response.status} session=${row.id}`)
      }

      if (revokeCookie) {
        const revoked = await safeFetch(`${currentUrl}/`, {
          headers: { cookie: revokeCookie },
          redirect: 'manual',
          signal: AbortSignal.timeout(15000),
        })
        record('revoked session cookie is refused', revoked.response?.status === 401,
          revoked.error ? `unreachable: ${revoked.error.message}` : `HTTP ${revoked.response.status}`)
      } else {
        record('revoked session cookie is refused', false, 'no session cookie from the revocation login')
      }
    }

    // 14. Persistence: sessions and adopted instances survive a gateway
    //     restart on the same state root. Recorded before the logout check,
    //     which invalidates USERS[0]'s session.
    const persisted = new Map()
    for (const user of sessions.keys()) {
      const sessionCookie = sessions.get(user)
      const cookies = harnessCookies.get(user) ?? []

      // Walk the launch handoff once so the harness issues its own cookie.
      const handoff = await safeFetch(`${currentUrl}/`, {
        headers: { cookie: sessionCookie },
        redirect: 'manual',
        signal: AbortSignal.timeout(20000),
      })
      let location = handoff.response?.headers.get('location') ?? ''
      let hops = 0
      while (location && hops < 5) {
        const follow = await safeFetch(new URL(location, currentUrl), {
          headers: { cookie: cookieHeader(sessionCookie, cookies) },
          redirect: 'manual',
          signal: AbortSignal.timeout(20000),
        })
        mergeHarnessCookies(cookies, follow.response)
        if (follow.response && follow.response.status >= 300 && follow.response.status < 400) {
          location = follow.response.headers.get('location') ?? ''
          hops += 1
          continue
        }
        break
      }

      const instance = gateway.instances.get(user)
      persisted.set(user, { sessionCookie, port: instance?.port ?? null, pid: instance?.pid ?? null })
      console.log(`captured ${user} persistence state (port ${instance?.port ?? 'none'}, pid ${instance?.pid ?? 'none'}, harness cookies ${cookies.length})`)
    }

    await gateway.stop({ stopInstances: false })
    activeGateway = null
    const gateway2 = createGateway({ config, logger: () => {} })
    activeGateway = gateway2
    const secondGateway = await gateway2.start(0)
    currentUrl = secondGateway.url
    console.log(`\ngateway restarted on ${currentUrl} with the same state root\n`)

    if (persisted.size === 0) {
      record('gateway restart persistence', false, 'no logged-in sessions to verify')
    }
    for (const [user, snapshot] of persisted) {
      const cookies = harnessCookies.get(user) ?? []
      let status = 0
      let failure = null
      try {
        const nav = await fetch(`${currentUrl}/`, {
          headers: { cookie: cookieHeader(snapshot.sessionCookie, cookies), accept: 'text/html' },
          redirect: 'manual',
          signal: AbortSignal.timeout(30000),
        })
        status = nav.status
      } catch (error) {
        failure = error.message
      }
      record(`${user} session survives the gateway restart`, status === 200,
        failure ? `error ${failure}` : `HTTP ${status}${status === 302 ? ' (launch-token recovery)' : ''}`)

      const adopted = gateway2.instances.get(user)
      record(`${user} instance re-adopted on its port`, adopted?.port === snapshot.port && adopted?.adopted === true,
        `port ${snapshot.port} -> ${adopted?.port ?? 'none'} adopted=${adopted?.adopted ?? false}`)
    }

    // 15. Logout invalidates the gateway session (after the persistence checks).
    const firstUser = USERS[0]
    if (sessions.has(firstUser)) {
      await fetch(`${currentUrl}/api/gateway/logout`, { method: 'POST', headers: { cookie: sessions.get(firstUser) } })
      const after = await fetch(`${currentUrl}/`, {
        headers: { cookie: `${sessions.get(firstUser)}; sysadmin_launch_done=1` },
        redirect: 'manual',
      })
      record('logout invalidates the session', after.status === 401, `HTTP ${after.status}`)
    }

    await gateway2.stop()
    activeGateway = null
  } finally {
    if (activeGateway) await activeGateway.stop()
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
