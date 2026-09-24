/**
 * HTTP client for the Python sysadmin platform backend.
 *
 * The harness owns the model loop; the backend owns identity, quota, the
 * bounded sysadmin tools, the approval gate and the audit store. This client is
 * the single place where harness-originated data crosses into the backend, so
 * every call carries the authenticated user's bearer token and never a
 * client-supplied identity header.
 */

export class BackendError extends Error {
  /**
   * @param {string} message
   * @param {number} status
   * @param {unknown} body
   */
  constructor(message, status, body) {
    super(message)
    this.name = 'BackendError'
    this.status = status
    this.body = body
  }
}

export class BackendClient {
  /**
   * @param {object} options
   * @param {string} [options.baseUrl] agent platform base, default :3080
   * @param {string} [options.authUrl] auth gateway base, default :3081
   * @param {string} [options.token] the caller's per-user bearer token
   * @param {number} [options.timeoutMs]
   * @param {typeof fetch} [options.fetchImpl]
   */
  constructor({
    baseUrl = 'http://127.0.0.1:3080',
    authUrl = 'http://127.0.0.1:3081',
    token = '',
    timeoutMs = 15000,
    fetchImpl = globalThis.fetch,
  } = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, '')
    this.authUrl = authUrl.replace(/\/+$/, '')
    this.token = token
    this.timeoutMs = timeoutMs
    this.fetch = fetchImpl
  }

  headers(extra = {}) {
    const headers = { accept: 'application/json', ...extra }
    if (this.token) headers.authorization = `Bearer ${this.token}`
    return headers
  }

  async #request(url, init = {}) {
    let response
    try {
      response = await this.fetch(url, {
        ...init,
        headers: this.headers(init.headers),
        signal: AbortSignal.timeout(this.timeoutMs),
      })
    } catch (error) {
      throw new BackendError(`backend unreachable: ${error instanceof Error ? error.message : String(error)}`, 0, null)
    }
    const text = await response.text()
    let body = text
    try {
      body = text ? JSON.parse(text) : null
    } catch {
      /* keep the raw text */
    }
    if (!response.ok) {
      const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : body
      throw new BackendError(`backend returned ${response.status}: ${String(detail)}`, response.status, body)
    }
    return body
  }

  /** @returns {Promise<Record<string, unknown>>} */
  health() {
    return this.#request(`${this.baseUrl}/health`, { method: 'GET' })
  }

  /** @returns {Promise<Record<string, unknown>>} */
  listTools() {
    return this.#request(`${this.baseUrl}/api/tools/list`, { method: 'GET' })
  }

  /**
   * Execute one bounded backend tool as the authenticated caller.
   *
   * @param {string} name
   * @param {Record<string, unknown>} parameters
   * @returns {Promise<unknown>}
   */
  executeTool(name, parameters = {}) {
    return this.#request(`${this.baseUrl}/api/tools/execute`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ name, parameters }),
    })
  }

  /**
   * Resolve an identity from the auth gateway. Used by the multi-user gateway
   * to confirm a session before it is trusted.
   *
   * @param {string} sessionId
   * @returns {Promise<Record<string, unknown>>}
   */
  verifySession(sessionId) {
    return this.#request(`${this.authUrl}/api/v1/auth/verify`, {
      method: 'GET',
      headers: { cookie: `session_id=${sessionId}` },
    })
  }
}
