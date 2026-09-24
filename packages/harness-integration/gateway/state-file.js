/**
 * Atomic state-file helpers shared by the gateway's persisted stores.
 *
 * Persisted state follows the backend outbox discipline: write a temp file,
 * fsync it, then rename over the target. A crash mid-write leaves the previous
 * good file in place and never a half-written record. Files are created 0600
 * (directories 0700) because gateway state holds opaque session ids, ports and
 * pids — never credential material.
 */
import { closeSync, fsyncSync, mkdirSync, openSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'

/**
 * Create a directory private to the service account.
 *
 * @param {string} path
 */
export function ensurePrivateDir(path) {
  mkdirSync(path, { recursive: true, mode: 0o700 })
}

/**
 * Write `content` to `path` atomically (temp file + fsync + rename, 0600).
 *
 * @param {string} path
 * @param {string} content
 */
export function writeFileAtomic(path, content) {
  ensurePrivateDir(dirname(path))
  const tmp = `${path}.tmp`
  const fd = openSync(tmp, 'w', 0o600)
  try {
    writeFileSync(fd, content)
    fsyncSync(fd)
  } finally {
    closeSync(fd)
  }
  renameSync(tmp, path)
  fsyncDir(dirname(path))
}

/**
 * Read and parse a JSON file, returning null when it is missing or corrupt.
 *
 * @param {string} path
 * @returns {unknown|null}
 */
export function readJsonFile(path) {
  try {
    return JSON.parse(readFileSync(path, 'utf8'))
  } catch {
    return null
  }
}

/** @param {string} path */
export function removeFile(path) {
  try {
    rmSync(path, { force: true })
  } catch {
    /* already gone */
  }
}

/** Best-effort directory fsync so a rename is durable. @param {string} dir */
function fsyncDir(dir) {
  let fd
  try {
    fd = openSync(dir, 'r')
    fsyncSync(fd)
  } catch {
    /* not supported everywhere; the rename succeeded regardless */
  } finally {
    if (fd !== undefined) closeSync(fd)
  }
}
