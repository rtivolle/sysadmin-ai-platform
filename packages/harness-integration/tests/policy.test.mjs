/**
 * Command policy tests.
 *
 * The strongest check here is parity: the harness guard and the Python backend
 * approval gate must classify the same command the same way, or the harness is a
 * second, weaker policy engine.
 */
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'

import {
  POLICY_ACTIONS,
  commandFromToolCall,
  evaluateCommandSafety,
  normalizeCommand,
  tokenize,
} from '../dsh-plugin-sysadmin/lib/policy.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(HERE, '..', '..', '..')
const PYTHON = resolve(REPO_ROOT, 'backend', '.venv', 'bin', 'python3')

const CASES = [
  ['ls -la', POLICY_ACTIONS.ALLOW],
  ['cat /var/log/nginx/error.log', POLICY_ACTIONS.ALLOW],
  ['whoami', POLICY_ACTIONS.ALLOW],
  ['rm -rf /', POLICY_ACTIONS.BLOCKED],
  ['rm -r -f /var/tmp/x', POLICY_ACTIONS.BLOCKED],
  ['rm --recursive --force /var/tmp/x', POLICY_ACTIONS.BLOCKED],
  ['dd if=/dev/sda of=/dev/sdb', POLICY_ACTIONS.BLOCKED],
  ['mkfs.ext4 /dev/sda1', POLICY_ACTIONS.BLOCKED],
  ['reboot', POLICY_ACTIONS.BLOCKED],
  ['nft flush ruleset', POLICY_ACTIONS.BLOCKED],
  ['echo x > /dev/sda', POLICY_ACTIONS.BLOCKED],
  ['systemctl restart nginx', POLICY_ACTIONS.APPROVAL_REQUIRED],
  ['sed -i s/a/b/ /etc/nginx/nginx.conf', POLICY_ACTIONS.APPROVAL_REQUIRED],
  ['echo hello | grep h', POLICY_ACTIONS.APPROVAL_REQUIRED],
  ['chmod 777 /etc/shadow', POLICY_ACTIONS.APPROVAL_REQUIRED],
  ['', POLICY_ACTIONS.BLOCKED],
]

test('classifies commands into the expected policy action', () => {
  for (const [command, expected] of CASES) {
    const verdict = evaluateCommandSafety(command)
    assert.equal(verdict.action, expected, `command ${JSON.stringify(command)} -> ${verdict.action}, expected ${expected}`)
  }
})

test('never allows a destructive command that contains read-only words', () => {
  // "ls" appears, but the command is destructive: the destructive check must win.
  assert.equal(evaluateCommandSafety('ls && rm -rf /').action, POLICY_ACTIONS.BLOCKED)
})

test('tokenizer handles quoting and normalization', () => {
  assert.deepEqual(tokenize('cat "/var/log/my file.log"'), ['cat', '/var/log/my file.log'])
  assert.deepEqual(tokenize("echo 'a b' c"), ['echo', 'a b', 'c'])
  assert.equal(normalizeCommand('  ls   -la ').canonical, 'ls -la')
})

test('extracts commands only from shell-like tools', () => {
  assert.equal(commandFromToolCall('bash', { command: 'ls' }), 'ls')
  assert.equal(commandFromToolCall('terminal', { command: 'df -h' }), 'df -h')
  assert.equal(commandFromToolCall('read_file', { command: 'ls' }), null)
  assert.equal(commandFromToolCall('bash', {}), null)
  assert.equal(commandFromToolCall('bash', undefined), null)
})

test('matches the Python backend approval gate action-for-action', { skip: !existsSync(PYTHON) }, () => {
  const script = [
    'import json, sys',
    'from backend.services.approval_gate.filter import evaluate_command_safety',
    'commands = json.loads(sys.argv[1])',
    'print(json.dumps({c: evaluate_command_safety(c)["action"] for c in commands}))',
  ].join('\n')

  const raw = execFileSync(PYTHON, ['-c', script, JSON.stringify(CASES.map(([command]) => command))], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
  })
  const pythonActions = JSON.parse(raw)

  for (const [command] of CASES) {
    assert.equal(
      evaluateCommandSafety(command).action,
      pythonActions[command],
      `parity mismatch for ${JSON.stringify(command)}: js=${evaluateCommandSafety(command).action} python=${pythonActions[command]}`,
    )
  }
})
