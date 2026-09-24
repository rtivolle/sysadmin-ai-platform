/**
 * Read-only listing of the signed-in user's workspace.
 *
 * The browser never chooses a root: every path is relative to the instance
 * workspace, `..` is rejected, and symlink entries are reported but not followed.
 */
import { lstatSync, readdirSync, realpathSync } from 'node:fs'
import { isAbsolute, join, relative } from 'node:path'

export const SURFACE_PATH = '/api/sysadmin/surface'
export const MAX_SURFACE_ENTRIES = 200

export class SurfaceError extends Error {
  /**
   * @param {string} message
   * @param {number} status
   */
  constructor(message, status = 400) {
    super(message)
    this.name = 'SurfaceError'
    this.status = status
  }
}

/**
 * @param {string} workspaceRoot
 * @param {string} userId
 * @param {string} [relativePath]
 * @returns {{ user: string, path: string, truncated: boolean, entries: Array<{ name: string, kind: string, bytes: number|null }> }}
 */
export function listSurface(workspaceRoot, userId, relativePath = '') {
  const requested = normalizeRelative(relativePath)
  let root
  let target
  try {
    root = realpathSync(workspaceRoot)
    target = realpathSync(requested === '' ? root : join(root, requested))
  } catch {
    throw new SurfaceError('workspace path not found', 404)
  }
  if (!isInside(root, target)) throw new SurfaceError('path escapes the workspace')
  let dirStat
  try {
    dirStat = lstatSync(target)
  } catch {
    throw new SurfaceError('workspace path not found', 404)
  }
  if (!dirStat.isDirectory()) throw new SurfaceError('not a directory')

  /** @type {Array<{ name: string, kind: string, bytes: number|null }>} */
  const entries = []
  let truncated = false
  for (const name of readdirSync(target)) {
    if (name === '.' || name === '..' || name.includes('/') || name.includes('\\')) continue
    if (entries.length >= MAX_SURFACE_ENTRIES) {
      truncated = true
      break
    }
    let stat
    try {
      stat = lstatSync(join(target, name))
    } catch {
      continue
    }
    let kind = 'other'
    if (stat.isSymbolicLink()) kind = 'symlink'
    else if (stat.isDirectory()) kind = 'directory'
    else if (stat.isFile()) kind = 'file'
    entries.push({
      name,
      kind,
      bytes: kind === 'file' ? stat.size : null,
    })
  }
  entries.sort((left, right) => {
    const rank = (kind) => (kind === 'directory' ? 0 : kind === 'file' ? 1 : 2)
    const byKind = rank(left.kind) - rank(right.kind)
    if (byKind !== 0) return byKind
    return left.name.localeCompare(right.name, undefined, { numeric: true, sensitivity: 'base' })
  })
  return { user: userId, path: requested, truncated, entries }
}

/**
 * @param {string} input
 * @returns {string}
 */
export function normalizeRelative(input) {
  const raw = String(input ?? '').replaceAll('\\', '/').trim()
  if (raw === '' || raw === '.') return ''
  if (raw.startsWith('/') || /^[A-Za-z]:/.test(raw)) {
    throw new SurfaceError('absolute paths are not allowed')
  }
  const parts = []
  for (const part of raw.split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') throw new SurfaceError('path escapes the workspace')
    parts.push(part)
  }
  return parts.join('/')
}

/**
 * @param {string} root
 * @param {string} target
 * @returns {boolean}
 */
function isInside(root, target) {
  const rel = relative(root, target)
  return rel === '' || (!rel.startsWith('..') && !isAbsolute(rel))
}
