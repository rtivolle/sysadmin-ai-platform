/**
 * Sandbox runner and resource envelope tests.
 *
 * Tests the hardened launcher (packages/harness-integration/sandbox/dsh-runner.sh)
 * and its integration with InstanceManager:
 * - Bubblewrap command line argument construction and cgroups v2 resource envelope
 * - Validation of workspace and DSH_HOME (symlinks, non-existent, bad permissions)
 * - Dropped capabilities and read-only system mounts
 * - Fail-closed handling when runner exits 126
 * - Environment variable and working directory propagation
 */
import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
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
const REPO_ROOT = resolve(HERE, '..', '..', '..')
const RUNNER_PATH = resolve(HERE, '..', 'sandbox', 'dsh-runner.sh')

/** @type {string[]} */
const scratchDirs = []

function makeScratch() {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-sandbox-test-'))
  scratchDirs.push(dir)
  return dir
}

after(() => {
  for (const dir of scratchDirs) {
    rmSync(dir, { recursive: true, force: true })
  }
})

/**
 * Helper to run dsh-runner.sh synchronously and capture result.
 *
 * @param {string[]} args
 * @param {object} [options]
 * @returns {{ status: number|null, stdout: string, stderr: string }}
 */
function runSandbox(args, { env = {}, cwd = process.cwd() } = {}) {
  const result = spawnSync(RUNNER_PATH, args, {
    cwd,
    env: { ...process.env, ...env },
    encoding: 'utf8',
  })
  return {
    status: result.status,
    stdout: result.stdout || '',
    stderr: result.stderr || '',
  }
}

/**
 * Setup valid workspace and home directories with 0700 permissions.
 *
 * @param {string} scratch
 */
function createValidPaths(scratch) {
  const home = join(scratch, 'home')
  const workspace = join(scratch, 'ws')
  mkdirSync(home, { recursive: true, mode: 0o700 })
  chmodSync(home, 0o700)
  mkdirSync(workspace, { recursive: true, mode: 0o700 })
  chmodSync(workspace, 0o700)
  return { home, workspace }
}

test('dsh-runner.sh exits with 126 when required arguments are missing', () => {
  // No arguments
  const res1 = runSandbox([])
  assert.equal(res1.status, 126, 'must exit 126 when invoked with no arguments')
  assert.match(res1.stderr, /user ID is required/)

  // Missing home / workspace
  const res2 = runSandbox(['--user', 'sysadmin-01'])
  assert.equal(res2.status, 126, 'must exit 126 when workspace is missing')

  // Unsafe user ID
  const res3 = runSandbox(['--user', '../escaped_user', '--home', '/tmp', '--workspace', '/tmp', '--', 'true'])
  assert.equal(res3.status, 126, 'must exit 126 on invalid user ID')
  assert.match(res3.stderr, /invalid user ID/)

  // No command
  const scratch = makeScratch()
  const { home, workspace } = createValidPaths(scratch)
  const res4 = runSandbox(['--user', 'sysadmin-01', '--home', home, '--workspace', workspace])
  assert.equal(res4.status, 126, 'must exit 126 when no command is specified')
  assert.match(res4.stderr, /no command specified/)
})

test('dsh-runner.sh exits with 126 on lone flags missing their parameter', () => {
  for (const flag of ['--user', '--home', '--workspace', '--netns']) {
    const res = runSandbox([flag])
    assert.equal(res.status, 126, `lone ${flag} must exit 126`)
    assert.match(res.stderr, new RegExp(`option ${flag} requires an argument`))
  }
})

test('dsh-runner.sh rejects non-existent workspace or DSH_HOME with exit 126', () => {
  const scratch = makeScratch()
  const { home, workspace } = createValidPaths(scratch)
  const nonexistent = join(scratch, 'does-not-exist')

  // Nonexistent workspace
  const res1 = runSandbox([
    '--user', 'sysadmin-01',
    '--home', home,
    '--workspace', nonexistent,
    '--', 'echo', 'fail',
  ])
  assert.equal(res1.status, 126)
  assert.match(res1.stderr, /workspace does not exist/)

  // Nonexistent home
  const res2 = runSandbox([
    '--user', 'sysadmin-01',
    '--home', nonexistent,
    '--workspace', workspace,
    '--', 'echo', 'fail',
  ])
  assert.equal(res2.status, 126)
  assert.match(res2.stderr, /DSH_HOME does not exist/)
})

test('dsh-runner.sh rejects symlinked workspace or DSH_HOME with exit 126', () => {
  const scratch = makeScratch()
  const { home, workspace } = createValidPaths(scratch)

  const symlinkWorkspace = join(scratch, 'symlink-ws')
  symlinkSync(workspace, symlinkWorkspace)

  const symlinkHome = join(scratch, 'symlink-home')
  symlinkSync(home, symlinkHome)

  // Symlinked workspace
  const res1 = runSandbox([
    '--user', 'sysadmin-01',
    '--home', home,
    '--workspace', symlinkWorkspace,
    '--', 'echo', 'fail',
  ])
  assert.equal(res1.status, 126)
  assert.match(res1.stderr, /workspace must not be a symlink/)

  // Symlinked DSH_HOME
  const res2 = runSandbox([
    '--user', 'sysadmin-01',
    '--home', symlinkHome,
    '--workspace', workspace,
    '--', 'echo', 'fail',
  ])
  assert.equal(res2.status, 126)
  assert.match(res2.stderr, /DSH_HOME must not be a symlink/)
})

test('dsh-runner.sh rejects workspace or DSH_HOME with mode other than 0700 with exit 126', () => {
  const scratch = makeScratch()
  const { home, workspace } = createValidPaths(scratch)

  // Set workspace mode to 0755
  chmodSync(workspace, 0o755)
  const res1 = runSandbox([
    '--user', 'sysadmin-01',
    '--home', home,
    '--workspace', workspace,
    '--', 'echo', 'fail',
  ])
  assert.equal(res1.status, 126)
  assert.match(res1.stderr, /workspace must have permissions 0700/)

  // Restore workspace, set home mode to 0755
  chmodSync(workspace, 0o700)
  chmodSync(home, 0o755)
  const res2 = runSandbox([
    '--user', 'sysadmin-01',
    '--home', home,
    '--workspace', workspace,
    '--', 'echo', 'fail',
  ])
  assert.equal(res2.status, 126)
  assert.match(res2.stderr, /DSH_HOME must have permissions 0700/)
})

test('dsh-runner.sh records correct sandbox envelope with cgroup limits, mount barriers, and cap-drop ALL', () => {
  const scratch = makeScratch()
  const { home, workspace } = createValidPaths(scratch)
  const envelopeFile = join(scratch, 'envelope.json')

  const res = runSandbox(
    [
      '--user', 'sysadmin-01',
      '--home', home,
      '--workspace', workspace,
      '--', 'echo', 'hello sandbox',
    ],
    { env: { DSH_SANDBOX_ENVELOPE_OUT: envelopeFile } },
  )
  assert.equal(res.status, 0)
  assert.equal(res.stdout.trim(), 'hello sandbox')

  const envelope = JSON.parse(readFileSync(envelopeFile, 'utf8'))
  assert.equal(envelope.userId, 'sysadmin-01')
  assert.equal(envelope.home, home)
  assert.equal(envelope.workspace, workspace)
  assert.equal(envelope.mock, true)

  // Verify cgroup v2 limits
  assert.deepEqual(envelope.cgroupLimits, {
    MemoryMax: '4G',
    MemorySwapMax: '0',
    TasksMax: 128,
    CPUQuota: '200%',
  })

  // Verify bwrap confinement arguments
  const { bwrapArgs } = envelope
  assert.ok(bwrapArgs.includes('--die-with-parent'), 'bwrapArgs must include --die-with-parent')
  assert.ok(bwrapArgs.includes('--unshare-pid'), 'bwrapArgs must include --unshare-pid')
  assert.ok(bwrapArgs.includes('--unshare-ipc'), 'bwrapArgs must include --unshare-ipc')
  assert.ok(bwrapArgs.includes('--unshare-uts'), 'bwrapArgs must include --unshare-uts')
  assert.ok(bwrapArgs.includes('--unshare-cgroup-try'), 'bwrapArgs must include --unshare-cgroup-try')
  assert.ok(bwrapArgs.includes('--cap-drop') && bwrapArgs[bwrapArgs.indexOf('--cap-drop') + 1] === 'ALL', 'must drop all capabilities')
  assert.ok(bwrapArgs.includes('--proc') && bwrapArgs[bwrapArgs.indexOf('--proc') + 1] === '/proc', 'must mount fresh /proc')
  assert.ok(bwrapArgs.includes('--dev') && bwrapArgs[bwrapArgs.indexOf('--dev') + 1] === '/dev', 'must mount minimal /dev')
  assert.ok(bwrapArgs.includes('--tmpfs') && bwrapArgs[bwrapArgs.indexOf('--tmpfs') + 1] === '/tmp', 'must mount private /tmp')
  assert.ok(bwrapArgs.includes('--ro-bind') && bwrapArgs[bwrapArgs.indexOf('--ro-bind') + 1] === '/usr', 'must mount /usr read-only')
  assert.ok(bwrapArgs.includes('--unshare-net'), 'must unshare net when no netns is provided')

  // Verify read-write bind mounts strictly bounded to home and workspace
  const homeBindIdx = bwrapArgs.findIndex((arg, i) => arg === '--bind' && bwrapArgs[i + 1] === home)
  assert.ok(homeBindIdx !== -1, 'must bind user DSH_HOME read-write')
  assert.equal(bwrapArgs[homeBindIdx + 2], home)

  const wsBindIdx = bwrapArgs.findIndex((arg, i) => arg === '--bind' && bwrapArgs[i + 1] === workspace)
  assert.ok(wsBindIdx !== -1, 'must bind workspace read-write')
  assert.equal(bwrapArgs[wsBindIdx + 2], workspace)

  const chdirIdx = bwrapArgs.indexOf('--chdir')
  assert.ok(chdirIdx !== -1, 'must set --chdir')
  assert.equal(bwrapArgs[chdirIdx + 1], workspace)

  // Verify command array
  assert.deepEqual(envelope.command, ['echo', 'hello sandbox'])
})

test('dsh-runner.sh omits --unshare-net when --netns is specified', () => {
  const scratch = makeScratch()
  const { home, workspace } = createValidPaths(scratch)
  const envelopeFile = join(scratch, 'envelope-netns.json')

  const res = runSandbox(
    [
      '--user', 'sysadmin-01',
      '--home', home,
      '--workspace', workspace,
      '--netns', 'netns-dsh-sysadmin-01',
      '--', 'echo', 'netns active',
    ],
    { env: { DSH_SANDBOX_ENVELOPE_OUT: envelopeFile } },
  )
  assert.equal(res.status, 0)

  const envelope = JSON.parse(readFileSync(envelopeFile, 'utf8'))
  assert.equal(envelope.netns, 'netns-dsh-sysadmin-01')
  assert.ok(!envelope.bwrapArgs.includes('--unshare-net'), 'must NOT include --unshare-net when netns is active')
})

test('dsh-runner.sh propagates environment variables and sets working directory', () => {
  const scratch = makeScratch()
  const { home, workspace } = createValidPaths(scratch)

  const script = join(scratch, 'env-probe.cjs')
  writeFileSync(script, `
    const result = {
      cwd: process.cwd(),
      user: process.env.SYSADMIN_USER,
      token: process.env.SYSADMIN_TOKEN,
      backendUrl: process.env.SYSADMIN_BACKEND_URL,
    }
    console.log(JSON.stringify(result))
  `)
  chmodSync(script, 0o755)

  const res = runSandbox(
    [
      '--user', 'sysadmin-01',
      '--home', home,
      '--workspace', workspace,
      '--', process.execPath, script,
    ],
    {
      env: {
        SYSADMIN_USER: 'sysadmin-01',
        SYSADMIN_TOKEN: 'secret-token-xyz',
        SYSADMIN_BACKEND_URL: 'http://127.0.0.1:3080',
      },
    },
  )
  assert.equal(res.status, 0)
  const parsed = JSON.parse(res.stdout.trim())
  assert.equal(realpathSync(parsed.cwd), realpathSync(workspace), 'must execute in the user workspace directory')
  assert.equal(parsed.user, 'sysadmin-01')
  assert.equal(parsed.token, 'secret-token-xyz')
  assert.equal(parsed.backendUrl, 'http://127.0.0.1:3080')
})

test('dsh-runner.sh exits with MOCK_FAIL_CODE when simulated failure requested', () => {
  const scratch = makeScratch()
  const { home, workspace } = createValidPaths(scratch)

  const res = runSandbox(
    [
      '--user', 'sysadmin-01',
      '--home', home,
      '--workspace', workspace,
      '--', 'echo', 'should not run',
    ],
    { env: { MOCK_FAIL_CODE: '126' } },
  )
  assert.equal(res.status, 126, 'must exit with simulated fail code')
  assert.match(res.stderr, /Simulated mock failure with exit code 126/)
})

test('InstanceManager spawns instances through dsh-runner.sh', async () => {
  const scratch = makeScratch()
  const keysDir = join(scratch, 'keys')
  mkdirSync(keysDir, { recursive: true, mode: 0o700 })
  writeFileSync(join(keysDir, 'sysadmin-01.key'), 'test-bearer-token\n')

  // Fake dsh executable that listens and outputs URL
  const fakeDsh = join(scratch, 'fake-dsh.cjs')
  writeFileSync(fakeDsh, `#!/usr/bin/env node
const http = require('node:http')
const argv = process.argv.slice(2)
const port = Number(argv[argv.indexOf('--port') + 1])
http.createServer((req, res) => res.end('ok')).listen(port, '127.0.0.1', () => {
  console.log('dsh web: http://127.0.0.1:' + port + '/?token=fake-token')
})
setInterval(() => {}, 1000)
`)
  chmodSync(fakeDsh, 0o755)

  const freePort = await findFreePort('127.0.0.1', 39100, 39200)
  const stateRoot = join(scratch, 'state')
  const envelopeOut = join(scratch, 'dsh-envelope.json')

  const config = {
    ...loadConfig({}),
    dshBin: fakeDsh,
    dshRunnerBin: RUNNER_PATH,
    sandboxEnabled: true,
    stateRoot,
    dshHomeRoot: join(stateRoot, 'homes'),
    workspaceRoot: join(stateRoot, 'workspaces'),
    keysDir,
    instanceRegistryDir: join(stateRoot, 'instances'),
    instanceLogDir: join(stateRoot, 'logs'),
    instancePortStart: freePort,
    instancePortEnd: freePort,
    instanceReadyTimeoutMs: 6000,
    profileSource: resolve(HERE, '..', 'profile'),
    pluginSource: resolve(HERE, '..', 'dsh-plugin-sysadmin'),
  }

  process.env.DSH_SANDBOX_ENVELOPE_OUT = envelopeOut
  const manager = new InstanceManager({ config })
  try {
    const instance = await manager.ensure('sysadmin-01')
    assert.equal(instance.state, 'running')
    assert.equal(instance.port, freePort)

    // Verify envelope file recorded by runner
    const envelope = JSON.parse(readFileSync(envelopeOut, 'utf8'))
    assert.equal(envelope.userId, 'sysadmin-01')
    assert.equal(envelope.home, instance.home)
    assert.equal(envelope.workspace, instance.workspace)
    assert.ok(envelope.command.includes('--profile'))
    assert.ok(envelope.command.includes(String(freePort)))

    // Verify outbox file was pre-created with 0600 mode and mounted
    assert.ok(existsSync(instance.outboxPath), 'outbox file must be pre-created by InstanceManager')
    const outboxStat = statSync(instance.outboxPath)
    assert.equal(outboxStat.mode & 0o777, 0o600, 'outbox file mode must be 0600')
    assert.ok(envelope.bwrapArgs.includes(instance.outboxPath), 'outbox file must be mounted in bwrapArgs')
  } finally {
    delete process.env.DSH_SANDBOX_ENVELOPE_OUT
    await manager.stopAll()
  }
})

test('InstanceManager handles exit 126 fail-closed without retrying', async () => {
  const scratch = makeScratch()
  const keysDir = join(scratch, 'keys')
  mkdirSync(keysDir, { recursive: true, mode: 0o700 })
  writeFileSync(join(keysDir, 'sysadmin-01.key'), 'test-bearer-token\n')

  // Failing runner that simulates exit 126
  const failingRunner = join(scratch, 'failing-runner.sh')
  writeFileSync(failingRunner, `#!/usr/bin/env bash
echo "Security validation failed: cgroup limit verification error" >&2
exit 126
`)
  chmodSync(failingRunner, 0o755)

  const freePort = await findFreePort('127.0.0.1', 39250, 39350)
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
    instancePortEnd: freePort,
    instanceReadyTimeoutMs: 3000,
    restartMaxAttempts: 3,
    profileSource: resolve(HERE, '..', 'profile'),
    pluginSource: resolve(HERE, '..', 'dsh-plugin-sysadmin'),
  }

  const manager = new InstanceManager({ config })
  try {
    await assert.rejects(
      manager.ensure('sysadmin-01'),
      /exited during startup \(code=126\)/,
    )

    // Check status in manager: state must be failed, reason set, and restarts 0
    const status = manager.statusFor('sysadmin-01')
    assert.ok(status, 'failed instance record must be retained in manager')
    assert.equal(status.state, 'failed')
    assert.equal(status.failureReason, 'Sandbox limit or security validation failure (exit 126)')
    assert.equal(status.restarts, 0, 'must not schedule restarts when exit code is 126')
  } finally {
    await manager.stopAll()
  }
})
