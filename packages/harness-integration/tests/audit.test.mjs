/**
 * Audit sink tests: the schema must match the backend's, delivery must fall back
 * to a durable spool, and a spool write failure must never throw into a tool turn.
 */
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { after, test } from 'node:test'

import { AuditSink, buildAuditEvent } from '../dsh-plugin-sysadmin/lib/audit.js'

const scratch = mkdtempSync(join(tmpdir(), 'dsh-audit-'))
after(() => rmSync(scratch, { recursive: true, force: true }))

test('builds an event with the backend audit schema', () => {
  const event = buildAuditEvent({
    userId: 'sysadmin-01',
    sessionId: 'sess-1',
    action: 'tool:bash',
    toolName: 'bash',
    parameters: { command: 'ls' },
    exitCode: 0,
    durationMs: 12,
  })

  for (const field of [
    'event_id', 'timestamp', 'service', 'user_id', 'session_id', 'action', 'tool_name',
    'parameters', 'command', 'human_approved', 'approval_id', 'exit_code', 'duration_ms',
    'priority', 'incident_id', 'prompt_tokens', 'completion_tokens', 'tokens_prompt', 'tokens_completion',
  ]) {
    assert.ok(field in event, `missing field ${field}`)
  }
  assert.equal(event.service, 'dsh-agent')
  assert.equal(event.user_id, 'sysadmin-01')
  assert.match(event.timestamp, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/)
})

test('truncates oversized proposed_content like the backend', () => {
  const big = 'x'.repeat(900)
  const event = buildAuditEvent({
    action: 'config_lint_and_diff',
    parameters: { proposed_content: big },
  })
  assert.ok(event.parameters.proposed_content.length < big.length)
  assert.match(event.parameters.proposed_content, /\[truncated 900 bytes\]$/)
})

test('posts to the VictoriaLogs jsonline endpoint on success', async () => {
  const calls = []
  const sink = new AuditSink({
    victoriaLogsUrl: 'http://127.0.0.1:9428/',
    outboxPath: join(scratch, 'ok-outbox.jsonl'),
    fetchImpl: async (url, init) => {
      calls.push({ url, init })
      return { ok: true, status: 200 }
    },
  })

  const result = await sink.emit({ userId: 'sysadmin-02', action: 'test' })
  assert.equal(result.logged, true)
  assert.equal(result.destination, 'victorialogs')
  assert.equal(calls.length, 1)
  assert.match(calls[0].url, /\/insert\/jsonline\?_stream_fields=service,user_id,priority&_time_field=timestamp$/)
  assert.match(String(calls[0].init.body), /"user_id":"sysadmin-02"/)
})

test('spools to the outbox when the collector is unreachable', async () => {
  const outbox = join(scratch, 'fallback-outbox.jsonl')
  const sink = new AuditSink({
    outboxPath: outbox,
    fetchImpl: async () => {
      throw new Error('collector down')
    },
  })

  const result = await sink.emit({ userId: 'sysadmin-03', action: 'fallback' })
  assert.equal(result.destination, 'outbox')

  const lines = readFileSync(outbox, 'utf8').trim().split('\n')
  assert.equal(lines.length, 1)
  const parsed = JSON.parse(lines[0])
  assert.equal(parsed.user_id, 'sysadmin-03')
  assert.equal(parsed.action, 'fallback')
})

test('spools when the collector returns a non-ok status', async () => {
  const outbox = join(scratch, 'status-outbox.jsonl')
  const sink = new AuditSink({
    outboxPath: outbox,
    fetchImpl: async () => ({ ok: false, status: 503 }),
  })
  const result = await sink.emit({ userId: 'sysadmin-04', action: 'degraded' })
  assert.equal(result.destination, 'outbox')
})

test('never throws when both delivery paths fail', async () => {
  // Occupy the outbox's parent path with a regular file so the spool write fails.
  const blocker = join(scratch, 'blocker')
  writeFileSync(blocker, 'not a directory')
  const sink = new AuditSink({
    outboxPath: join(blocker, 'outbox.jsonl'),
    fetchImpl: async () => ({ ok: false, status: 500 }),
  })
  const result = await sink.emit({ userId: 'sysadmin-05', action: 'doomed' })
  assert.equal(result.logged, false)
  assert.equal(result.destination, 'dropped')
})
