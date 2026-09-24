/**
 * Gateway session store.
 *
 * A gateway session is the browser's proof of a successful login against the
 * platform auth gateway. It maps an opaque cookie value to the authenticated
 * sysadmin identity, a creation time and an expiry; it deliberately holds no
 * credential material.
 *
 * The in-memory map is the source of truth while the process lives. When a
 * `path` is configured, every mutation is persisted as JSONL (one record per
 * line, temp file + fsync + rename, 0600) and the file is loaded and pruned at
 * boot, so browser sessions survive a gateway restart. `lastSeenAt` is touched
 * on every lookup and flushed on a short debounce instead of once per request.
 *
 * With no `path` configured the store is memory-only, which keeps unit tests
 * and scratch runs free of disk state.
 */
import { randomBytes, timingSafeEqual } from 'node:crypto'
import { existsSync, readFileSync, renameSync } from 'node:fs'
import { writeFileAtomic } from './state-file.js'

const LAST_SEEN_FLUSH_MS = 2000

export class SessionStore {
  /**
   * @param {object} options
   * @param {number} options.ttlMs absolute session lifetime
   * @param {() => number} [options.now]
   * @param {string|null} [options.path] JSONL persistence path; null = memory only
   * @param {(message: string) => void} [options.logger]
   */
  constructor({ ttlMs, now = () => Date.now(), path = null, logger = () => {} }) {
    this.ttlMs = ttlMs
    this.now = now
    this.path = path || null
    this.log = logger
    /** @type {Map<string, { userId: string, createdAt: number, lastSeenAt: number, expiresAt: number }>} */
    this.sessions = new Map()
    this.dirty = false
    this.flushTimer = null
  }

  /**
   * @param {string} userId
   * @returns {string} the opaque session id
   */
  create(userId) {
    const sessionId = randomBytes(32).toString('base64url')
    const createdAt = this.now()
    this.sessions.set(sessionId, {
      userId,
      createdAt,
      lastSeenAt: createdAt,
      expiresAt: createdAt + this.ttlMs,
    })
    this.persistNow()
    return sessionId
  }

  /**
   * Resolve a session id, touching `lastSeenAt` and enforcing expiry.
   *
   * @param {string|undefined} sessionId
   * @returns {{ userId: string, createdAt: number, lastSeenAt: number, expiresAt: number }|null}
   */
  get(sessionId) {
    if (!sessionId) return null
    const record = this.sessions.get(sessionId)
    if (!record) return null
    const now = this.now()
    if (record.expiresAt <= now) {
      this.sessions.delete(sessionId)
      this.persistNow()
      return null
    }
    record.lastSeenAt = now
    this.scheduleFlush()
    return record
  }

  /** @param {string|undefined} sessionId */
  delete(sessionId) {
    if (!sessionId) return false
    const removed = this.sessions.delete(sessionId)
    if (removed) this.persistNow()
    return removed
  }

  /** Remove every expired session; returns the number dropped. */
  prune() {
    const now = this.now()
    let dropped = 0
    for (const [id, record] of this.sessions) {
      if (record.expiresAt <= now) {
        this.sessions.delete(id)
        dropped += 1
      }
    }
    if (dropped > 0) this.persistNow()
    return dropped
  }

  /** True when at least one non-expired session belongs to `userId`. */
  hasUser(userId) {
    for (const record of this.sessions.values()) {
      if (record.userId === userId && record.expiresAt > this.now()) return true
    }
    return false
  }

  /**
   * All live sessions, oldest first. Callers must not log the raw `id`:
   * it is the browser's bearer cookie.
   *
   * @returns {Array<{ id: string, userId: string, createdAt: number, lastSeenAt: number, expiresAt: number }>}
   */
  list() {
    const now = this.now()
    return [...this.sessions.entries()]
      .filter(([, record]) => record.expiresAt > now)
      .map(([id, record]) => ({ id, ...record }))
      .sort((a, b) => a.createdAt - b.createdAt)
  }

  /**
   * Load and prune persisted sessions. A file with no valid record at all is
   * moved aside as `.corrupt-<ts>.bak` and the store starts fresh; a partially
   * damaged file keeps its valid records and is rewritten.
   *
   * @returns {number} sessions loaded
   */
  load() {
    if (!this.path || !existsSync(this.path)) return 0
    let raw
    try {
      raw = readFileSync(this.path, 'utf8')
    } catch (error) {
      this.log(`cannot read session file ${this.path}: ${error instanceof Error ? error.message : String(error)}`)
      return 0
    }

    const now = this.now()
    let loaded = 0
    let invalid = 0
    let pruned = 0
    for (const line of raw.split('\n')) {
      if (!line.trim()) continue
      let record
      try {
        record = JSON.parse(line)
      } catch {
        invalid += 1
        continue
      }
      if (typeof record?.id !== 'string' || typeof record?.userId !== 'string') {
        invalid += 1
        continue
      }
      const createdAt = Number(record.createdAt) || now
      const expiresAt = Number(record.expiresAt) || createdAt + this.ttlMs
      if (expiresAt <= now) {
        pruned += 1
        continue
      }
      this.sessions.set(record.id, {
        userId: record.userId,
        createdAt,
        lastSeenAt: Number(record.lastSeenAt) || createdAt,
        expiresAt,
      })
      loaded += 1
    }

    if (invalid > 0 && loaded === 0) {
      const backup = `${this.path}.corrupt-${Date.now()}.bak`
      try {
        renameSync(this.path, backup)
        this.log(`session file was unreadable; kept as ${backup} and started fresh`)
      } catch (error) {
        this.log(`session file was unreadable and could not be moved aside: ${error instanceof Error ? error.message : String(error)}`)
      }
      this.persistNow()
    } else if (invalid > 0 || pruned > 0) {
      if (invalid > 0) this.log(`dropped ${invalid} damaged line(s) from ${this.path}`)
      if (pruned > 0) this.log(`pruned ${pruned} expired session(s) from ${this.path}`)
      this.persistNow()
    }
    return loaded
  }

  /** Flush pending `lastSeenAt` updates and cancel the debounce timer. */
  flush() {
    if (this.flushTimer) {
      clearTimeout(this.flushTimer)
      this.flushTimer = null
    }
    if (this.dirty) this.persistNow()
  }

  /** Write the whole store to disk atomically. No-op in memory-only mode. */
  persistNow() {
    if (!this.path) return
    const lines = []
    for (const [id, record] of this.sessions) {
      lines.push(JSON.stringify({
        id,
        userId: record.userId,
        createdAt: record.createdAt,
        lastSeenAt: record.lastSeenAt,
        expiresAt: record.expiresAt,
      }))
    }
    try {
      writeFileAtomic(this.path, lines.length > 0 ? `${lines.join('\n')}\n` : '')
      this.dirty = false
    } catch (error) {
      this.log(`failed to persist sessions to ${this.path}: ${error instanceof Error ? error.message : String(error)}`)
    }
  }

  scheduleFlush() {
    if (!this.path) return
    this.dirty = true
    if (this.flushTimer) return
    this.flushTimer = setTimeout(() => {
      this.flushTimer = null
      if (this.dirty) this.persistNow()
    }, LAST_SEEN_FLUSH_MS)
    this.flushTimer.unref?.()
  }

  /** Constant-time comparison helper for opaque ids of equal length. */
  static equals(a, b) {
    if (typeof a !== 'string' || typeof b !== 'string') return false
    const left = Buffer.from(a)
    const right = Buffer.from(b)
    if (left.length !== right.length) return false
    return timingSafeEqual(left, right)
  }
}

/**
 * Parse a `Cookie:` header into a name -> value map.
 *
 * @param {string|undefined} header
 * @returns {Record<string, string>}
 */
export function parseCookies(header) {
  /** @type {Record<string, string>} */
  const cookies = {}
  if (!header) return cookies
  for (const part of header.split(';')) {
    const index = part.indexOf('=')
    if (index <= 0) continue
    const name = part.slice(0, index).trim()
    const value = part.slice(index + 1).trim()
    if (name) cookies[name] = decodeURIComponent(value)
  }
  return cookies
}
