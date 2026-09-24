/**
 * Audit sink for the sysadmin harness.
 *
 * Writes the SAME event schema as the Python backend
 * (`backend/services/agent_tools/audit.py::log_audit_event`) so harness-originated
 * activity lands in the platform's VictoriaLogs audit store alongside backend
 * events and can be correlated by `user_id` / `session_id` / `event_id`.
 *
 * Delivery is at-least-once: an event that VictoriaLogs accepted but that was not
 * yet removed from the local outbox may be replayed after a crash. `event_id`
 * exists so consumers can deduplicate.
 */
import { randomUUID } from 'node:crypto'
import { closeSync, existsSync, mkdirSync, openSync, fsyncSync, writeSync } from 'node:fs'
import { dirname } from 'node:path'

export const DEFAULT_VICTORIALOGS_URL = 'http://127.0.0.1:9428'

/**
 * Build an audit event using the backend's field set.
 *
 * @param {object} input
 * @returns {Record<string, unknown>}
 */
export function buildAuditEvent(input) {
  const {
    userId = 'unknown',
    sessionId = '',
    action,
    toolName = action,
    parameters = {},
    command = '',
    humanApproved = false,
    approvalId = null,
    exitCode = 0,
    durationMs = 0,
    promptTokens = 0,
    completionTokens = 0,
    priority = 'standard',
    incidentId = null,
    service = 'dsh-agent',
    extra = undefined,
  } = input

  const event = {
    event_id: randomUUID(),
    timestamp: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'),
    service,
    user_id: userId,
    session_id: sessionId,
    action,
    tool_name: toolName,
    parameters: sanitizeParameters(parameters),
    command,
    human_approved: humanApproved,
    approval_id: approvalId,
    exit_code: exitCode,
    duration_ms: durationMs,
    priority,
    incident_id: incidentId,
    prompt_tokens: promptTokens,
    completion_tokens: completionTokens,
    tokens_prompt: promptTokens,
    tokens_completion: completionTokens,
  }
  if (extra !== undefined) event.extra = extra
  return event
}

/**
 * Bound the one field the backend truncates, so a large staged file cannot
 * bloat the audit store through the harness.
 *
 * @param {unknown} parameters
 * @returns {Record<string, unknown>}
 */
export function sanitizeParameters(parameters) {
  const safe = parameters !== null && typeof parameters === 'object' ? { ...parameters } : {}
  if (typeof safe.proposed_content === 'string' && safe.proposed_content.length > 500) {
    safe.proposed_content = `${safe.proposed_content.slice(0, 500)}... [truncated ${safe.proposed_content.length} bytes]`
  }
  return safe
}

export class AuditSink {
  /**
   * @param {object} options
   * @param {string} [options.victoriaLogsUrl]
   * @param {string} [options.outboxPath] absolute path; empty disables spooling
   * @param {typeof fetch} [options.fetchImpl]
   * @param {(message: string, error?: unknown) => void} [options.onWarning]
   */
  constructor({
    victoriaLogsUrl = DEFAULT_VICTORIALOGS_URL,
    outboxPath = '',
    fetchImpl = globalThis.fetch,
    onWarning = () => {},
  } = {}) {
    this.victoriaLogsUrl = victoriaLogsUrl.replace(/\/+$/, '')
    this.outboxPath = outboxPath
    this.fetch = fetchImpl
    this.onWarning = onWarning
  }

  /** @returns {string} the VictoriaLogs ingest URL for one JSON line */
  ingestUrl() {
    return `${this.victoriaLogsUrl}/insert/jsonline?_stream_fields=service,user_id,priority&_time_field=timestamp`
  }

  /**
   * Emit one audit event. Never throws: audit must not break a tool turn.
   *
   * @param {object} input see {@link buildAuditEvent}
   * @returns {Promise<{ logged: boolean, destination: string, event_id: string }>}
   */
  async emit(input) {
    const event = buildAuditEvent(input)
    try {
      if (await this.#post(event)) {
        return { logged: true, destination: 'victorialogs', event_id: event.event_id }
      }
    } catch (error) {
      this.onWarning('victorialogs ingest failed', error)
    }
    try {
      this.appendOutbox(event)
      return { logged: true, destination: 'outbox', event_id: event.event_id }
    } catch (error) {
      this.onWarning('audit outbox write failed', error)
      return { logged: false, destination: 'dropped', event_id: event.event_id }
    }
  }

  /** @returns {Promise<boolean>} */
  async #post(event) {
    const response = await this.fetch(this.ingestUrl(), {
      method: 'POST',
      headers: { 'content-type': 'application/stream+json' },
      body: `${JSON.stringify(event)}\n`,
      signal: AbortSignal.timeout(5000),
    })
    return response.ok
  }

  /**
   * Append a record to the spool and fsync it, so a crash after acceptance but
   * before delivery still has a durable copy.
   *
   * @param {Record<string, unknown>} event
   */
  appendOutbox(event) {
    if (!this.outboxPath) throw new Error('audit outbox path is not configured')
    mkdirSync(dirname(this.outboxPath), { recursive: true })
    const line = `${JSON.stringify(event)}\n`
    const fd = openSync(this.outboxPath, 'a', 0o600)
    try {
      writeSync(fd, line)
      fsyncSync(fd)
    } finally {
      closeSync(fd)
    }
  }

  /** @returns {boolean} whether a spool file currently holds records */
  hasSpooledRecords() {
    return Boolean(this.outboxPath) && existsSync(this.outboxPath)
  }
}
