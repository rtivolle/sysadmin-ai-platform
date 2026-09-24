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
