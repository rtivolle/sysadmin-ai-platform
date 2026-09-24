/**
 * Branding tests: the plugin retitles the harness web surface through its
 * `tapIndex` seam, so the rewrite contract is pinned here without running a
 * dsh process — title swap, favicon injection, idempotency, a missing-title
 * fallback, and proof that nothing else in the document changes.
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  BRAND_ICON,
  BRAND_TITLE,
  SURFACE_GLOBAL,
  brandIndexHtml,
  surfaceInjections,
} from '../dsh-plugin-sysadmin/lib/branding.js'

const SAMPLE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DeepSeek Harness</title>
<script src="/assets/index.js"></script>
</head>
<body>
<!-- marker: untouched -->
<div id="app"></div>
</body>
</html>`

/** @param {string} haystack @param {string} needle @returns {number} */
function count(haystack, needle) {
  return haystack.split(needle).length - 1
}

test('brandIndexHtml retitles the document and injects the Mila favicon', () => {
  const out = brandIndexHtml(SAMPLE)

  assert.equal(BRAND_TITLE, 'Mila — Sysadmin AI')
  assert.equal(BRAND_ICON, '/assets/mila-logo.png')
  assert.equal(out.includes('<title>DeepSeek Harness</title>'), false)
  assert.ok(out.includes(`<title>${BRAND_TITLE}</title>`))
  assert.ok(out.includes(`href="${BRAND_ICON}"`))
  assert.ok(
    out.indexOf(BRAND_ICON) < out.indexOf('</head>'),
    'the icon link must be inserted before </head>',
  )
})

test('brandIndexHtml is idempotent: a second pass does not duplicate the icon', () => {
  const once = brandIndexHtml(SAMPLE)
  const twice = brandIndexHtml(once)

  assert.equal(count(twice, BRAND_ICON), 1)
  assert.equal(count(twice, `<title>${BRAND_TITLE}</title>`), 1)
  assert.ok(twice.includes(`<meta name="application-name" content="${BRAND_TITLE}">`))
})

test('brandIndexHtml adds a title inside <head> when the document has none', () => {
  const input = '<!doctype html><html><head><meta charset="utf-8"></head><body>x</body></html>'
  const out = brandIndexHtml(input)

  assert.ok(out.includes(`<head><title>${BRAND_TITLE}</title>`))
  assert.ok(out.includes('<meta charset="utf-8">'))
})

test('brandIndexHtml leaves the rest of the document untouched', () => {
  const out = brandIndexHtml(SAMPLE)

  assert.ok(out.startsWith('<!doctype html>'))
  assert.ok(out.trimEnd().endsWith('</html>'))
  assert.ok(out.includes('<!-- marker: untouched -->'))
  assert.ok(out.includes('<script src="/assets/index.js"></script>'))
  assert.ok(out.includes('<div id="app"></div>'))
})

test('surfaceInjections publishes the signed-in user and not a filesystem path', () => {
  const [row] = surfaceInjections({ userId: 'sysadmin-01' })
  assert.equal(row.kind, 'global')
  assert.equal(row.name, SURFACE_GLOBAL)
  assert.equal(row.value.user, 'sysadmin-01')
  assert.equal(row.value.title, BRAND_TITLE)
  assert.equal(row.value.icon, BRAND_ICON)
  assert.equal(JSON.stringify(row).includes('/data/'), false)
})

test('BRAND_TITLE matches the title injected into the document', () => {
  const out = brandIndexHtml(SAMPLE)
  const match = out.match(/<title>([^<]*)<\/title>/i)

  assert.ok(match, 'the branded document must contain a title element')
  assert.equal(match[1], BRAND_TITLE)
})
