#!/usr/bin/env node
/**
 * Repeatable verification of the custom sysadmin harness.
 *
 * It proves, against the real `dsh` binary:
 *   1. the profile composes with the dsh-plugin-sysadmin bundle,
 *   2. the LiteLLM route and per-user key reference are present,
 *   3. the harness boots and the plugin's load banner is printed, which can only
 *      happen after its imports resolved, Config validated, the backend tool
 *      registered, and the policy/audit listeners attached,
 *   4. the web surface binds and refuses an unauthenticated request.
 *
 * Usage:
 *   node scripts/verify-harness.mjs [--dsh-bin /path/to/dsh] [--port 3199] [--keep]
 */
import { spawn } from 'node:child_process'
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync } from 'node:fs'
import { createServer } from 'node:net'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PKG_DIR = resolve(HERE, '..')

function arg(name, fallback) {
  const index = process.argv.indexOf(`--${name}`)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}
const hasFlag = (name) => process.argv.includes(`--${name}`)

const DSH_BIN = arg('dsh-bin', process.env.DSH_BIN ?? 'dsh')
const PORT = Number.parseInt(arg('port', '3199'), 10)
const PROFILE = process.env.SYSADMIN_PROFILE ?? 'sysadmin'
const WORK = join(PKG_DIR, '.verify-home')

const results = []
const record = (name, ok, detail = '') => {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`)
}

function run(command, args, options = {}) {
  return new Promise((resolvePromise) => {
    const child = spawn(command, args, { ...options, stdio: ['ignore', 'pipe', 'pipe'] })
    let stdout = ''
    let stderr = ''
    child.stdout?.on('data', (chunk) => { stdout += chunk })
    child.stderr?.on('data', (chunk) => { stderr += chunk })
    child.on('error', (error) => resolvePromise({ code: -1, stdout, stderr: String(error) }))
    child.on('close', (code) => resolvePromise({ code, stdout, stderr }))
  })
}

function delay(ms) {
  return new Promise((r) => setTimeout(r, ms))
}

async function freePort() {
  return new Promise((resolvePromise, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      server.close(() => resolvePromise(port))
    })
  })
}

async function main() {
  console.log(`verifying profile '${PROFILE}' with dsh at ${DSH_BIN}\n`)

  rmSync(WORK, { recursive: true, force: true })
  const profileDir = join(WORK, 'profiles', PROFILE)
  mkdirSync(join(profileDir, 'node_modules'), { recursive: true })
  cpSync(join(PKG_DIR, 'profile', 'package.json'), join(profileDir, 'package.json'))
  cpSync(join(PKG_DIR, 'profile', 'cordis.patch.yml'), join(profileDir, 'cordis.patch.yml'))
  cpSync(join(PKG_DIR, 'dsh-plugin-sysadmin'), join(profileDir, 'node_modules', 'dsh-plugin-sysadmin'), { recursive: true })

  const env = {
    ...process.env,
    DSH_HOME: WORK,
    DSH_TELEMETRY_DISABLED: '1',
    SYSADMIN_USER: 'sysadmin-01',
    SYSADMIN_TOKEN: 'verify-token',
    SYSADMIN_LITELLM_KEY: 'verify-key',
    SYSADMIN_AUDIT_OUTBOX: join(WORK, 'audit-outbox.jsonl'),
  }

  // 1. Static composition.
  const dump = await run(DSH_BIN, ['--profile', PROFILE, '--dump-config'], { env, cwd: PKG_DIR })
  if (dump.code !== 0) {
    record('profile composes', false, (dump.stderr || dump.stdout).trim().split('\n').slice(-3).join(' | '))
  } else {
    const text = dump.stdout
    record('profile composes', true)
    record('plugin row present', text.includes('id: sysadmin-harness') && text.includes('dsh-plugin-sysadmin'))
    record('LiteLLM route present', /apiKeyEnv: SYSADMIN_LITELLM_KEY/.test(text) && /baseURL:.*4000\/v1/.test(text))
    record('default model routed to litellm', /id: agent-default-model[\s\S]{0,200}?provider: litellm/.test(text))
  }

  // 2. Real boot.
  const port = PORT || (await freePort())
  const log = []
  const child = spawn(DSH_BIN, ['--profile', PROFILE, '--no-open', '--port', String(port)], {
    env: { ...env, SYSADMIN_HARNESS_PORT: String(port) },
    cwd: PKG_DIR,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  child.stdout?.on('data', (c) => log.push(String(c)))
  child.stderr?.on('data', (c) => log.push(String(c)))

  let booted = false
  for (let i = 0; i < 60 && !booted; i += 1) {
    await delay(500)
    booted = log.join('').includes('dsh web: http')
  }
  const output = log.join('')

  record('plugin load banner printed', output.includes('[sysadmin-harness] loaded'), output.match(/\[sysadmin-harness\][^\n]*/)?.[0] ?? 'no banner')

  let httpStatus = 0
  if (booted) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/`, { signal: AbortSignal.timeout(5000) })
      httpStatus = response.status
    } catch {
      httpStatus = 0
    }
  }
  record('web surface bound', booted, booted ? `http://127.0.0.1:${port}/` : 'no launch URL printed')
  record('unauthenticated request refused', httpStatus === 401, `HTTP ${httpStatus}`)

  // 3. Mila branding: the plugin's index tap must retitle the served page.
  // The launch URL redirects to `/` with a harness cookie; Node's fetch does
  // not keep cookies across redirects, so carry `set-cookie` over manually the
  // way a browser would.
  let indexHtml = ''
  const launchUrl = output.match(/https?:\/\/[^\s"'<>]*\?token=[A-Za-z0-9._~-]+/)?.[0]
  if (launchUrl) {
    try {
      const handoff = await fetch(launchUrl, { redirect: 'manual', signal: AbortSignal.timeout(10000) })
      const cookies = (handoff.headers.getSetCookie?.() ?? []).map((value) => value.split(';')[0]).join('; ')
      const response = await fetch(`http://127.0.0.1:${port}/`, {
        headers: cookies ? { cookie: cookies } : {},
        signal: AbortSignal.timeout(10000),
      })
      indexHtml = await response.text()
    } catch {
      /* reported as a failed check below */
    }
  }
  record(
    'Mila branding applied to the served index',
    indexHtml.includes('<title>Mila — Sysadmin AI</title>') && indexHtml.includes('/assets/mila-logo.png'),
    launchUrl ? 'title and favicon rewritten' : 'no launch URL captured from stdout',
  )

  if (!hasFlag('keep')) {
    child.kill('SIGTERM')
    await delay(1000)
    child.kill('SIGKILL')
    rmSync(WORK, { recursive: true, force: true })
  }

  const failed = results.filter((r) => !r.ok)
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`)
  if (failed.length > 0 && existsSync(join(WORK, 'profiles'))) {
    console.log(`scratch home kept at ${WORK} for inspection`)
  }
  process.exit(failed.length === 0 ? 0 : 1)
}

main().catch((error) => {
  console.error(`verification crashed: ${error?.stack ?? error}`)
  process.exit(2)
})
