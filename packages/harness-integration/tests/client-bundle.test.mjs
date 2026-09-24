/**
 * Smoke test for the client bundle: the factory form must load under the
 * harness module loader, apply() must occupy the brand and footer seats with
 * the sysadmin components, and the tab title must be pinned to Mila.
 *
 * No browser involved: the bundle is evaluated in a VM with a stub loader.
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'
import vm from 'node:vm'

const SOURCE = readFileSync(join(import.meta.dirname, '..', 'dsh-plugin-sysadmin', 'lib', 'client.js'), 'utf8')

function fakeRequire(specifier) {
  if (specifier === 'react') {
    return {
      createElement: (type, props, ...children) => ({ type, props: props ?? {}, children }),
      useEffect: () => {},
      useState: (initial) => [initial, () => {}],
    }
  }
  throw new Error(`unexpected require in client bundle: ${specifier}`)
}

test('client bundle factory applies to the brand and user seats', () => {
  const loaded = []
  const sandbox = {
    window: { __ModuleLoader__: { load: (entry) => loaded.push(entry) }, __SYSADMIN_SURFACE__: { user: 'sysadmin-01', title: 'Mila — Sysadmin AI', icon: '/assets/mila-logo.png' } },
    document: { title: '', querySelector: () => null },
    console,
  }
  vm.runInNewContext(SOURCE, sandbox)

  assert.equal(loaded.length, 1)
  assert.equal(loaded[0].id, 'dsh-plugin-sysadmin')

  const module = loaded[0].factory(fakeRequire)
  assert.deepEqual(Array.from(module.inject), ['slots'])

  const registrations = []
  const ctx = {
    slots: {
      inject: (seat, fn) => registrations.push({ seat, fn }),
      register: (spec, component) => registrations.push({ register: spec, component }),
    },
    theme: { overrideTokens: (source, tokens) => registrations.push({ override: source, tokens }) },
  }
  module.apply(ctx)

  assert.equal(sandbox.document.title, 'Mila — Sysadmin AI')
  const seats = new Set()
  for (const row of registrations) {
    if (row.seat) seats.add(row.seat)
    if (row.register) {
      seats.add(row.register.name)
      if (row.register.id) seats.add(`${row.register.name}#${row.register.id}`)
    }
    if (typeof row.fn === 'function') {
      const generator = row.fn()
      if (generator && typeof generator[Symbol.iterator] === 'function') {
        for (const step of generator) {
          if (step?.name) seats.add(step.name)
          if (step?.id) seats.add(`${step.name}#${step.id}`)
        }
      }
    }
  }
  for (const seat of ['sidebar.brand.mark', 'sidebar.brand.name', 'conversation.hero.brand.mark', 'sidebar.footer.action#sysadmin-user']) {
    assert.ok(seats.has(seat), `missing seat ${seat}`)
  }
  const overrides = registrations.filter((row) => row.override)
  assert.equal(overrides.length, 1)
  assert.equal(overrides[0].override, 'dsh-plugin-sysadmin')
  assert.equal(overrides[0].tokens['--dsw-alias-brand-primary'].light, '#003cc5')
})
