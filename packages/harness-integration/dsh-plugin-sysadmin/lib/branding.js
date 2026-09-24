/**
 * Mila branding for the harness web surface.
 *
 * The harness UI is not forked: the bundle hooks the webserver's supported
 * `tapIndex` seam and rewrites the rendered index head. Dependency-free so it
 * can be unit tested outside a dsh process.
 */

/** Title shown in the browser tab. */
export const BRAND_TITLE = 'Mila — Sysadmin AI'
/** Logo served by the gateway; the browser reaches it through the proxy. */
export const BRAND_ICON = '/assets/mila-logo.png'
/** Boot global the client plugin reads for the signed-in user. */
export const SURFACE_GLOBAL = '__SYSADMIN_SURFACE__'

/**
 * Structured index rows: the signed-in user, for the in-app Mila chrome.
 * No token and no filesystem path — the files panel asks the surface route.
 *
 * @param {{ userId: string }} surface
 * @returns {Array<{ kind: 'global', name: string, value: { user: string, title: string, icon: string } }>}
 */
export function surfaceInjections({ userId }) {
  return [{
    kind: 'global',
    name: SURFACE_GLOBAL,
    value: {
      user: String(userId || 'sysadmin'),
      title: BRAND_TITLE,
      icon: BRAND_ICON,
    },
  }]
}

/**
 * Retitle the document and point the favicon at the Mila asset.
 *
 * @param {string} html
 * @returns {string}
 */
export function brandIndexHtml(html) {
  let out = String(html)
  if (/<title>[^<]*<\/title>/i.test(out)) {
    out = out.replace(/<title>[^<]*<\/title>/i, `<title>${BRAND_TITLE}</title>`)
  } else {
    out = out.replace(/<head(\s[^>]*)?>/i, (match) => `${match}<title>${BRAND_TITLE}</title>`)
  }
  if (!out.includes(BRAND_ICON)) {
    out = out.replace(
      /<\/head>/i,
      `<link rel="icon" type="image/png" href="${BRAND_ICON}">`
        + `<meta name="application-name" content="${BRAND_TITLE}"></head>`,
    )
  }
  return out
}
