/**
 * Browser half of dsh-plugin-sysadmin.
 *
 * Occupies the sidebar and hero brand seats with the Mila mark, shows the
 * signed-in sysadmin, and lists that user's workspace files. The official
 * DeepSeek brand row is disabled in the profile patch so these single slots
 * are free.
 *
 * Served as a client bundle. The factory form matches the harness module
 * loader; do not convert this file to ESM imports.
 */
window.__ModuleLoader__.load({
  id: 'dsh-plugin-sysadmin',
  factory: (require) => {
    const React = require('react')
    const { createElement: h, useEffect, useState } = React

    const MILA_BLUE = '#003cc5'
    const MILA_SLATE = '#353641'

    function surfaceBoot() {
      const boot = window.__SYSADMIN_SURFACE__
      if (boot && typeof boot === 'object') return boot
      return { user: 'sysadmin', title: 'Mila — Sysadmin AI', icon: '/assets/mila-logo.png' }
    }

    function MilaMark({ size, className }) {
      const edge = Number(size) > 0 ? Number(size) : 24
      return h('img', {
        className,
        src: surfaceBoot().icon || '/assets/mila-logo.png',
        alt: 'Mila',
        width: edge,
        height: edge,
        style: { width: edge, height: edge, objectFit: 'contain', display: 'block' },
      })
    }

    function MilaName() {
      return h('span', {
        style: { color: MILA_SLATE, fontWeight: 650, letterSpacing: '0.01em' },
      }, 'Mila')
    }

    function UserFiles({ wide }) {
      const boot = surfaceBoot()
      const [open, setOpen] = useState(false)
      const [path, setPath] = useState('')
      const [listing, setListing] = useState(null)
      const [error, setError] = useState('')

      useEffect(() => {
        if (!open) return undefined
        const controller = new AbortController()
        const query = path ? `?path=${encodeURIComponent(path)}` : ''
        setError('')
        fetch(`/api/sysadmin/surface${query}`, {
          credentials: 'same-origin',
          signal: controller.signal,
          headers: { accept: 'application/json' },
        }).then(async (response) => {
          const body = await response.json().catch(() => ({}))
          if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`)
          setListing(body)
        }).catch((fetchError) => {
          if (fetchError?.name === 'AbortError') return
          setListing(null)
          setError(fetchError instanceof Error ? fetchError.message : String(fetchError))
        })
        return () => controller.abort()
      }, [open, path])

      const parent = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : ''
      const label = wide ? boot.user : (boot.user || 'U').slice(0, 2)

      return h('div', { style: { display: 'flex', flexDirection: 'column', gap: 6, width: '100%' } },
        h('button', {
          type: 'button',
          title: `${boot.user} — fichiers`,
          onClick: () => setOpen((value) => !value),
          style: {
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            width: '100%',
            border: 'none',
            borderRadius: 8,
            background: 'transparent',
            color: MILA_SLATE,
            cursor: 'pointer',
            padding: wide ? '6px 8px' : '6px 0',
            font: 'inherit',
            textAlign: 'left',
          },
        },
          h('span', {
            style: {
              flex: 'none',
              width: 28,
              height: 28,
              borderRadius: 8,
              background: MILA_BLUE,
              color: '#fff',
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 11,
              fontWeight: 700,
            },
          }, label.slice(0, 2).toUpperCase()),
          wide ? h('span', { style: { minWidth: 0 } },
            h('span', { style: { display: 'block', fontWeight: 650, lineHeight: '18px' } }, boot.user),
            h('span', { style: { display: 'block', fontSize: 12, opacity: 0.7 } }, 'Fichiers'),
          ) : null,
        ),
        open ? h('div', {
          role: 'dialog',
          'aria-label': `Fichiers de ${boot.user}`,
          style: {
            position: 'fixed',
            zIndex: 40,
            left: wide ? 292 : 68,
            bottom: 16,
            width: 320,
            maxHeight: '70vh',
            overflow: 'auto',
            background: '#fff',
            color: MILA_SLATE,
            border: '1px solid #e2e3ea',
            borderTop: `3px solid ${MILA_BLUE}`,
            borderRadius: 12,
            boxShadow: '0 12px 32px rgba(53, 54, 65, 0.16)',
            padding: 12,
          },
        },
          h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, marginBottom: 8 } },
            h('strong', null, boot.user),
            h('button', {
              type: 'button',
              onClick: () => setOpen(false),
              style: { border: 'none', background: 'transparent', cursor: 'pointer', font: 'inherit' },
            }, 'Fermer'),
          ),
          h('div', { style: { fontSize: 12, opacity: 0.7, marginBottom: 8 } }, path || '/'),
          path ? h('button', {
            type: 'button',
            onClick: () => setPath(parent),
            style: { border: 'none', background: 'transparent', color: MILA_BLUE, cursor: 'pointer', padding: 0, marginBottom: 8, font: 'inherit' },
          }, '↑ dossier parent') : null,
          error ? h('p', { style: { color: '#b3261e' } }, error) : null,
          listing ? h('ul', { style: { listStyle: 'none', margin: 0, padding: 0 } },
            listing.entries.length === 0 ? h('li', { style: { opacity: 0.7 } }, 'Aucun fichier') : null,
            ...listing.entries.map((entry) => h('li', { key: entry.name },
              entry.kind === 'directory'
                ? h('button', {
                  type: 'button',
                  onClick: () => setPath(path ? `${path}/${entry.name}` : entry.name),
                  style: { border: 'none', background: 'transparent', color: MILA_BLUE, cursor: 'pointer', padding: '4px 0', font: 'inherit' },
                }, `${entry.name}/`)
                : h('span', { style: { display: 'block', padding: '4px 0' } },
                  entry.name,
                  entry.bytes != null ? h('span', { style: { opacity: 0.6 } }, ` · ${entry.bytes} o`) : null,
                ),
            )),
            listing.truncated ? h('li', { style: { opacity: 0.7 } }, 'Liste tronquée') : null,
          ) : (error ? null : h('p', { style: { opacity: 0.7 } }, 'Chargement…')),
        ) : null,
      )
    }

    const inject = ['slots']

    function apply(ctx) {
      ctx.slots.inject('sidebar.brand.mark', () => ctx.slots.inject('sidebar.brand.name', function* registerBrand() {
        yield ctx.slots.register({ name: 'sidebar.brand.mark' }, MilaMark)
        yield ctx.slots.register({ name: 'sidebar.brand.name' }, MilaName)
      }))
      ctx.slots.inject('conversation.hero.brand.mark', function* registerHero() {
        yield ctx.slots.register({ name: 'conversation.hero.brand.mark' }, MilaMark)
      })
      ctx.slots.inject('sidebar.footer.action', function* registerUser() {
        yield ctx.slots.register({ name: 'sidebar.footer.action', id: 'sysadmin-user' }, UserFiles)
      })
      try {
        ctx.theme?.overrideTokens?.('dsh-plugin-sysadmin', {
          '--dsw-alias-brand-primary': { light: '#003cc5', dark: '#7aa2ff' },
        })
      } catch {
        /* theme tokens are optional; the mark and user chip still render */
      }
    }

    return { apply, inject }
  },
})
