/**
 * Workspace listing for the signed-in user: relative paths only, no escape
 * through `..` or a symlink that leaves the workspace.
 */
import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import { listSurface, normalizeRelative } from '../dsh-plugin-sysadmin/lib/surface.js'

test('lists the user workspace and rejects paths that leave it', () => {
  const root = mkdtempSync(join(tmpdir(), 'sysadmin-surface-'))
  const outside = mkdtempSync(join(tmpdir(), 'sysadmin-surface-out-'))
  mkdirSync(join(root, 'notes'))
  writeFileSync(join(root, 'notes', 'a.txt'), 'hello')
  writeFileSync(join(root, 'readme.txt'), 'x')
  symlinkSync(outside, join(root, 'escape'))

  const listing = listSurface(root, 'sysadmin-01')
  assert.equal(listing.user, 'sysadmin-01')
  assert.equal(listing.path, '')
  assert.deepEqual(listing.entries.map((entry) => entry.name), ['notes', 'readme.txt', 'escape'])
  assert.equal(listing.entries.find((entry) => entry.name === 'escape').kind, 'symlink')
  assert.equal(listing.entries.find((entry) => entry.name === 'readme.txt').bytes, 1)

  const nested = listSurface(root, 'sysadmin-01', 'notes')
  assert.equal(nested.path, 'notes')
  assert.equal(nested.entries[0].name, 'a.txt')

  assert.throws(() => normalizeRelative('../etc'), /escapes/)
  assert.throws(() => listSurface(root, 'sysadmin-01', '../etc'), /escapes/)
  assert.throws(() => listSurface(root, 'sysadmin-01', '/etc'), /absolute/)
})
