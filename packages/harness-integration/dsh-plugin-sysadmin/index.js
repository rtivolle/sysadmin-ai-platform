/**
 * dsh-plugin-sysadmin — the custom DeepSeek Harness bundle for the sysadmin platform.
 *
 * It wires the harness to the Python platform backend in three directions:
 *
 *  1. Model traffic already leaves through the profile's `litellm` route, so every
 *     completion is quota-controlled by the backend's LiteLLM gateway using the
 *     per-user virtual key (`SYSADMIN_LITELLM_KEY`).
 *  2. `sysadmin_backend_tool` forwards bounded sysadmin tool calls to the backend,
 *     where the approval gate, workspace confinement and audit live.
 *  3. A `tools/pre-execute` policy guard classifies shell commands with the SAME
 *     rules as the backend (`BLOCKED` / `ALLOW` / `APPROVAL_REQUIRED`) and a
 *     `tools/result` observer mirrors every tool outcome into the platform audit
 *     store (VictoriaLogs, with a durable local outbox).
 *
 * Nothing here talks to a cloud provider; the only egress is loopback to the
 * platform's own services.
 */
import Schema from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'

import { AuditSink, DEFAULT_VICTORIALOGS_URL } from './lib/audit.js'
import { BackendClient, BackendError } from './lib/backend.js'
import { POLICY_ACTIONS, commandFromToolCall, evaluateCommandSafety } from './lib/policy.js'

export const name = 'sysadmin-harness'

/** The tool registry must exist before this plugin registers into it. */
export const inject = ['tools']

export const Config = Schema.object({
  /** Authenticated sysadmin identity, injected per user by the harness gateway. */
  userId: Schema.string().default('unknown'),
  /** Harness/browser session identifier used for audit correlation. */
  sessionId: Schema.string().default(''),
  /** The caller's per-user bearer token for backend calls. */
  userToken: Schema.string().default(''),
  /** Agent platform (tools, approvals) base URL. */
  backendBaseUrl: Schema.string().default('http://127.0.0.1:3080'),
  /** Auth gateway base URL. */
  authUrl: Schema.string().default('http://127.0.0.1:3081'),
  /** VictoriaLogs ingest base URL. */
  victoriaLogsUrl: Schema.string().default(DEFAULT_VICTORIALOGS_URL),
  /** Absolute path for the audit spool; empty disables spooling. */
  auditOutboxPath: Schema.string().default(''),
  /** Enforce the destructive-command policy before a shell tool runs. */
  enforceCommandPolicy: Schema.boolean().default(true),
  /** Register the backend-forwarding tool. */
  backendToolsEnabled: Schema.boolean().default(true),
  /** Emit one audit record per tool outcome. */
  auditToolResults: Schema.boolean().default(true),
})

/** Tokens whose policy denial was already audited, so the result observer skips them. */
const MAX_TRACKED_TOKENS = 512

/**
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {ReturnType<typeof Config>} config
 */
export function apply(ctx, config) {
  const audit = new AuditSink({
    victoriaLogsUrl: config.victoriaLogsUrl,
    outboxPath: config.auditOutboxPath,
    onWarning: (message, error) => {
      ctx.logger?.warn?.(`[sysadmin-harness] ${message}: ${error instanceof Error ? error.message : String(error)}`)
    },
  })
  const backend = new BackendClient({
    baseUrl: config.backendBaseUrl,
    authUrl: config.authUrl,
    token: config.userToken,
  })

  /** @type {Map<string, number>} */
  const startedAt = new Map()
  const alreadyAudited = new Set()
  const alreadyAuditedQueue = []

  const baseEvent = (extra = {}) => ({
    userId: config.userId,
    sessionId: config.sessionId,
    ...extra,
  })

  const markAudited = (key) => {
    if (key === undefined || key === null || key === '') return
    alreadyAudited.add(key)
    alreadyAuditedQueue.push(key)
    while (alreadyAuditedQueue.length > MAX_TRACKED_TOKENS) {
      alreadyAudited.delete(alreadyAuditedQueue.shift())
    }
  }

  const tokenOf = (exec) => String(exec?.token ?? '')

  // ── 1. Policy guard: classify shell commands exactly like the backend ───────
  ctx.on('tools/pre-execute', async (exec, next) => {
    const token = tokenOf(exec)
    if (token) startedAt.set(token, Date.now())

    const command = commandFromToolCall(exec?.name, exec?.arguments)
    if (!config.enforceCommandPolicy || command === null) return next()

    const verdict = evaluateCommandSafety(command)

    if (verdict.action === POLICY_ACTIONS.BLOCKED) {
      markAudited(token)
      await audit.emit(baseEvent({
        action: `policy_deny:${exec.name}`,
        toolName: String(exec.name),
        command,
        exitCode: -1,
        extra: { policy: 'destructive-command-filter', reason: verdict.reason, matched: verdict.matched },
      }))
      return { kind: 'deny', reason: `[sysadmin-policy] ${verdict.reason}` }
    }

    if (verdict.action === POLICY_ACTIONS.APPROVAL_REQUIRED) {
      // Do not decide for the human: record the request and let the harness
      // approval seam (`dsh-user-approval`) ask. The backend gate stays
      // authoritative for a real execution.
      await audit.emit(baseEvent({
        action: `approval_required:${exec.name}`,
        toolName: String(exec.name),
        command,
        humanApproved: false,
        extra: { policy: 'human-in-the-loop', reason: verdict.reason },
      }))
    }

    return next()
  })

  // ── 2. Result observer: mirror every tool outcome into the audit store ─────
  if (config.auditToolResults) {
    ctx.on('tools/result', (exec, result) => {
      const token = tokenOf(exec)
      const durationMs = token && startedAt.has(token) ? Date.now() - startedAt.get(token) : 0
      if (token) startedAt.delete(token)
      if (alreadyAudited.has(token)) {
        alreadyAudited.delete(token)
        return
      }
      const isError = Boolean(result && result.isError)
      const command = commandFromToolCall(exec?.name, exec?.arguments)
      void audit.emit(baseEvent({
        action: `tool:${exec?.name ?? 'unknown'}`,
        toolName: String(exec?.name ?? 'unknown'),
        parameters: exec?.arguments && typeof exec.arguments === 'object' ? exec.arguments : {},
        command: command ?? '',
        exitCode: isError ? 1 : 0,
        durationMs,
        extra: { call_id: String(exec?.callId ?? ''), outcome: isError ? 'error' : 'success' },
      }))
    })
  }

  // ── 3. Backend tool bridge ────────────────────────────────────────────────
  if (config.backendToolsEnabled) {
    ctx.tools.register(defineTool({
      name: 'sysadmin_backend_tool',
      description: [
        'Run one bounded sysadmin tool on the platform backend so the operation is',
        'identity-checked, quota-controlled, approval-gated and audited there.',
        'Known tools: search_log_stream, config_lint_and_diff, doc_runbook_reader,',
        'sandboxed_bash. Pass the tool parameters as a JSON object string.',
      ].join(' '),
      parameters: {
        tool: { type: 'string', required: true, description: 'Backend tool name.' },
        arguments_json: {
          type: 'string',
          required: true,
          description: 'JSON object string of the parameters the backend tool expects.',
        },
      },
      output: {
        schema: { type: 'string' },
        render: (_args, value) => [{ type: 'text', text: value }],
      },
      async execute(args, exec) {
        let parsed
        try {
          parsed = args.arguments_json.trim() === '' ? {} : JSON.parse(args.arguments_json)
        } catch (error) {
          throw new Error(`arguments_json is not valid JSON: ${error instanceof Error ? error.message : String(error)}`)
        }
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
          throw new Error('arguments_json must decode to a JSON object')
        }

        const started = Date.now()
        try {
          const body = await backend.executeTool(args.tool, parsed)
          await audit.emit(baseEvent({
            action: `backend_tool:${args.tool}`,
            toolName: 'sysadmin_backend_tool',
            parameters: { tool: args.tool, arguments: parsed },
            exitCode: 0,
            durationMs: Date.now() - started,
            extra: { transport: 'backend', call_id: String(exec?.callId ?? '') },
          }))
          return JSON.stringify(body, null, 2)
        } catch (error) {
          const status = error instanceof BackendError ? error.status : 0
          await audit.emit(baseEvent({
            action: `backend_tool:${args.tool}`,
            toolName: 'sysadmin_backend_tool',
            parameters: { tool: args.tool, arguments: parsed },
            exitCode: status === 0 ? -1 : status,
            durationMs: Date.now() - started,
            extra: { transport: 'backend', outcome: 'error', message: error instanceof Error ? error.message : String(error) },
          }))
          throw error
        }
      },
    }))
  }

  const banner = `[sysadmin-harness] loaded user=${config.userId} backend=${config.backendBaseUrl} policy=${config.enforceCommandPolicy ? 'on' : 'off'}`
  ctx.logger?.info?.(banner)
  // A startup banner on stdout is deliberate: it is the evidence operators and
  // the verification script use to confirm the bundle actually loaded.
  console.log(banner)
}
