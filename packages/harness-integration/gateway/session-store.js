/**
 * In-memory gateway session store.
 *
 * A gateway session is the browser's proof of a successful login against the
 * platform auth gateway. It maps an opaque cookie value to the authenticated
 * sysadmin identity and an expiry; it deliberately holds no credential material.
 */
import { randomBytes, timingSafeEqual } from 'node:crypto'

export class SessionStore {
  /**
   * @param {object} options
   * @param {number} options.ttlMs absolute session lifetime
   * @param {() => number} [options.now]
   */
  constructor({ ttlMs, now = () => Date.now() }) {
    this.ttlMs = ttlMs
    this.now = now
    /** @type {Map<string, { userId: string, createdAt: number, expiresAt: number }>} */
    this.sessions = new Map()
  }

  /**
   * @param {string} userId
   * @returns {string} the opaque session id
   */
  create(userId) {
    const sessionId = randomBytes(32).toString('base64url')
    const createdAt = this.now()
    this.sessions.set(sessionId, { userId, createdAt, expiresAt: createdAt + this.ttlMs })
    return sessionId
  }

  /**
   * @param {string|undefined} sessionId
   * @returns {{ userId: string, createdAt: number, expiresAt: number }|null}
   */
  get(sessionId) {
    if (!sessionId) return null
    const record = this.sessions.get(sessionId)
    if (!record) return null
    if (record.expiresAt <= this.now()) {
      this.sessions.delete(sessionId)
      return null
    }
    return record
  }

  /** @param {string|undefined} sessionId */
  delete(sessionId) {
    if (!sessionId) return false
    return this.sessions.delete(sessionId)
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
    return dropped
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
