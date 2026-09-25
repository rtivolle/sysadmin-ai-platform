/**
 * Empirical challenger test suite for Milestone M1:
 * - Fail-closed exit 126 handling during spawn and mid-flight
 * - Absence of retry loops when exit code is 126
 * - Retention of failed state in status queries (statusFor and listStatus)
 * - Concurrent multi-user instance spawning with sandbox enabled
 * - Workspace and DSH_HOME isolation with 0700 POSIX permissions
 * - Environment variable propagation into sandboxed processes
 * - Coalescence of concurrent calls for identical users
 */
import assert from 'node:assert/strict'
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { after, test } from 'node:test'
import { fileURLToPath } from 'node:url'

import { loadConfig } from '../gateway/config.js'
import {
  findFreePort,
  HarnessInstance,
  InstanceManager,
} from '../gateway/instance-manager.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const RUNNER_PATH = resolve(HERE, '..', 'sandbox', 'dsh-runner.sh')

/** @type {string[]} */
const scratchDirs = []

function makeScratch() {
  const dir = mkdtempSync(join(tmpdir(), 'challenger-m1-'))
  scratchDirs.push(dir)
  return dir
}

after(() => {
  for (const dir of scratchDirs) {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('Empirical Challenge 1A: exit 126 during startup marks instance failed without retry loop', async () => {
  const scratch = makeScratch()
  const keysDir = join(scratch, 'keys')
  mkdirSync(keysDir, { recursive: true, mode: 0o700 })
  writeFileSync(join(keysDir, 'user-fail.key'), 'token-fail-126\n')

  const failingRunner = join(scratch, 'fail-runner.sh')
  writeFileSync(failingRunner, `#!/usr/bin/env bash
echo "Cgroup v2 envelope could not be enforced" >&2
exit 126
`)
  chmodSync(failingRunner, 0o755)

  const freePort = await findFreePort('127.0.0.1', 38100, 38200)
  const stateRoot = join(scratch, 'state')

  const config = {
    ...loadConfig({}),
    dshBin: 'dsh',
    dshRunnerBin: failingRunner,
    sandboxEnabled: true,
    stateRoot,
    dshHomeRoot: join(stateRoot, 'homes'),
    workspaceRoot: join(stateRoot, 'workspaces'),
    keysDir,
    instanceRegistryDir: join(stateRoot, 'instances'),
    instanceLogDir: join(stateRoot, 'logs'),
    instancePortStart: freePort,
    instancePortEnd: freePort + 10,
    instanceReadyTimeoutMs: 2000,
    restartMaxAttempts: 5,
    restartBackoffMs: 100,
    profileSource: resolve(HERE, '..', 'profile'),
    pluginSource: resolve(HERE, '..', 'dsh-plugin-sysadmin'),
  }

  const manager = new InstanceManager({ config })
  try {
    await assert.rejects(
      manager.ensure('user-fail'),
      /exited during startup \(code=126\)/,
      'ensure must reject when runner exits 126',
    )

    // Verify state in manager
    const status = manager.statusFor('user-fail')
    assert.ok(status, 'failed instance record must be retained in manager')
    assert.equal(status.state, 'failed', 'state must be failed')
    assert.equal(
      status.failureReason,
      'Sandbox limit or security validation failure (exit 126)',
      'failureReason must record sandbox failure',
    )
    assert.equal(status.restarts, 0, 'restarts must be 0')

    // Verify listStatus
    const all = manager.listStatus()
    const userEntry = all.find((entry) => entry.userId === 'user-fail')
    assert.ok(userEntry, 'listStatus must contain entry for user-fail')
    assert.equal(userEntry.state, 'failed')

    // Wait past restart backoff duration to empirically prove no retry timer fires
    await new Promise((r) => setTimeout(r, 400))
    const statusAfter = manager.statusFor('user-fail')
    assert.equal(statusAfter.state, 'failed')
    assert.equal(statusAfter.restarts, 0, 'no restart loop occurred')
  } finally {
    await manager.stopAll()
  }
})

test('Empirical Challenge 1B: mid-flight exit 126 transitions running instance to failed without retry loop', async () => {
  const scratch = makeScratch()
  const keysDir = join(scratch, 'keys')
  mkdirSync(keysDir, { recursive: true, mode: 0o700 })
  writeFileSync(join(keysDir, 'user-midflight.key'), 'token-midflight\n')

  const mockDsh = join(scratch, 'mock-dsh.cjs')
  writeFileSync(mockDsh, `#!/usr/bin/env node
const http = require('node:http')
const argv = process.argv.slice(2)
const port = Number(argv[argv.indexOf('--port') + 1])
http.createServer((req, res) => res.end('ok')).listen(port, '127.0.0.1', () => {
  console.log('dsh web: http://127.0.0.1:' + port + '/?token=token-midflight')
})
setInterval(() => {}, 1000)
`)
  chmodSync(mockDsh, 0o755)

  const passthroughRunner = join(scratch, 'passthrough-runner.sh')
  writeFileSync(passthroughRunner, `#!/usr/bin/env bash
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--" ]; then shift; break; fi
  shift
done
exec "$@"
`)
  chmodSync(passthroughRunner, 0o755)

  const freePort = await findFreePort('127.0.0.1', 38210, 38300)
  const stateRoot = join(scratch, 'state')

  const config = {
    ...loadConfig({}),
    dshBin: mockDsh,
    dshRunnerBin: passthroughRunner,
    sandboxEnabled: true,
    stateRoot,
    dshHomeRoot: join(stateRoot, 'homes'),
    workspaceRoot: join(stateRoot, 'workspaces'),
    keysDir,
    instanceRegistryDir: join(stateRoot, 'instances'),
    instanceLogDir: join(stateRoot, 'logs'),
    instancePortStart: freePort,
    instancePortEnd: freePort + 10,
    instanceReadyTimeoutMs: 3000,
    restartMaxAttempts: 5,
    restartBackoffMs: 100,
    profileSource: resolve(HERE, '..', 'profile'),
    pluginSource: resolve(HERE, '..', 'dsh-plugin-sysadmin'),
  }

  const manager = new InstanceManager({ config })
  try {
    const instance = await manager.ensure('user-midflight')
    assert.equal(instance.state, 'running')
    assert.ok(instance.running)

    // Simulate process terminating with exit 126 while running
    instance.child.emit('exit', 126, null)

    assert.equal(instance.state, 'failed')
    assert.equal(
      instance.failureReason,
      'Sandbox limit or security validation failure (exit 126)',
    )
    assert.equal(instance.restartTimer, null, 'restartTimer must be null on exit 126')

    // Wait past backoff to verify no restart occurs
    await new Promise((r) => setTimeout(r, 300))
    const status = manager.statusFor('user-midflight')
    assert.equal(status.state, 'failed')
    assert.equal(status.restarts, 0, 'must not restart after mid-flight exit 126')
  } finally {
    await manager.stopAll()
  }
})

test('Empirical Challenge 1C: workspace symlink triggers exit 126 and fails closed', async () => {
  const scratch = makeScratch()
  const keysDir = join(scratch, 'keys')
  mkdirSync(keysDir, { recursive: true, mode: 0o700 })
  writeFileSync(join(keysDir, 'user-symlink.key'), 'token-symlink\n')

  const freePort = await findFreePort('127.0.0.1', 38310, 38400)
  const stateRoot = join(scratch, 'state')
  const workspaceRoot = join(stateRoot, 'workspaces')
  mkdirSync(workspaceRoot, { recursive: true, mode: 0o700 })

  // Plant a symlink as the user's workspace
  const realTarget = join(scratch, 'real-target')
  mkdirSync(realTarget, { recursive: true, mode: 0o700 })
  symlinkSync(realTarget, join(workspaceRoot, 'user-symlink'))

  const config = {
    ...loadConfig({}),
    dshBin: 'dsh',
    dshRunnerBin: RUNNER_PATH,
    sandboxEnabled: true,
    stateRoot,
    dshHomeRoot: join(stateRoot, 'homes'),
    workspaceRoot,
    keysDir,
    instanceRegistryDir: join(stateRoot, 'instances'),
    instanceLogDir: join(stateRoot, 'logs'),
    instancePortStart: freePort,
    instancePortEnd: freePort + 10,
    instanceReadyTimeoutMs: 2000,
    profileSource: resolve(HERE, '..', 'profile'),
    pluginSource: resolve(HERE, '..', 'dsh-plugin-sysadmin'),
  }

  const manager = new InstanceManager({ config })
  try {
    await assert.rejects(
      manager.ensure('user-symlink'),
      /exited during startup \(code=126\)/,
      'symlinked workspace must trigger exit 126 fail-closed',
    )
    const status = manager.statusFor('user-symlink')
    assert.equal(status.state, 'failed')
    assert.equal(status.failureReason, 'Sandbox limit or security validation failure (exit 126)')
    assert.equal(status.restarts, 0)
  } finally {
    await manager.stopAll()
  }
})

test('Empirical Challenge 2: concurrent multi-user spawning, workspace isolation, and env propagation', async () => {
  const scratch = makeScratch()
  const keysDir = join(scratch, 'keys')
  mkdirSync(keysDir, { recursive: true, mode: 0o700 })

  const userCount = 4
  const users = Array.from({ length: userCount }, (_, i) => `sysadmin-0${i + 1}`)
  for (const u of users) {
    writeFileSync(join(keysDir, `${u}.key`), `secret-token-${u}\n`)
  }

  // Probe executable that records environment and launches HTTP listener immediately
  const probeDsh = join(scratch, 'probe-dsh.cjs')
  writeFileSync(probeDsh, `#!/usr/bin/env node
const http = require('node:http')
const fs = require('node:fs')
const path = require('node:path')

const argv = process.argv.slice(2)
const port = Number(argv[argv.indexOf('--port') + 1])

const record = {
  cwd: process.cwd(),
  user: process.env.SYSADMIN_USER,
  token: process.env.SYSADMIN_TOKEN,
  dshHome: process.env.DSH_HOME,
  litellmUrl: process.env.SYSADMIN_LITELLM_URL,
  backendUrl: process.env.SYSADMIN_BACKEND_URL,
  port,
}
fs.writeFileSync(path.join(process.cwd(), 'env-record.json'), JSON.stringify(record, null, 2))

http.createServer((req, res) => res.end('ok')).listen(port, '127.0.0.1', () => {
  console.log('dsh web: http://127.0.0.1:' + port + '/?token=secret-token-' + process.env.SYSADMIN_USER)
})
setInterval(() => {}, 1000)
`)
  chmodSync(probeDsh, 0o755)

  const freePort = await findFreePort('127.0.0.1', 38500, 38650)
  const stateRoot = join(scratch, 'state')

  const config = {
    ...loadConfig({}),
    dshBin: probeDsh,
    dshRunnerBin: RUNNER_PATH,
    sandboxEnabled: true,
    stateRoot,
    dshHomeRoot: join(stateRoot, 'homes'),
    workspaceRoot: join(stateRoot, 'workspaces'),
    keysDir,
    instanceRegistryDir: join(stateRoot, 'instances'),
    instanceLogDir: join(stateRoot, 'logs'),
    instancePortStart: freePort,
    instancePortEnd: freePort + 50,
    instanceReadyTimeoutMs: 8000,
    profileSource: resolve(HERE, '..', 'profile'),
    pluginSource: resolve(HERE, '..', 'dsh-plugin-sysadmin'),
  }

  const manager = new InstanceManager({ config })
  try {
    // Spawn all users concurrently
    const instances = await Promise.all(users.map((u) => manager.ensure(u)))

    const allocatedPorts = new Set()
    const workspacePaths = new Set()
    const homePaths = new Set()

    for (const inst of instances) {
      assert.equal(inst.state, 'running')
      assert.ok(inst.running)

      // Port uniqueness
      assert.ok(!allocatedPorts.has(inst.port), `port ${inst.port} must be unique across instances`)
      allocatedPorts.add(inst.port)

      // Workspace uniqueness & 0700 permissions
      assert.ok(!workspacePaths.has(inst.workspace), `workspace ${inst.workspace} must be unique`)
      workspacePaths.add(inst.workspace)
      const wsMode = statSync(inst.workspace).mode & 0o777
      assert.equal(wsMode, 0o700, `workspace ${inst.workspace} must be mode 0700`)

      // Home uniqueness & 0700 permissions
      assert.ok(!homePaths.has(inst.home), `home ${inst.home} must be unique`)
      homePaths.add(inst.home)
      const homeMode = statSync(inst.home).mode & 0o777
      assert.equal(homeMode, 0o700, `home ${inst.home} must be mode 0700`)

      // Environment variable and cwd verification inside child process
      const record = JSON.parse(readFileSync(join(inst.workspace, 'env-record.json'), 'utf8'))
      assert.equal(record.user, inst.userId)
      assert.equal(record.token, `secret-token-${inst.userId}`)
      assert.equal(record.dshHome, inst.home)
      assert.equal(record.litellmUrl, config.litellmUrl)
      assert.equal(record.backendUrl, config.agentUrl)
      assert.equal(record.port, inst.port)

      // Envelope verification from runner
      const envelope = JSON.parse(readFileSync(`/tmp/dsh-sandbox-envelope-${inst.userId}.json`, 'utf8'))
      assert.equal(envelope.userId, inst.userId)
      assert.equal(envelope.home, inst.home)
      assert.equal(envelope.workspace, inst.workspace)
      assert.deepEqual(envelope.cgroupLimits, {
        MemoryMax: '4G',
        MemorySwapMax: '0',
        TasksMax: 128,
        CPUQuota: '200%',
      })
      assert.ok(envelope.bwrapArgs.includes('--cap-drop'))
      assert.ok(envelope.bwrapArgs.includes('ALL'))
      assert.ok(envelope.bwrapArgs.includes('--ro-bind'))
      assert.ok(envelope.bwrapArgs.includes('/usr'))
    }
  } finally {
    await manager.stopAll()
  }
})

test('Empirical Challenge 3: concurrent ensure calls for identical user coalesce into single instance', async () => {
  const scratch = makeScratch()
  const keysDir = join(scratch, 'keys')
  mkdirSync(keysDir, { recursive: true, mode: 0o700 })
  writeFileSync(join(keysDir, 'user-coalesce.key'), 'token-coalesce\n')

  const dummyDsh = join(scratch, 'dummy-dsh.cjs')
  writeFileSync(dummyDsh, `#!/usr/bin/env node
const http = require('node:http')
const argv = process.argv.slice(2)
const port = Number(argv[argv.indexOf('--port') + 1])
http.createServer((req, res) => res.end('ok')).listen(port, '127.0.0.1', () => {
  console.log('dsh web: http://127.0.0.1:' + port + '/?token=token-coalesce')
})
setInterval(() => {}, 1000)
`)
  chmodSync(dummyDsh, 0o755)

  const freePort = await findFreePort('127.0.0.1', 38700, 38800)
  const stateRoot = join(scratch, 'state')

  const config = {
    ...loadConfig({}),
    dshBin: dummyDsh,
    dshRunnerBin: RUNNER_PATH,
    sandboxEnabled: true,
    stateRoot,
    dshHomeRoot: join(stateRoot, 'homes'),
    workspaceRoot: join(stateRoot, 'workspaces'),
    keysDir,
    instanceRegistryDir: join(stateRoot, 'instances'),
    instanceLogDir: join(stateRoot, 'logs'),
    instancePortStart: freePort,
    instancePortEnd: freePort + 10,
    instanceReadyTimeoutMs: 3000,
    profileSource: resolve(HERE, '..', 'profile'),
    pluginSource: resolve(HERE, '..', 'dsh-plugin-sysadmin'),
  }

  const manager = new InstanceManager({ config })
  try {
    const [inst1, inst2, inst3] = await Promise.all([
      manager.ensure('user-coalesce'),
      manager.ensure('user-coalesce'),
      manager.ensure('user-coalesce'),
    ])
    assert.equal(inst1, inst2, 'instances must refer to exact same object')
    assert.equal(inst2, inst3, 'instances must refer to exact same object')
    assert.equal(inst1.state, 'running')
  } finally {
    await manager.stopAll()
  }
})
