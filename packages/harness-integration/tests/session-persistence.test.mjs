/**
 * Session and instance persistence tests.
 *
 * These exercise the gateway's restart story against real temp files and real
 * child processes: browser sessions survive a gateway restart through JSONL,
 * damaged files are quarantined instead of trusted, and harness processes
 * recorded in the instance registry are re-adopted on their recorded port (or
 * dropped when the registry record lies).
 *
 * Each test gets its own `dsh-persist-*` scratch directory; every child is
 * killed in cleanup even when a test fails mid-way.
 */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import {
  chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync,
} from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { after, test } from 'node:test'

import { loadConfig } from '../gateway/config.js'
import {
  findFreePort,
  InstanceManager,
  isPortAnswering,
  processAlive,
} from '../gateway/instance-manager.js'
import { SessionStore } from '../gateway/session-store.js'

/** @type {string[]} */
const scratchDirs = []
/** @type {Set<import('node:child_process').ChildProcess>} */
const children = new Set()

function makeScratch() {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-persist-'))
  scratchDirs.push(dir)
  return dir
}

/** @param {import('node:child_process').ChildProcess} child */
function track(child) {
  children.add(child)
  child.once('exit', () => children.delete(child))
  return child
}

function delay(ms) {
  return new Promise((resolveDelay) => setTimeout(resolveDelay, ms))
}

/**
 * Poll `probe` until it returns truthy or the timeout elapses.
 *
 * @param {() => Promise<unknown>|unknown} probe
 * @param {number} [timeoutMs]
 * @param {number} [intervalMs]
 * @returns {Promise<boolean>}
 */
async function waitFor(probe, timeoutMs = 8000, intervalMs = 50) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await probe()) return true
    await delay(intervalMs)
  }
  return false
}

/** @param {import('node:child_process').ChildProcess} child @param {number} [timeoutMs] */
function waitForExit(child, timeoutMs = 5000) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve(true)
  return new Promise((resolveExit) => {
    const timer = setTimeout(() => {
      try {
        child.kill('SIGKILL')
      } catch {
        /* already gone */
      }
      resolveExit(false)
    }, timeoutMs)
    child.once('exit', () => {
      clearTimeout(timer)
      resolveExit(true)
    })
  })
}

/**
 * Fake `dsh` executable: listens on `--port` and prints a launch URL.
 *
 * @param {string} scratch
 * @returns {string} path to the executable
 */
function writeFakeDsh(scratch) {
  const path = join(scratch, 'fake-dsh.cjs')
  writeFileSync(path, `#!/usr/bin/env node
const http = require('node:http')
const argv = process.argv.slice(2)
const port = Number(argv[argv.indexOf('--port') + 1])
http.createServer((req, res) => res.end('fake harness')).listen(port, '127.0.0.1', () => {
  console.log('dsh web: http://127.0.0.1:' + port + '/?token=fake-token-1')
})
setInterval(() => {}, 1000)
`)
  chmodSync(path, 0o755)
  return path
}

/** A pid that is guaranteed to be dead on this host. */
function deadPid() {
  let pid = 999999
  while (processAlive(pid)) pid += 1
  return pid
}

/**
 * Manager config pointed entirely at a scratch tree.
 *
 * @param {string} scratch
 * @param {Record<string, unknown>} [overrides]
 */
function testManagerConfig(scratch, overrides = {}) {
  const stateRoot = join(scratch, 'state')
  return {
    ...loadConfig({}),
    stateRoot,
    dshHomeRoot: join(stateRoot, 'homes'),
    workspaceRoot: join(stateRoot, 'workspaces'),
    keysDir: join(scratch, 'keys'),
    instanceRegistryDir: join(stateRoot, 'instances'),
    instanceLogDir: join(stateRoot, 'logs'),
    instanceReadyTimeoutMs: 8000,
    restartMaxAttempts: 0,
    restartBackoffMs: 20,
    profileSource: resolve(import.meta.dirname, '..', 'profile'),
    pluginSource: resolve(import.meta.dirname, '..', 'dsh-plugin-sysadmin'),
    ...overrides,
  }
}

/**
 * @param {ReturnType<typeof testManagerConfig>} config
 * @param {string} userId
 * @param {Record<string, unknown>} record
 */
function writeRegistryEntry(config, userId, record) {
  mkdirSync(config.instanceRegistryDir, { recursive: true, mode: 0o700 })
  writeFileSync(join(config.instanceRegistryDir, `${userId}.json`), `${JSON.stringify(record, null, 2)}\n`)
}

after(async () => {
  await Promise.all([...children].map((child) => {
    if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve()
    return new Promise((resolveExit) => {
      child.once('exit', resolveExit)
      try {
        child.kill('SIGKILL')
      } catch {
        resolveExit()
      }
    })
  }))
  children.clear()
  for (const dir of scratchDirs) rmSync(dir, { recursive: true, force: true })
})

// ---------------------------------------------------------------------------
// Browser session persistence
// ---------------------------------------------------------------------------

test('session store round-trips sessions through JSONL and forgets deleted ones', () => {
  const path = join(makeScratch(), 'sessions.jsonl')
  const first = new SessionStore({ ttlMs: 60_000, path })
  const id = first.create('sysadmin-01')
  assert.ok(existsSync(path), 'create must persist the JSONL file')

  const second = new SessionStore({ ttlMs: 60_000, path })
  assert.equal(second.load(), 1)
  assert.equal(second.get(id)?.userId, 'sysadmin-01')

  assert.equal(second.delete(id), true)
  const third = new SessionStore({ ttlMs: 60_000, path })
  assert.equal(third.load(), 0)
  assert.equal(third.get(id), null)
})

test('load keeps only unexpired sessions and the next write drops expired records from disk', () => {
  const path = join(makeScratch(), 'sessions.jsonl')
  const now = Date.now()
  const expired = {
    id: 'expired-session',
    userId: 'sysadmin-01',
    createdAt: now - 120_000,
    lastSeenAt: now - 120_000,
    expiresAt: now - 60_000,
  }
  const fresh = {
    id: 'fresh-session',
    userId: 'sysadmin-02',
    createdAt: now - 1_000,
    lastSeenAt: now - 1_000,
    expiresAt: now + 60_000,
  }
  writeFileSync(path, `${JSON.stringify(expired)}\n${JSON.stringify(fresh)}\n`)

  const store = new SessionStore({ ttlMs: 60_000, path })
  assert.equal(store.load(), 1)
  assert.equal(store.get('expired-session'), null)
  assert.equal(store.get('fresh-session')?.userId, 'sysadmin-02')

  // load() prunes expired records from memory and rewrites the file
  // immediately, so expired lines do not accumulate across restarts.
  const lines = readFileSync(path, 'utf8').split('\n').filter((line) => line.trim())
  assert.equal(lines.length, 1)
  assert.equal(JSON.parse(lines[0]).id, 'fresh-session')
})

test('load quarantines a fully corrupt file and the store stays usable', () => {
  const dir = makeScratch()
  const path = join(dir, 'sessions.jsonl')
  writeFileSync(path, 'not json\n')

  const store = new SessionStore({ ttlMs: 60_000, path })
  assert.equal(store.load(), 0)

  const backups = readdirSync(dir).filter((name) => name.includes('.corrupt-') && name.endsWith('.bak'))
  assert.equal(backups.length, 1, 'the unreadable file must be kept as a .corrupt-*.bak')
  assert.equal(readFileSync(join(dir, backups[0]), 'utf8'), 'not json\n')

  const id = store.create('sysadmin-01')
  const reloaded = new SessionStore({ ttlMs: 60_000, path })
  assert.equal(reloaded.load(), 1)
  assert.equal(reloaded.get(id)?.userId, 'sysadmin-01')
})

test('load keeps valid records from a partially damaged file and rewrites it cleanly', () => {
  const path = join(makeScratch(), 'sessions.jsonl')
  const now = Date.now()
  const valid = {
    id: 'valid-session',
    userId: 'sysadmin-01',
    createdAt: now,
    lastSeenAt: now,
    expiresAt: now + 60_000,
  }
  writeFileSync(path, `${JSON.stringify(valid)}\nthis line is garbage\n`)

  const store = new SessionStore({ ttlMs: 60_000, path })
  assert.equal(store.load(), 1)
  assert.equal(store.get('valid-session')?.userId, 'sysadmin-01')

  const raw = readFileSync(path, 'utf8')
  assert.equal(raw.includes('garbage'), false)
  const lines = raw.split('\n').filter((line) => line.trim())
  assert.equal(lines.length, 1)
  assert.equal(JSON.parse(lines[0]).id, 'valid-session')
})

test('get touches lastSeenAt and flush persists it for the next process', () => {
  const path = join(makeScratch(), 'sessions.jsonl')
  let now = 1_000_000
  const store = new SessionStore({ ttlMs: 86_400_000, path, now: () => now })
  const id = store.create('sysadmin-01')

  now += 5_000
  assert.equal(store.get(id)?.lastSeenAt, 1_005_000)
  store.flush()

  const persisted = JSON.parse(readFileSync(path, 'utf8').trim())
  assert.equal(persisted.lastSeenAt, 1_005_000)

  const reloaded = new SessionStore({ ttlMs: 86_400_000, path, now: () => now })
  assert.equal(reloaded.load(), 1)
  assert.equal(reloaded.get(id)?.lastSeenAt, 1_005_000)
})

// ---------------------------------------------------------------------------
// Harness instance registry persistence
// ---------------------------------------------------------------------------

test('recover adopts a live harness process recorded in the registry', { timeout: 10_000 }, async () => {
  const scratch = makeScratch()
  const config = testManagerConfig(scratch)
  const port = await findFreePort('127.0.0.1', 39800, 39900)
  const script = `require('http').createServer((q,s)=>s.end('ok')).listen(${port},'127.0.0.1');setInterval(()=>{},1000)`
  const child = track(spawn(process.execPath, ['-e', script], { stdio: 'ignore' }))
  assert.ok(await waitFor(() => isPortAnswering('127.0.0.1', port, 500)), 'stub harness must start listening')

  writeRegistryEntry(config, 'sysadmin-01', {
    userId: 'sysadmin-01',
    port,
    pid: child.pid,
    startedAt: Date.now(),
    home: join(config.dshHomeRoot, 'sysadmin-01'),
    workspace: join(config.workspaceRoot, 'sysadmin-01'),
  })

  const manager = new InstanceManager({ config })
  try {
    const result = await manager.recover()
    assert.deepEqual(result.adopted, ['sysadmin-01'])
    assert.deepEqual(result.dropped, [])
    assert.deepEqual(result.unknownOwner, [])

    const instance = manager.get('sysadmin-01')
    assert.ok(instance, 'an adopted instance must be returned by get()')
    assert.equal(instance.port, port)
    assert.equal(instance.adopted, true)

    await manager.stopAll()
    assert.equal(await waitForExit(child), true, 'stopAll must kill the adopted process')
    assert.equal(processAlive(child.pid), false)
    assert.equal(existsSync(manager.registryPath('sysadmin-01')), false)
  } finally {
    await manager.stopAll().catch(() => {})
    try {
      child.kill('SIGKILL')
    } catch {
      /* already gone */
    }
  }
})

test('recover drops a stale registry entry with a dead pid and removes its file', async () => {
  const scratch = makeScratch()
  const config = testManagerConfig(scratch)
  const port = await findFreePort('127.0.0.1', 39901, 39949)
  writeRegistryEntry(config, 'sysadmin-02', {
    userId: 'sysadmin-02',
    port,
    pid: deadPid(),
    startedAt: Date.now(),
    home: join(config.dshHomeRoot, 'sysadmin-02'),
    workspace: join(config.workspaceRoot, 'sysadmin-02'),
  })

  const manager = new InstanceManager({ config })
  const result = await manager.recover()

  assert.deepEqual(result.dropped, ['sysadmin-02'])
  assert.deepEqual(result.unknownOwner, [])
  assert.deepEqual(result.adopted, [])
  assert.equal(manager.get('sysadmin-02'), undefined)
  assert.equal(existsSync(manager.registryPath('sysadmin-02')), false)
})

test('recover reports a port answering with a dead pid as unknown owner and leaves it alone', async () => {
  const scratch = makeScratch()
  const config = testManagerConfig(scratch)
  const server = createServer((_req, res) => res.end('still here'))
  const port = await new Promise((resolveListen) => {
    server.listen(0, '127.0.0.1', () => resolveListen(server.address().port))
  })

  try {
    writeRegistryEntry(config, 'sysadmin-03', {
      userId: 'sysadmin-03',
      port,
      pid: deadPid(),
      startedAt: Date.now(),
      home: join(config.dshHomeRoot, 'sysadmin-03'),
      workspace: join(config.workspaceRoot, 'sysadmin-03'),
    })

    const manager = new InstanceManager({ config })
    const result = await manager.recover()

    assert.deepEqual(result.unknownOwner, ['sysadmin-03'])
    assert.deepEqual(result.dropped, [])
    assert.deepEqual(result.adopted, [])
    assert.equal(manager.get('sysadmin-03'), undefined)
    assert.equal(existsSync(manager.registryPath('sysadmin-03')), false)

    // No process was killed and the existing listener is still serving.
    const response = await fetch(`http://127.0.0.1:${port}/`)
    assert.equal(await response.text(), 'still here')
  } finally {
    await new Promise((resolveClose) => server.close(resolveClose))
  }
})

test('ensure reuses the recorded free port when starting the configured dsh binary', { timeout: 10_000 }, async () => {
  const scratch = makeScratch()
  const keysDir = join(scratch, 'keys')
  mkdirSync(keysDir, { recursive: true, mode: 0o700 })
  writeFileSync(join(keysDir, 'sysadmin-01.key'), 'fake-user-token\n')

  const fakeDsh = writeFakeDsh(scratch)

  const preferred = await findFreePort('127.0.0.1', 39950, 39999)
  const config = testManagerConfig(scratch, {
    dshBin: fakeDsh,
    instancePortStart: preferred,
    instancePortEnd: preferred,
  })
  writeRegistryEntry(config, 'sysadmin-01', {
    userId: 'sysadmin-01',
    port: preferred,
    pid: deadPid(),
    startedAt: Date.now(),
    home: join(config.dshHomeRoot, 'sysadmin-01'),
    workspace: join(config.workspaceRoot, 'sysadmin-01'),
  })

  const manager = new InstanceManager({ config })
  try {
    const instance = await manager.ensure('sysadmin-01')
    assert.equal(instance.port, preferred, 'ensure must reuse the recorded port when it is still free')
    assert.equal(instance.state, 'running')
    assert.equal(await isPortAnswering('127.0.0.1', preferred), true)

    const registry = JSON.parse(readFileSync(manager.registryPath('sysadmin-01'), 'utf8'))
    assert.equal(registry.port, preferred)
    assert.equal(registry.pid, instance.pid)
  } finally {
    await manager.stopAll()
  }
})

test('restart keeps the instance port instead of falling back to the lowest free one', { timeout: 10_000 }, async () => {
  const scratch = makeScratch()
  mkdirSync(join(scratch, 'keys'), { recursive: true, mode: 0o700 })
  writeFileSync(join(scratch, 'keys', 'sysadmin-01.key'), 'fake-user-token\n')
  const fakeDsh = writeFakeDsh(scratch)

  // Two free ports where the recorded one is NOT the lowest: a restart that
  // honestly reuses the current port must keep the higher one.
  const lowest = await findFreePort('127.0.0.1', 39960, 39980)
  const recorded = await findFreePort('127.0.0.1', lowest + 1, 39999)
  const config = testManagerConfig(scratch, {
    dshBin: fakeDsh,
    instancePortStart: lowest,
    instancePortEnd: recorded,
  })
  writeRegistryEntry(config, 'sysadmin-01', {
    userId: 'sysadmin-01',
    port: recorded,
    pid: deadPid(),
    startedAt: Date.now(),
    home: join(config.dshHomeRoot, 'sysadmin-01'),
    workspace: join(config.workspaceRoot, 'sysadmin-01'),
  })

  const manager = new InstanceManager({ config })
  try {
    const first = await manager.ensure('sysadmin-01')
    assert.equal(first.port, recorded, 'the recorded port must win over the lowest free port')

    const restarted = await manager.restart('sysadmin-01')
    assert.equal(restarted.port, recorded, 'restart must keep the port when it is still free')
    assert.notEqual(restarted.pid, first.pid, 'restart must be a new process')
  } finally {
    await manager.stopAll()
  }
})
