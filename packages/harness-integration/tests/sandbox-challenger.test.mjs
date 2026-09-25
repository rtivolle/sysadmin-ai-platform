/**
 * Empirical Challenger Suite for dsh-runner.sh
 *
 * Adversarially tests:
 * 1. Path traversal and symlink attacks against workspace and DSH_HOME
 * 2. Strict 0700 permission enforcement (0755, 0777, 0750, 0707, 0770, 0600, 0000)
 * 3. File-instead-of-directory attacks
 * 4. Missing paths and missing arguments
 * 5. Adversarial and injection user IDs (command injection, path traversal, control chars, whitespace)
 * 6. Missing target commands and empty command arrays
 * 7. Unrecognized option injection
 * 8. Envelope cgroup limits (MemoryMax=4G, MemorySwapMax=0, TasksMax=128, CPUQuota=200%)
 * 9. Bubblewrap barrier enforcement (--cap-drop ALL, --ro-bind /usr /usr, --tmpfs /tmp, --unshare-net)
 * 10. Execution cwd and exit code propagation
 * 11. Empirical demonstration of discovered vulnerabilities & edge cases:
 *     - Issue 1: Fail-open unconfined execution fallback on Linux when bwrap is absent
 *     - Issue 2: Bash shift error code 1 instead of 126 on orphaned trailing flags
 *     - Issue 3: JSON envelope syntax error with multiline arguments
 */
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { after, test } from 'node:test'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const RUNNER_PATH = resolve(HERE, '..', 'sandbox', 'dsh-runner.sh')

/** @type {string[]} */
const scratchDirs = []

function makeScratch() {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-challenger-'))
  scratchDirs.push(dir)
  return dir
}

after(() => {
  for (const dir of scratchDirs) {
    rmSync(dir, { recursive: true, force: true })
  }
})

function runRunner(args, { env = {}, cwd = process.cwd() } = {}) {
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

function setupDirs(scratch) {
  const home = join(scratch, 'home')
  const ws = join(scratch, 'ws')
  mkdirSync(home, { recursive: true, mode: 0o700 })
  chmodSync(home, 0o700)
  mkdirSync(ws, { recursive: true, mode: 0o700 })
  chmodSync(ws, 0o700)
  return { home, ws }
}

test('CHALLENGE 1: Symlink attacks on workspace and DSH_HOME must fail closed (exit 126)', () => {
  const scratch = makeScratch()
  const { home, ws } = setupDirs(scratch)

  // Relative symlink to workspace
  const relWsSymlink = join(scratch, 'rel-ws-link')
  symlinkSync('ws', relWsSymlink)

  // Absolute symlink to workspace
  const absWsSymlink = join(scratch, 'abs-ws-link')
  symlinkSync(ws, absWsSymlink)

  // Relative symlink to home
  const relHomeSymlink = join(scratch, 'rel-home-link')
  symlinkSync('home', relHomeSymlink)

  // Absolute symlink to home
  const absHomeSymlink = join(scratch, 'abs-home-link')
  symlinkSync(home, absHomeSymlink)

  // Broken symlink to workspace
  const brokenWsSymlink = join(scratch, 'broken-ws-link')
  symlinkSync(join(scratch, 'non-existent-target'), brokenWsSymlink)

  // Broken symlink to home
  const brokenHomeSymlink = join(scratch, 'broken-home-link')
  symlinkSync(join(scratch, 'non-existent-target'), brokenHomeSymlink)

  const scenarios = [
    { name: 'relative workspace symlink', args: ['--user', 'sysadmin-01', '--home', home, '--workspace', relWsSymlink, '--', 'true'] },
    { name: 'absolute workspace symlink', args: ['--user', 'sysadmin-01', '--home', home, '--workspace', absWsSymlink, '--', 'true'] },
    { name: 'relative DSH_HOME symlink', args: ['--user', 'sysadmin-01', '--home', relHomeSymlink, '--workspace', ws, '--', 'true'] },
    { name: 'absolute DSH_HOME symlink', args: ['--user', 'sysadmin-01', '--home', absHomeSymlink, '--workspace', ws, '--', 'true'] },
    { name: 'broken workspace symlink', args: ['--user', 'sysadmin-01', '--home', home, '--workspace', brokenWsSymlink, '--', 'true'] },
    { name: 'broken DSH_HOME symlink', args: ['--user', 'sysadmin-01', '--home', brokenHomeSymlink, '--workspace', ws, '--', 'true'] },
  ]

  for (const s of scenarios) {
    const res = runRunner(s.args)
    assert.equal(res.status, 126, `${s.name} must exit 126, got ${res.status}`)
    assert.ok(res.stderr.length > 0, `${s.name} must emit error message to stderr`)
  }
})

test('CHALLENGE 2: Non-0700 permissions on workspace or DSH_HOME must fail closed (exit 126)', () => {
  const scratch = makeScratch()
  const { home, ws } = setupDirs(scratch)

  const badModes = [
    { mode: 0o755, label: '0755 (rwxr-xr-x)' },
    { mode: 0o777, label: '0777 (rwxrwxrwx)' },
    { mode: 0o750, label: '0750 (rwxr-x---)' },
    { mode: 0o770, label: '0770 (rwxrwx---)' },
    { mode: 0o707, label: '0707 (rwx---rwx)' },
    { mode: 0o600, label: '0600 (rw-------)' },
  ]

  // Test workspace with bad modes
  for (const { mode, label } of badModes) {
    chmodSync(ws, mode)
    const res = runRunner(['--user', 'sysadmin-01', '--home', home, '--workspace', ws, '--', 'true'])
    assert.equal(res.status, 126, `Workspace mode ${label} must exit 126, got ${res.status}`)
    assert.match(res.stderr, /workspace must have permissions 0700/)
  }
  chmodSync(ws, 0o700)

  // Test DSH_HOME with bad modes
  for (const { mode, label } of badModes) {
    chmodSync(home, mode)
    const res = runRunner(['--user', 'sysadmin-01', '--home', home, '--workspace', ws, '--', 'true'])
    assert.equal(res.status, 126, `DSH_HOME mode ${label} must exit 126, got ${res.status}`)
    assert.match(res.stderr, /DSH_HOME must have permissions 0700/)
  }
  chmodSync(home, 0o700)
})

test('CHALLENGE 3: Regular files supplied instead of directories must fail closed (exit 126)', () => {
  const scratch = makeScratch()
  const { home, ws } = setupDirs(scratch)

  const fileWs = join(scratch, 'ws-file')
  writeFileSync(fileWs, 'not a directory', { mode: 0o700 })

  const fileHome = join(scratch, 'home-file')
  writeFileSync(fileHome, 'not a directory', { mode: 0o700 })

  // Workspace is a file
  const res1 = runRunner(['--user', 'sysadmin-01', '--home', home, '--workspace', fileWs, '--', 'true'])
  assert.equal(res1.status, 126, 'Workspace as file must exit 126')
  assert.match(res1.stderr, /workspace is not a directory/)

  // DSH_HOME is a file
  const res2 = runRunner(['--user', 'sysadmin-01', '--home', fileHome, '--workspace', ws, '--', 'true'])
  assert.equal(res2.status, 126, 'DSH_HOME as file must exit 126')
  assert.match(res2.stderr, /DSH_HOME is not a directory/)
})

test('CHALLENGE 4: Non-existent paths must fail closed (exit 126)', () => {
  const scratch = makeScratch()
  const { home, ws } = setupDirs(scratch)

  const missing1 = join(scratch, 'missing-1')
  const missing2 = join(scratch, 'missing-2')

  const res1 = runRunner(['--user', 'sysadmin-01', '--home', home, '--workspace', missing1, '--', 'true'])
  assert.equal(res1.status, 126, 'Nonexistent workspace must exit 126')
  assert.match(res1.stderr, /workspace does not exist/)

  const res2 = runRunner(['--user', 'sysadmin-01', '--home', missing2, '--workspace', ws, '--', 'true'])
  assert.equal(res2.status, 126, 'Nonexistent DSH_HOME must exit 126')
  assert.match(res2.stderr, /DSH_HOME does not exist/)

  const res3 = runRunner(['--user', 'sysadmin-01', '--home', missing2, '--workspace', missing1, '--', 'true'])
  assert.equal(res3.status, 126, 'Both nonexistent must exit 126')
})

test('CHALLENGE 5: Invalid, injection, and malicious user IDs must fail closed (exit 126)', () => {
  const scratch = makeScratch()
  const { home, ws } = setupDirs(scratch)

  const adversarialUserIds = [
    { id: '../root', reason: 'path traversal' },
    { id: 'user/subdir', reason: 'subpath separator' },
    { id: 'user;reboot', reason: 'command separator semicolon' },
    { id: 'user|reboot', reason: 'pipe injection' },
    { id: 'user&reboot', reason: 'background injection' },
    { id: 'user`id`', reason: 'backtick command substitution' },
    { id: 'user$(id)', reason: 'subshell command substitution' },
    { id: 'user name', reason: 'space in user id' },
    { id: ' user', reason: 'leading space' },
    { id: 'user ', reason: 'trailing space' },
    { id: 'user\nnewline', reason: 'newline injection' },
    { id: '-flag_user', reason: 'leading dash option injection' },
    { id: '_leading_underscore', reason: 'leading underscore (not alphanumeric)' },
    { id: '.leading_dot', reason: 'leading dot' },
    { id: '@admin', reason: 'at sign' },
    { id: 'user*glob', reason: 'glob character' },
    { id: 'user!excl', reason: 'exclamation mark' },
    { id: 'a'.repeat(65), reason: 'exceeds maximum length of 64 characters' },
    { id: '', reason: 'empty user id' },
  ]

  for (const { id, reason } of adversarialUserIds) {
    const res = runRunner(['--user', id, '--home', home, '--workspace', ws, '--', 'true'])
    assert.equal(res.status, 126, `User ID '${id}' (${reason}) must exit 126, got ${res.status}`)
    assert.match(res.stderr, /user ID/)
  }

  // Also test valid IDs to ensure regex does not falsely reject legitimate users
  const validUserIds = ['sysadmin-01', 'admin', 'user.name', 'user_01', 'U123', '007agent', 'a'.repeat(64)]
  for (const id of validUserIds) {
    const envelopeFile = join(scratch, `env-${id.slice(0, 10)}.json`)
    const res = runRunner(
      ['--user', id, '--home', home, '--workspace', ws, '--', 'true'],
      { env: { DSH_SANDBOX_ENVELOPE_OUT: envelopeFile } },
    )
    assert.equal(res.status, 0, `Valid user ID '${id}' should succeed, got ${res.status}: ${res.stderr}`)
  }
})

test('CHALLENGE 6: Missing target command must fail closed (exit 126)', () => {
  const scratch = makeScratch()
  const { home, ws } = setupDirs(scratch)

  // With '--' but no command
  const res1 = runRunner(['--user', 'sysadmin-01', '--home', home, '--workspace', ws, '--'])
  assert.equal(res1.status, 126, 'Trailing -- without command must exit 126')
  assert.match(res1.stderr, /no command specified/)

  // Without '--' and no command
  const res2 = runRunner(['--user', 'sysadmin-01', '--home', home, '--workspace', ws])
  assert.equal(res2.status, 126, 'Missing command must exit 126')
  assert.match(res2.stderr, /no command specified/)
})

test('CHALLENGE 7: Unrecognized options must fail closed (exit 126)', () => {
  const scratch = makeScratch()
  const { home, ws } = setupDirs(scratch)

  const badOptions = ['--unknown-flag', '-x', '--bind', '--cap-drop', '--unshare-net', '--profile']

  for (const opt of badOptions) {
    const res = runRunner([opt, '--user', 'sysadmin-01', '--home', home, '--workspace', ws, '--', 'true'])
    assert.equal(res.status, 126, `Unrecognized option '${opt}' must exit 126, got ${res.status}`)
    assert.match(res.stderr, /unrecognized option/)
  }
})

test('CHALLENGE 8: Positional argument fallback works and validates strictly', () => {
  const scratch = makeScratch()
  const { home, ws } = setupDirs(scratch)

  // Valid positional syntax: <userId> <dshHome> <workspace> [netns] -- <cmd...>
  const envelope1 = join(scratch, 'pos-env-1.json')
  const res1 = runRunner(
    ['sysadmin-01', home, ws, '--', 'echo', 'pos-ok'],
    { env: { DSH_SANDBOX_ENVELOPE_OUT: envelope1 } },
  )
  assert.equal(res1.status, 0, `Positional syntax should succeed: ${res1.stderr}`)
  assert.equal(res1.stdout.trim(), 'pos-ok')

  const parsed1 = JSON.parse(readFileSync(envelope1, 'utf8'))
  assert.equal(parsed1.userId, 'sysadmin-01')
  assert.equal(parsed1.home, home)
  assert.equal(parsed1.workspace, ws)
  assert.ok(parsed1.bwrapArgs.includes('--unshare-net'))

  // Valid positional with netns
  const envelope2 = join(scratch, 'pos-env-2.json')
  const res2 = runRunner(
    ['sysadmin-01', home, ws, 'netns-dsh-01', '--', 'echo', 'pos-netns-ok'],
    { env: { DSH_SANDBOX_ENVELOPE_OUT: envelope2 } },
  )
  assert.equal(res2.status, 0)
  const parsed2 = JSON.parse(readFileSync(envelope2, 'utf8'))
  assert.equal(parsed2.netns, 'netns-dsh-01')
  assert.ok(!parsed2.bwrapArgs.includes('--unshare-net'))

  // Invalid positional: missing workspace
  const res3 = runRunner(['sysadmin-01', home])
  assert.equal(res3.status, 126, 'Insufficient positional arguments must exit 126')
})

test('CHALLENGE 9: Resource envelope must specify strict cgroups v2 limits and Bubblewrap barriers', () => {
  const scratch = makeScratch()
  const { home, ws } = setupDirs(scratch)
  const envelopePath = join(scratch, 'envelope-verify.json')

  const res = runRunner(
    ['--user', 'sysadmin-01', '--home', home, '--workspace', ws, '--', 'echo', 'verified'],
    { env: { DSH_SANDBOX_ENVELOPE_OUT: envelopePath } },
  )
  assert.equal(res.status, 0)

  const env = JSON.parse(readFileSync(envelopePath, 'utf8'))

  // 1. Cgroups v2 specifications
  assert.equal(env.cgroupLimits.MemoryMax, '4G', 'MemoryMax must be 4G')
  assert.equal(env.cgroupLimits.MemorySwapMax, '0', 'MemorySwapMax must be 0 (no swap leakage)')
  assert.equal(env.cgroupLimits.TasksMax, 128, 'TasksMax must be 128')
  assert.equal(env.cgroupLimits.CPUQuota, '200%', 'CPUQuota must be 200%')

  // 2. Bubblewrap arguments
  const args = env.bwrapArgs
  assert.ok(args.includes('--die-with-parent'), 'must die with parent')
  assert.ok(args.includes('--unshare-pid'), 'must unshare pid')
  assert.ok(args.includes('--unshare-ipc'), 'must unshare ipc')
  assert.ok(args.includes('--unshare-uts'), 'must unshare uts')
  assert.ok(args.includes('--unshare-cgroup-try'), 'must unshare cgroup-try')
  assert.ok(args.includes('--cap-drop'), 'must drop caps')
  assert.equal(args[args.indexOf('--cap-drop') + 1], 'ALL', 'must drop ALL capabilities')
  assert.ok(args.includes('--proc'), 'must mount /proc')
  assert.equal(args[args.indexOf('--proc') + 1], '/proc')
  assert.ok(args.includes('--dev'), 'must mount /dev')
  assert.equal(args[args.indexOf('--dev') + 1], '/dev')
  assert.ok(args.includes('--tmpfs'), 'must mount /tmp tmpfs')
  assert.equal(args[args.indexOf('--tmpfs') + 1], '/tmp')
  assert.ok(args.includes('--ro-bind'), 'must ro-bind /usr')
  assert.equal(args[args.indexOf('--ro-bind') + 1], '/usr')
  assert.equal(args[args.indexOf('--ro-bind') + 2], '/usr')

  // 3. Mount boundaries: only home and workspace are bound read-write
  const binds = []
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--bind') {
      binds.push({ src: args[i + 1], dst: args[i + 2] })
    }
  }
  assert.equal(binds.length, 2, 'only DSH_HOME and workspace must be --bind read-write')
  assert.ok(binds.some(b => b.src === home && b.dst === home), 'home must be bound read-write')
  assert.ok(binds.some(b => b.src === ws && b.dst === ws), 'workspace must be bound read-write')

  // 4. Working directory
  const chdirIdx = args.indexOf('--chdir')
  assert.ok(chdirIdx !== -1, '--chdir must be present')
  assert.equal(args[chdirIdx + 1], ws, '--chdir must target user workspace')
})

test('CHALLENGE 10: Exit code and signals propagation from executed command', () => {
  const scratch = makeScratch()
  const { home, ws } = setupDirs(scratch)

  // Exit 0
  const res0 = runRunner(['--user', 'sysadmin-01', '--home', home, '--workspace', ws, '--', 'sh', '-c', 'exit 0'])
  assert.equal(res0.status, 0)

  // Exit 42
  const res42 = runRunner(['--user', 'sysadmin-01', '--home', home, '--workspace', ws, '--', 'sh', '-c', 'exit 42'])
  assert.equal(res42.status, 42, 'Must propagate exit code 42')

  // Exit 1
  const res1 = runRunner(['--user', 'sysadmin-01', '--home', home, '--workspace', ws, '--', 'sh', '-c', 'exit 1'])
  assert.equal(res1.status, 1, 'Must propagate exit code 1')
})

test('REMEDIATION VERIFIED 1: Missing bwrap on Linux fails closed with exit 126', () => {
  const scratch = makeScratch()
  const { home, ws } = setupDirs(scratch)

  // Simulate a Linux environment (uname returning Linux)
  const fakeBin = join(scratch, 'bin')
  mkdirSync(fakeBin, { mode: 0o755 })
  const fakeUname = join(fakeBin, 'uname')
  writeFileSync(fakeUname, '#!/bin/sh\necho Linux\n', { mode: 0o755 })

  // Invoke dsh-runner.sh on simulated Linux with SYSADMIN_SANDBOX_MOCK=0 and nonexistent BWRAP_BIN
  const res = runRunner(
    [
      '--user', 'sysadmin-01',
      '--home', home,
      '--workspace', ws,
      '--', 'echo', 'FAIL_OPEN_HOST_EXECUTION_DETECTED',
    ],
    {
      env: {
        PATH: `${fakeBin}:/usr/bin:/bin`,
        BWRAP_BIN: join(fakeBin, 'nonexistent-bwrap'),
        SYSADMIN_SANDBOX_MOCK: '0',
      },
    },
  )

  // Verified: runner fails closed with 126 and command never executes unconfined
  assert.equal(
    res.status,
    126,
    'Remediation verified: runner exited 126 fail-closed instead of unconfined execution',
  )
  assert.match(
    res.stderr,
    /bwrap executable not found/,
    'Remediation verified: stderr explicitly reports missing bwrap binary',
  )
  assert.doesNotMatch(
    res.stdout,
    /FAIL_OPEN_HOST_EXECUTION_DETECTED/,
    'Remediation verified: unconfined command did NOT execute',
  )
})

test('REMEDIATION VERIFIED 2: Orphaned trailing flag causes exit 126 fail-closed', () => {
  for (const flag of ['--user', '--home', '--workspace', '--netns']) {
    const res = runRunner([flag])
    assert.equal(
      res.status,
      126,
      `Remediation verified: orphaned ${flag} exits 126 fail-closed`,
    )
    assert.match(
      res.stderr,
      new RegExp(`option ${flag} requires an argument`),
      `stderr reports missing argument for ${flag}`,
    )
  }
})

test('VULNERABILITY DEMONSTRATION 3: Unescaped control characters in arguments break mock envelope JSON', () => {
  const scratch = makeScratch()
  const { home, ws } = setupDirs(scratch)
  const envOut = join(scratch, 'env.json')

  const res = runRunner(
    [
      '--user', 'sysadmin-01',
      '--home', home,
      '--workspace', ws,
      '--', 'echo', 'arg with\nnewline',
    ],
    { env: { DSH_SANDBOX_ENVELOPE_OUT: envOut } },
  )
  assert.equal(res.status, 0)

  // Attempting to parse the resulting envelope JSON throws SyntaxError
  assert.throws(
    () => JSON.parse(readFileSync(envOut, 'utf8')),
    /Bad control character in string literal in JSON/,
    'Defect confirmed: unescaped control chars produce syntactically invalid JSON',
  )
})
