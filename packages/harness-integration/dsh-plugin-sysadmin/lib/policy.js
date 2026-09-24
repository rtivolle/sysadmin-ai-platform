/**
 * Command safety policy for the sysadmin harness.
 *
 * This is a faithful JavaScript port of the platform's Python policy in
 * `backend/services/approval_gate/filter.py` so that the harness enforces the
 * SAME classification as the backend approval gate:
 *
 *   BLOCKED           -> unconditionally refused (destructive operations)
 *   ALLOW             -> simple read-only command, no shell syntax
 *   APPROVAL_REQUIRED -> mutating operation or shell expression
 *
 * Keeping one classification is the point: the harness must not become a
 * second, weaker policy engine than the backend it reports to.
 */

export const POLICY_ACTIONS = Object.freeze({
  BLOCKED: 'BLOCKED',
  ALLOW: 'ALLOW',
  APPROVAL_REQUIRED: 'APPROVAL_REQUIRED',
})

const DANGEROUS_COMMANDS = [
  /\brm\s+-rf/i,
  /\bmkfs/i,
  /\bdd\s+if=/i,
  />\s*\/dev\/(?:sd|nvme|vd|hd)/i,
  /\biptables\s+-F/i,
  /\b(?:reboot|shutdown)\b/i,
  /:\(\)\s*\{\s*:\|:&\s*\}\s*;/i,
]

const HARDENED_DANGEROUS_PATTERNS = [
  // rm -rf and recursive forced deletions (combined and split flags)
  /\brm\s+.*-(?:[a-zA-Z]*r[a-zA-Z]*f|[a-zA-Z]*f[a-zA-Z]*r)/i,
  /\brm\s+.*--recursive.*--force/i,
  /\brm\s+.*--force.*--recursive/i,

  // Filesystem formatting
  /\bmkfs(?:\.[a-z0-9]+)?\b/i,

  // Raw block device writes via dd
  /\bdd\s+.*(?:of|if)=\/dev\/(?:sd|nvme|vd|hd|mapper)/i,

  // Direct shell redirection to raw block devices
  />\s*\/dev\/(?:sd|nvme|vd|hd|mapper)/i,

  // Firewall flushing
  /\biptables\s+.*-F/i,
  /\bnft\s+flush/i,
  /\bufw\s+(?:reset|disable)/i,

  // System power state changes
  /\b(?:reboot|shutdown|poweroff|init\s+[06]|telinit\s+[06])\b/i,

  // Fork bombs (including whitespace and subshell variants)
  /:\(\)\s*\{\s*:\|:&\s*\}\s*;/i,
  /:\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;/i,
  /\w+\(\)\s*\{\s*\w+\s*\|\s*\w+\s*&\s*\}\s*;\s*\w+/i,
]

const SHELL_SYNTAX = /[;|&<>`$(){}\\\r\n]/

const READ_ONLY_COMMANDS = new Set([
  'cat', 'date', 'df', 'echo', 'free', 'head', 'id', 'ls', 'pwd',
  'tail', 'uname', 'wc', 'whoami',
])

/**
 * Split a command line into tokens approximating POSIX shell word splitting.
 * Handles single quotes, double quotes and backslash escapes; unbalanced quotes
 * degrade to whitespace splitting rather than throwing (mirroring shlex
 * failure handling in the Python policy).
 *
 * @param {string} text
 * @returns {string[]}
 */
export function tokenize(text) {
  const tokens = []
  let current = ''
  let quote = null
  let escaped = false
  let started = false

  for (const char of text) {
    if (escaped) {
      current += char
      escaped = false
      started = true
      continue
    }
    if (char === '\\' && quote !== "'") {
      escaped = true
      started = true
      continue
    }
    if (quote) {
      if (char === quote) quote = null
      else current += char
      started = true
      continue
    }
    if (char === "'" || char === '"') {
      quote = char
      started = true
      continue
    }
    if (/\s/.test(char)) {
      if (started) {
        tokens.push(current)
        current = ''
        started = false
      }
      continue
    }
    current += char
    started = true
  }

  if (escaped) current += '\\'
  if (started) tokens.push(current)
  return tokens
}

/**
 * Normalize a command to its canonical single-space form plus tokens.
 *
 * @param {string} command
 * @returns {{ canonical: string, tokens: string[] }}
 */
export function normalizeCommand(command) {
  if (typeof command !== 'string' || command.trim() === '') {
    return { canonical: '', tokens: [] }
  }
  const tokens = tokenize(command.trim())
  return { canonical: tokens.join(' '), tokens }
}

/**
 * Detect an `rm` invocation that is both recursive and forced, inspecting each
 * invocation's tokens so combined (`-rf`) and split (`-r -f`) flags are caught.
 *
 * @param {string} command
 * @returns {boolean}
 */
function hasRecursiveForcedRm(command) {
  for (const invocation of command.matchAll(/\brm\b[^;&|]*/gi)) {
    const tokens = tokenize(invocation[0])
    const flags = new Set()
    const longFlags = new Set()
    for (const token of tokens.slice(1)) {
      if (token.startsWith('--')) longFlags.add(token.toLowerCase())
      else if (token.startsWith('-')) {
        for (const letter of token.slice(1)) flags.add(letter.toLowerCase())
      }
    }
    const recursive = flags.has('r') || longFlags.has('--recursive')
    const force = flags.has('f') || longFlags.has('--force')
    if (recursive && force) return true
  }
  return false
}

/**
 * Evaluate a shell command against the platform safety policy.
 *
 * @param {string} command
 * @returns {{ action: string, reason: string, matched: string|null }}
 */
export function evaluateCommandSafety(command) {
  if (typeof command !== 'string' || command.trim() === '') {
    return {
      action: POLICY_ACTIONS.BLOCKED,
      reason: 'Security violation: empty command',
      matched: null,
    }
  }

  if (hasRecursiveForcedRm(command)) {
    return {
      action: POLICY_ACTIONS.BLOCKED,
      reason: 'Security violation: recursive forced deletion',
      matched: 'rm -r -f',
    }
  }

  for (const pattern of [...DANGEROUS_COMMANDS, ...HARDENED_DANGEROUS_PATTERNS]) {
    if (pattern.test(command)) {
      return {
        action: POLICY_ACTIONS.BLOCKED,
        reason: `Security violation: Command matched blocked pattern '${pattern.source}'`,
        matched: pattern.source,
      }
    }
  }

  if (!SHELL_SYNTAX.test(command)) {
    const argv = tokenize(command)
    if (argv.length > 0 && READ_ONLY_COMMANDS.has(argv[0])) {
      return {
        action: POLICY_ACTIONS.ALLOW,
        reason: 'Simple read-only command',
        matched: null,
      }
    }
  }

  return {
    action: POLICY_ACTIONS.APPROVAL_REQUIRED,
    reason: 'Mutating operation detected or shell expression; human approval required',
    matched: null,
  }
}

/**
 * Tool names whose arguments carry a shell command to classify.
 * @type {ReadonlySet<string>}
 */
export const SHELL_TOOL_NAMES = Object.freeze(
  new Set(['bash', 'terminal', 'pwsh', 'shell', 'sandboxed_bash', 'run_command', 'run_code']),
)

/**
 * Extract a shell command string from a tool call's arguments, when present.
 *
 * @param {string} toolName
 * @param {Record<string, unknown>|undefined} args
 * @returns {string|null}
 */
export function commandFromToolCall(toolName, args) {
  if (!SHELL_TOOL_NAMES.has(toolName) || args === null || typeof args !== 'object') {
    return null
  }
  for (const key of ['command', 'cmd', 'script']) {
    const value = args[key]
    if (typeof value === 'string' && value.trim() !== '') return value
  }
  return null
}
