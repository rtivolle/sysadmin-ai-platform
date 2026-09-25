/* Admin console for the Mila sysadmin harness gateway. Vanilla JS, no build step. */
'use strict'

const state = {
  authenticated: false,
  user: null,
  activeTab: 'overview',
  pollTimer: null,
  logUser: null,
  logTimer: null,
  logNowrap: false,
  tickTimer: null,
  authGeneration: 0,
  serviceOutput: '',
  quotaDirty: false,
  quotaLoading: false,
  quotaSaving: false,
  localModelsDirty: false,
  localModelsBusy: false,
  loaded: {},
}

const $ = (id) => document.getElementById(id)

// Ordered tab list doubles as the hash router's whitelist.
const TABS = [
  'overview', 'users', 'quotas', 'instances', 'models',
  'local-models', 'approvals', 'audit', 'services', 'hardware',
]

function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
}

function fmtDuration(ms) {
  if (!Number.isFinite(ms)) return '—'
  const seconds = Math.floor(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

function fmtClock(ts) {
  const date = new Date(ts ?? Date.now())
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

function fmtBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '—'
  const units = ['o', 'Kio', 'Mio', 'Gio', 'Tio']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1 }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`
}

// ── Relative time ─────────────────────────────────────────────────────────────

function fmtRelative(ts, { future = 'dans ', past = 'il y a ' } = {}) {
  if (!Number.isFinite(ts)) return '—'
  const diff = Number(ts) - Date.now()
  const abs = Math.abs(diff)
  if (abs < 60_000) return diff < 0 ? 'à l’instant' : 'maintenant'
  const minutes = Math.floor(abs / 60_000)
  if (minutes < 60) return `${diff < 0 ? past : future}${minutes} min`
  const hours = Math.floor(minutes / 60)
  const remMinutes = minutes % 60
  if (hours < 24) return `${diff < 0 ? past : future}${hours} h${remMinutes ? ` ${remMinutes} min` : ''}`
  const days = Math.floor(hours / 24)
  const remHours = hours % 24
  return `${diff < 0 ? past : future}${days} j${remHours ? ` ${remHours} h` : ''}`
}

function fmtAgo(ts) {
  return fmtRelative(ts, { future: '', past: 'il y a ' })
}

function fmtCountdown(tsMs) {
  if (!Number.isFinite(tsMs)) return '—'
  const diff = Number(tsMs) - Date.now()
  if (diff <= 0) return 'expirée'
  const seconds = Math.floor(diff / 1000)
  if (seconds < 60) return `dans ${seconds} s`
  const minutes = Math.floor(seconds / 60)
  const remSeconds = seconds % 60
  if (minutes < 60) return `dans ${minutes} min ${remSeconds} s`
  const hours = Math.floor(minutes / 60)
  const remMinutes = minutes % 60
  return `dans ${hours} h ${remMinutes} min`
}

function fmtSessionExpiry(ts) {
  if (!Number.isFinite(ts)) return '—'
  if (Number(ts) <= Date.now()) return 'expirée'
  return fmtRelative(ts)
}

// ── Usage meters ─────────────────────────────────────────────────────────────

function usagePercent(used, limit) {
  if (!Number.isFinite(limit) || limit <= 0) return 0
  return Math.max(0, Math.min(100, (used / limit) * 100))
}

function usageTone(pct) {
  if (pct >= 95) return 'bad'
  if (pct >= 75) return 'warn'
  return 'ok'
}

function usageBar(used, limit) {
  const pct = usagePercent(used, limit)
  const tone = usageTone(pct)
  return `<div class="mila-meter" role="img" aria-label="${esc(`${used} sur ${limit}`)}"><span class="mila-meter-fill ${tone}" style="width:${pct.toFixed(1)}%"></span></div>`
}

function emptyState(title, hint = '') {
  return `<div class="mila-empty" role="status">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M8 12h8"/></svg>
    <p>${esc(title)}</p>
    ${hint ? `<p class="mila-muted">${esc(hint)}</p>` : ''}
  </div>`
}

function updateNavBadge(name, count, tone) {
  const badge = $('tabs').querySelector(`[data-badge="${name}"]`)
  if (!badge) return
  if (count > 0) {
    badge.textContent = String(count)
    badge.hidden = false
    if (tone) badge.dataset.tone = tone
  } else {
    badge.textContent = ''
    badge.hidden = true
    delete badge.dataset.tone
  }
}

// ── Instance state ───────────────────────────────────────────────────────────

function instanceStateTone(state) {
  if (state === 'running') return 'ok'
  if (state === 'failed') return 'bad'
  if (state === 'starting' || state === 'restarting') return 'busy'
  return 'warn'
}

function instanceStateLabel(state) {
  if (state === 'running') return 'en service'
  if (state === 'failed') return 'en échec'
  if (state === 'starting') return 'démarrage'
  if (state === 'restarting') return 'redémarrage'
  return state || 'arrêtée'
}

function instanceBadge(instance) {
  if (!instance) {
    return '<span class="mila-state"><span class="mila-dot"></span><span class="mila-badge">arrêtée</span></span>'
  }
  const state = String(instance.state ?? 'unknown')
  const tone = instanceStateTone(state)
  return `<span class="mila-state"><span class="mila-dot ${tone}"></span><span class="mila-badge ${tone}">${esc(instanceStateLabel(state))}</span></span>`
}

async function api(path, options = {}) {
  const headers = { 'x-sysadmin-admin': '1', ...(options.headers ?? {}) }
  if (options.body !== undefined) headers['content-type'] = 'application/json'
  const response = await fetch(path, {
    method: options.method ?? 'GET',
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  })
  if (response.status === 401) {
    setAuthenticated(false)
    throw new Error('session administrateur expirée')
  }
  let payload = null
  const text = await response.text()
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = { detail: text }
    }
  }
  if (!response.ok) {
    throw new Error(payload?.detail || `HTTP ${response.status}`)
  }
  return payload
}

// ── Toasts ───────────────────────────────────────────────────────────────────

const TOAST_ICONS = {
  error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>',
  success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m8.5 12.5 2.5 2.5 5-5"/></svg>',
  info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/></svg>',
}

const TOAST_CLOSE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>'

// One live toast per tone+message: a sustained outage must not stack an
// identical error every 7s poll while the persistent alert already shows it.
const liveToasts = new Map()

function pruneLiveToasts() {
  for (const [key, node] of liveToasts) {
    if (!node.isConnected) liveToasts.delete(key)
  }
}

function toast(message, { tone = 'info', timeout = 5000 } = {}) {
  const key = `${tone}:${message}`
  const existing = liveToasts.get(key)
  if (existing?.isConnected) return existing

  const container = $('toasts')
  while (container.children.length >= 4) container.firstElementChild?.remove()
  pruneLiveToasts()

  const node = document.createElement('div')
  node.className = `mila-toast ${tone}`
  node.setAttribute('role', tone === 'error' ? 'alert' : 'status')
  node.innerHTML = `
    ${TOAST_ICONS[tone] ?? TOAST_ICONS.info}
    <div class="body">${esc(message)}</div>
    <button class="close" type="button" aria-label="Fermer">${TOAST_CLOSE}</button>`

  let leaving = false
  const dismiss = () => {
    if (leaving) return
    leaving = true
    node.classList.add('leaving')
    const remove = () => {
      node.remove()
      if (liveToasts.get(key) === node) liveToasts.delete(key)
    }
    node.addEventListener('transitionend', remove, { once: true })
    setTimeout(remove, 220)
  }

  node.querySelector('.close').addEventListener('click', dismiss)
  container.appendChild(node)
  liveToasts.set(key, node)
  if (timeout > 0) setTimeout(dismiss, timeout)
  return node
}

function setError(message) {
  const alert = $('console-error')
  if (message) {
    alert.textContent = message
    alert.hidden = false
    toast(message, { tone: 'error' })
  } else {
    alert.textContent = ''
    alert.hidden = true
  }
}

// ── Confirm dialog ───────────────────────────────────────────────────────────

function confirmDialog({ title, message, confirmLabel = 'Confirmer', danger = true, extraLabel }) {
  const dialog = $('confirm-dialog')
  if (dialog.open) return Promise.resolve({ ok: false, extra: false })
  if (typeof dialog.showModal !== 'function') {
    const ok = window.confirm(`${title}\n\n${message}`)
    return Promise.resolve({ ok, extra: false })
  }
  $('confirm-title').textContent = title
  $('confirm-message').textContent = message
  const okButton = $('confirm-ok')
  okButton.textContent = confirmLabel
  okButton.classList.toggle('danger', danger)
  const extraWrap = $('confirm-extra-wrap')
  const extraInput = $('confirm-extra')
  if (extraLabel) {
    extraWrap.hidden = false
    $('confirm-extra-label').textContent = extraLabel
    extraInput.checked = false
  } else {
    extraWrap.hidden = true
    extraInput.checked = false
  }
  return new Promise((resolve) => {
    let settled = false
    const finish = (ok) => {
      if (settled) return
      settled = true
      dialog.close()
      resolve({ ok, extra: extraInput.checked })
    }
    okButton.onclick = () => finish(true)
    $('confirm-cancel').onclick = () => finish(false)
    dialog.oncancel = () => finish(false)
    dialog.showModal()
  })
}

// ── Auth state ───────────────────────────────────────────────────────────────

function setAuthenticated(authenticated) {
  state.authGeneration += 1
  state.authenticated = authenticated
  $('login-view').hidden = authenticated
  $('console-view').hidden = !authenticated
  $('logout').hidden = !authenticated
  $('admin-user').textContent = authenticated ? (state.user ?? '') : ''
  if (authenticated) {
    startPolling()
    startTicker()
  } else {
    stopPolling()
    stopTicker()
    stopLogPolling(true)
    state.user = null
    state.loaded = {}
    // A dirty edit or in-flight save from the previous session must not block
    // the next login's first render (guards would early-return forever).
    state.quotaDirty = false
    state.quotaLoading = false
    state.quotaSaving = false
    state.localModelsDirty = false
    state.localModelsBusy = false
  }
}

function startTicker() {
  stopTicker()
  state.tickTimer = setInterval(tickCountdowns, 1000)
}

function stopTicker() {
  if (state.tickTimer) clearInterval(state.tickTimer)
  state.tickTimer = null
}

function tickCountdowns() {
  if (document.visibilityState !== 'visible') return
  for (const el of document.querySelectorAll('[data-countdown]')) {
    const ms = Number(el.dataset.countdown)
    el.textContent = fmtCountdown(ms)
    el.classList.toggle('mila-muted', ms <= Date.now())
  }
}

function startPolling() {
  stopPolling()
  refreshActive()
  state.pollTimer = setInterval(() => {
    if (document.visibilityState === 'visible') refreshActive()
  }, 7000)
}

function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer)
  state.pollTimer = null
}

// RENDERERS is filled below; the audit tab is static HTML with no fetch.
const RENDERERS = {}

function refreshActive() {
  const tab = state.activeTab
  const render = RENDERERS[tab]
  if (!render) {
    updateLastUpdated()
    return
  }
  const panel = $(`tab-${tab}`)
  if (!state.loaded[tab]) showSkeleton(panel)
  const generation = state.authGeneration
  render().then(() => {
    if (generation !== state.authGeneration) return
    state.loaded[tab] = true
    updateLastUpdated()
  }).catch((error) => {
    if (generation !== state.authGeneration) return
    if (!state.loaded[tab]) {
      panel.innerHTML = `<div class="mila-empty" role="status"><p>Impossible de charger cette section.</p><p class="mila-muted">${esc(error.message)}</p></div>`
    }
    setError(error.message)
  })
}

function updateLastUpdated() {
  $('last-updated').textContent = `actualisé à ${fmtClock()}`
}

function showSkeleton(panel) {
  panel.innerHTML = `
    <div class="mila-skeleton-page" aria-hidden="true">
      <div class="mila-skeleton mila-skeleton-line" style="width: 34%; height: 1.4rem;"></div>
      <div class="mila-skeleton mila-skeleton-line" style="width: 62%;"></div>
      <div class="mila-skeleton-grid">
        <div class="mila-skeleton mila-skeleton-card"></div>
        <div class="mila-skeleton mila-skeleton-card"></div>
        <div class="mila-skeleton mila-skeleton-card"></div>
        <div class="mila-skeleton mila-skeleton-card"></div>
      </div>
      <div class="mila-skeleton mila-skeleton-line"></div>
      <div class="mila-skeleton mila-skeleton-line" style="width: 78%;"></div>
    </div>`
}

// ── Hash router ──────────────────────────────────────────────────────────────

function currentHashTab() {
  const hash = location.hash.replace(/^#/, '')
  return TABS.includes(hash) ? hash : 'overview'
}

function syncTabUI() {
  for (const name of TABS) $(`tab-${name}`).hidden = name !== state.activeTab
  for (const button of $('tabs').querySelectorAll('button[data-tab]')) {
    const selected = button.dataset.tab === state.activeTab
    button.classList.toggle('active', selected)
    button.setAttribute('aria-selected', String(selected))
    button.tabIndex = selected ? 0 : -1
  }
}

function activateTab(tab, { render = true } = {}) {
  if (!TABS.includes(tab)) tab = 'overview'
  const changed = state.activeTab !== tab
  state.activeTab = tab
  syncTabUI()
  if (changed) {
    setError('')
    if (tab !== 'instances') stopLogPolling()
  }
  if (render) refreshActive()
}

function goTo(tab) {
  const target = `#${tab}`
  if (location.hash !== target) location.hash = target
  else activateTab(tab)
}

$('tabs').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-tab]')
  if (!button) return
  goTo(button.dataset.tab)
})

$('tabs').addEventListener('keydown', (event) => {
  const buttons = Array.from($('tabs').querySelectorAll('button[data-tab]'))
  if (!buttons.length) return
  let index = buttons.findIndex((button) => button.dataset.tab === state.activeTab)
  if (index < 0) index = 0
  let target = null
  switch (event.key) {
    case 'ArrowDown':
    case 'ArrowRight': target = (index + 1) % buttons.length; break
    case 'ArrowUp':
    case 'ArrowLeft': target = (index - 1 + buttons.length) % buttons.length; break
    case 'Home': target = 0; break
    case 'End': target = buttons.length - 1; break
    default: return
  }
  event.preventDefault()
  const button = buttons[target]
  goTo(button.dataset.tab)
  button.focus()
})

window.addEventListener('hashchange', () => activateTab(currentHashTab()))

$('refresh').addEventListener('click', () => refreshActive())

// The tablist is vertical in the desktop sidebar and horizontal in the mobile
// nav rail; keep aria-orientation in lockstep with the responsive breakpoint.
const navMedia = window.matchMedia('(min-width: 1024px)')
function syncNavOrientation() {
  $('tabs').setAttribute('aria-orientation', navMedia.matches ? 'vertical' : 'horizontal')
}
navMedia.addEventListener('change', syncNavOrientation)
syncNavOrientation()

// ── Page header helper ───────────────────────────────────────────────────────

function renderPageHeader(title, subtitleHtml = '', actionsHtml = '') {
  return `
    <div class="mila-page-head">
      <div class="mila-page-head-text">
        <h2>${esc(title)}</h2>
        ${subtitleHtml ? `<p class="mila-page-sub">${subtitleHtml}</p>` : ''}
      </div>
      ${actionsHtml ? `<div class="mila-page-head-actions">${actionsHtml}</div>` : ''}
    </div>`
}

// ── Gateway chip ─────────────────────────────────────────────────────────────

function updateGatewayChip(overview) {
  const services = overview?.services ?? []
  const total = services.length
  const up = services.filter((service) => service.status === 'up').length
  const degraded = services.filter((service) => service.status === 'degraded').length
  const down = services.filter((service) => service.status === 'down').length
  const tone = down > 0 ? 'bad' : degraded > 0 ? 'warn' : 'ok'

  const chip = $('gateway-chip')
  chip.dataset.tone = tone
  $('gateway-chip-dot').className = `mila-dot ${tone}`
  const label = $('gateway-chip-label')
  if (down > 0) label.textContent = `Passerelle · ${down} en panne`
  else if (degraded > 0) label.textContent = `Passerelle · ${degraded} dégradé`
  else label.textContent = `Passerelle · ${total} services`

  const badge = $('tabs').querySelector('[data-badge="services"]')
  if (badge) {
    const unhealthy = degraded + down
    badge.textContent = unhealthy > 0 ? String(unhealthy) : ''
    badge.hidden = unhealthy === 0
    badge.dataset.tone = down > 0 ? 'bad' : 'warn'
  }
}

// ── Login ────────────────────────────────────────────────────────────────────

$('token-reveal').addEventListener('click', () => {
  const input = $('token')
  const reveal = input.type === 'password'
  input.type = reveal ? 'text' : 'password'
  const button = $('token-reveal')
  button.setAttribute('aria-pressed', String(reveal))
  button.title = reveal ? 'Masquer le jeton' : 'Afficher le jeton'
  button.setAttribute('aria-label', reveal ? 'Masquer le jeton' : 'Afficher le jeton')
  input.focus()
})

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault()
  $('login-error').textContent = ''
  const submit = $('login-submit')
  const original = submit.textContent
  submit.disabled = true
  submit.textContent = 'Connexion…'
  try {
    const result = await api('/api/admin/login', {
      method: 'POST',
      body: { token: $('token').value },
    })
    state.user = result.user ?? null
    $('token').value = ''
    setAuthenticated(true)
    if (result.backendVerified === false) {
      // Auth caveat must stay visible until acknowledged, not auto-dismiss.
      toast('Jeton maître validé localement, mais le backend ne l’a pas confirmé (service indisponible ?).', { tone: 'info', timeout: 0 })
    }
  } catch (error) {
    $('login-error').textContent = error.message
  } finally {
    submit.disabled = false
    submit.textContent = original
  }
})

$('logout').addEventListener('click', async () => {
  try {
    await api('/api/admin/logout', { method: 'POST', body: {} })
  } catch {
    /* the cookie list is cleared regardless */
  }
  setAuthenticated(false)
})

// ── Overview ─────────────────────────────────────────────────────────────────

async function renderOverview() {
  const generation = state.authGeneration
  const data = await api('/api/admin/overview')
  if (generation !== state.authGeneration) return
  updateGatewayChip(data)
  const gateway = data.gateway
  const services = data.services ?? []
  const up = services.filter((service) => service.status === 'up').length
  const degraded = services.filter((service) => service.status === 'degraded').length
  const down = services.filter((service) => service.status === 'down').length

  const cards = services.map((service) => {
    const tone = service.status === 'up' ? 'ok' : service.status === 'down' ? 'bad' : 'warn'
    const label = service.status === 'up' ? 'en service' : service.status === 'down' ? 'hors service' : 'dégradé'
    return `
      <div class="mila-service-card">
        <div class="mila-service-top">
          <span class="mila-dot ${tone}"></span>
          <span class="mila-service-name">${esc(service.name)}</span>
          <span class="mila-badge ${tone}">${esc(label)}</span>
        </div>
        <div class="mila-service-meta">
          <span class="mila-service-detail">${esc(service.detail ?? '')}</span>
          ${service.latencyMs != null ? `<span class="mila-service-latency">${esc(service.latencyMs)} ms</span>` : ''}
        </div>
      </div>`
  }).join('')

  const persistence = gateway.persistence ?? {}
  const paths = [
    ['sessions', persistence.sessionsFile],
    ['registre d’instances', persistence.registryDir],
    ['journaux', persistence.logDir],
  ].map(([label, value]) => value ? `
    <div class="mila-path-row">
      <span class="mila-path-key">${esc(label)}</span>
      <code class="mila-path-value">${esc(value)}</code>
    </div>` : '').join('')

  const servicesSummary = down > 0 ? `${down} hors service` : degraded > 0 ? `${degraded} dégradé` : 'tous en service'

  $('tab-overview').innerHTML = `
    ${renderPageHeader('Vue d’ensemble', 'État en direct de la passerelle, de ses services backend et de la persistance.')}
    <div class="mila-stats">
      <div class="mila-stat">
        <span class="mila-stat-label">Démarrée depuis</span>
        <span class="mila-stat-value">${esc(fmtDuration(gateway.uptimeMs))}</span>
        <span class="mila-stat-sub">Port ${esc(gateway.port)} · <code>${esc(gateway.backendUrl)}</code></span>
      </div>
      <div class="mila-stat">
        <span class="mila-stat-label">Sessions</span>
        <span class="mila-stat-value">${esc(gateway.sessions)}</span>
        <span class="mila-stat-sub">navigateurs connectés</span>
      </div>
      <div class="mila-stat">
        <span class="mila-stat-label">Instances</span>
        <span class="mila-stat-value">${esc(gateway.activeInstances)} actives</span>
        <span class="mila-stat-sub">${esc(gateway.knownInstances)} connues</span>
      </div>
      <div class="mila-stat">
        <span class="mila-stat-label">Sessions admin</span>
        <span class="mila-stat-value">${esc(gateway.adminSessions)}</span>
        <span class="mila-stat-sub">opérateurs connectés</span>
      </div>
    </div>
    <div class="mila-section">
      <h3>Services backend <span class="mila-muted">· ${esc(up)}/${esc(services.length)} en service (${esc(servicesSummary)})</span></h3>
      <div class="mila-service-grid">${cards}</div>
    </div>
    <div class="mila-section">
      <h3>Persistance</h3>
      <div class="mila-paths">${paths || '<span class="mila-muted">Aucun chemin de persistance renseigné</span>'}</div>
    </div>`
}

// ── Users & sessions ─────────────────────────────────────────────────────────

async function renderUsers() {
  const generation = state.authGeneration
  const [{ users }, { sessions }] = await Promise.all([
    api('/api/admin/users'),
    api('/api/admin/sessions'),
  ])
  if (generation !== state.authGeneration) return

  const keyCount = users.filter((user) => user.hasKey).length
  const activeSessions = users.reduce((sum, user) => sum + user.activeSessions, 0)

  const userRows = users.map((user) => `
    <tr>
      <td><code class="mila-mono">${esc(user.userId)}</code></td>
      <td>${user.hasKey ? '<span class="mila-badge ok">clé</span>' : '<span class="mila-badge bad">sans clé</span>'}</td>
      <td class="numeric">${user.activeSessions > 0 ? `<span class="mila-badge info">${esc(user.activeSessions)}</span>` : esc(user.activeSessions)}</td>
      <td>${instanceBadge(user.instance)}${user.instance?.port ? ` <span class="mila-muted">:${esc(user.instance.port)}</span>` : ''}</td>
      <td><button class="mila-button secondary small" data-rotate="${esc(user.userId)}">Rotation du mot de passe</button></td>
    </tr>`).join('')

  const sessionRows = sessions.map((session) => {
    const expired = Number(session.expiresAt) <= Date.now()
    return `
    <tr>
      <td><code>${esc(session.id)}</code></td>
      <td>${esc(session.userId)}</td>
      <td>${esc(fmtAgo(session.createdAt))}</td>
      <td>${esc(fmtAgo(session.lastSeenAt))}</td>
      <td class="${expired ? 'mila-muted' : ''}">${esc(fmtSessionExpiry(session.expiresAt))}</td>
      <td><button class="mila-button danger ghost small" data-revoke="${esc(session.id)}" data-user="${esc(session.userId)}">Révoquer</button></td>
    </tr>`
  }).join('')

  $('tab-users').innerHTML = `
    ${renderPageHeader('Utilisateurs & sessions', 'Comptes provisionnés, clés d’API et sessions de passerelle actives.')}
    <div class="mila-stats">
      <div class="mila-stat">
        <span class="mila-stat-label">Utilisateurs</span>
        <span class="mila-stat-value">${esc(users.length)}</span>
        <span class="mila-stat-sub">comptes provisionnés</span>
      </div>
      <div class="mila-stat">
        <span class="mila-stat-label">Clés présentes</span>
        <span class="mila-stat-value">${esc(keyCount)}</span>
        <span class="mila-stat-sub">sur ${esc(users.length)} utilisateurs</span>
      </div>
      <div class="mila-stat">
        <span class="mila-stat-label">Sessions actives</span>
        <span class="mila-stat-value">${esc(activeSessions)}</span>
        <span class="mila-stat-sub">passerelle</span>
      </div>
    </div>
    <div class="mila-section">
      <h3>Utilisateurs</h3>
      <p class="mila-page-sub">Utilisateurs provisionnés dans <code>login-credentials.json</code>. La rotation écrit le nouveau mot de passe dans <code>initial-passwords.txt</code> (jamais affiché ici).</p>
      ${users.length ? `<div class="mila-table-wrap"><table class="mila-table">
        <thead><tr><th>Utilisateur</th><th>Clé d’API</th><th>Sessions actives</th><th>Instance</th><th></th></tr></thead>
        <tbody>${userRows}</tbody>
      </table></div>` : emptyState('Aucun utilisateur provisionné', 'Le fichier login-credentials.json est vide.')}
    </div>
    <div class="mila-section">
      <h3>Sessions de la passerelle</h3>
      ${sessions.length ? `<div class="mila-table-wrap"><table class="mila-table">
        <thead><tr><th>ID (haché)</th><th>Utilisateur</th><th>Créée</th><th>Dernière activité</th><th>Expire</th><th></th></tr></thead>
        <tbody>${sessionRows}</tbody>
      </table></div>` : emptyState('Aucune session active', 'Les navigateurs connectés apparaîtront ici.')}
    </div>`

  for (const button of $('tab-users').querySelectorAll('[data-rotate]')) {
    button.addEventListener('click', async () => {
      const user = button.dataset.rotate
      const choice = await confirmDialog({
        title: 'Rotation du mot de passe',
        message: `Générer un nouveau mot de passe pour ${user} ?`,
        confirmLabel: 'Renouveler',
        danger: false,
      })
      if (!choice.ok) return
      button.disabled = true
      try {
        const result = await api(`/api/admin/users/${encodeURIComponent(user)}/rotate-password`, { method: 'POST', body: {} })
        setError('')
        toast(`Mot de passe de ${user} renouvelé — à récupérer dans ${result.passwordFile}`, { tone: 'success' })
      } catch (error) {
        setError(error.message)
      } finally {
        button.disabled = false
      }
    })
  }
  for (const button of $('tab-users').querySelectorAll('[data-revoke]')) {
    button.addEventListener('click', async () => {
      const id = button.dataset.revoke
      const choice = await confirmDialog({
        title: 'Révoquer la session',
        message: `Révoquer la session de ${button.dataset.user} ?`,
        confirmLabel: 'Révoquer',
        danger: true,
        extraLabel: 'Arrêter aussi son instance',
      })
      if (!choice.ok) return
      button.disabled = true
      try {
        await api(`/api/admin/sessions/${encodeURIComponent(id)}/revoke`, { method: 'POST', body: { stopInstance: choice.extra } })
        await renderUsers()
      } catch (error) {
        setError(error.message)
        button.disabled = false
      }
    })
  }
}

// ── Quotas ───────────────────────────────────────────────────────────────────

const quotaLabels = { concurrency: 'Requêtes simultanées', rpm: 'Requêtes / minute', tpm: 'Tokens / minute', daily_tokens: 'Tokens / jour' }

async function renderQuotas() {
  if (state.quotaDirty || state.quotaLoading || state.quotaSaving) return
  state.quotaLoading = true
  const generation = state.authGeneration
  try {
    const data = await api('/api/admin/quotas')
    if (state.quotaDirty || state.quotaSaving) return
    if (generation !== state.authGeneration) return
    $('tab-quotas').innerHTML = `
      ${renderPageHeader('Quotas', 'Limites partagées dans Valkey, appliquées aux prochaines admissions et requêtes LiteLLM. Les requêtes en cours et les compteurs restent inchangés. Une limite personnalisée s’applique aussi pendant une élévation P1.')}
      <div class="mila-section">
        <p class="mila-page-sub">Usage : requêtes actives de l’agent, tokens consommés et réservés aujourd’hui. Les limites RPM/TPM sont appliquées par LiteLLM ; ses compteurs ne sont pas exposés ici.</p>
        ${data.users.length ? `<div class="quota-grid">${data.users.map((user) => quotaCard(user, data.bounds)).join('')}</div>` : emptyState('Aucun utilisateur avec quotas', 'La passerelle d’authentification n’expose aucun quota individuel.')}
      </div>`
    for (const form of $('tab-quotas').querySelectorAll('form')) {
      form.addEventListener('input', () => {
        form.dataset.dirty = '1'
        state.quotaDirty = true
        form.querySelector('.quota-status').textContent = 'Modifications non enregistrées'
      })
      form.addEventListener('submit', (event) => { event.preventDefault(); saveQuota(form, false) })
      form.querySelector('[data-quota-reset]').addEventListener('click', () => saveQuota(form, true))
    }
  } finally {
    state.quotaLoading = false
  }
}

function quotaCard(user, bounds) {
  const usage = user.usage ?? {}
  const limits = user.limits ?? {}
  const consumed = usage.daily_tokens ?? 0
  const reserved = usage.reserved_tokens ?? 0
  const dailyLimit = limits.daily_tokens ?? 0
  const concurrency = usage.concurrency ?? 0
  const concurrencyLimit = limits.concurrency ?? 0
  return `
    <form class="quota-card" data-quota-user="${esc(user.user_id)}">
      <div class="quota-card-head">
        <h3>${esc(user.user_id)}</h3>
        ${user.p1_elevated ? '<span class="mila-badge warn">P1</span>' : ''}
      </div>
      <p class="quota-card-meta">${esc(user.day)} · ${esc(user.timezone)}</p>
      <div class="quota-meter-block">
        <div class="quota-meter-label">
          <span>Requêtes simultanées</span>
          <span class="numeric">${esc(concurrency)} / ${esc(concurrencyLimit)}</span>
        </div>
        ${usageBar(concurrency, concurrencyLimit)}
      </div>
      <div class="quota-meter-block">
        <div class="quota-meter-label">
          <span>Tokens journaliers</span>
          <span class="numeric">${esc((consumed + reserved).toLocaleString())} / ${esc(dailyLimit.toLocaleString())}</span>
        </div>
        ${usageBar(consumed + reserved, dailyLimit)}
        <p class="mila-help">${esc(consumed.toLocaleString())} consommés · ${esc(reserved.toLocaleString())} réservés</p>
      </div>
      ${Object.entries(quotaLabels).map(([field, label]) => {
        const [min, max] = bounds?.[field] ?? []
        const hint = Number.isFinite(min) && Number.isFinite(max) ? `<span class="mila-field-hint">${esc(min)} – ${esc(max)}</span>` : ''
        return `
        <label>${label}
          <input class="mila-input" type="number" name="${field}" ${Number.isFinite(min) ? `min="${esc(min)}"` : ''} ${Number.isFinite(max) ? `max="${esc(max)}"` : ''} step="1" value="${esc(limits[field])}" required>
          ${hint}
        </label>`
      }).join('')}
      <div class="mila-row">
        <button class="mila-button" type="submit">Enregistrer</button>
        <button class="mila-button secondary" type="button" data-quota-reset>Valeurs par défaut</button>
      </div>
      <p class="mila-muted quota-status" role="status">${Object.keys(user.overrides ?? {}).length ? 'Limites personnalisées' : 'Valeurs par défaut'}</p>
    </form>`
}

async function saveQuota(form, reset) {
  if (state.quotaSaving) return
  const user = form.dataset.quotaUser
  const limits = reset ? {} : Object.fromEntries(Object.keys(quotaLabels).map((field) => [field, Number(form.elements[field].value)]))
  const status = form.querySelector('.quota-status')
  state.quotaSaving = true
  form.querySelectorAll('button, input').forEach((control) => { control.disabled = true })
  status.textContent = 'Enregistrement…'
  try {
    await api(`/api/admin/quotas/${encodeURIComponent(user)}`, { method: 'POST', body: { limits } })
    const fresh = await api('/api/admin/quotas')
    const updated = fresh.users.find((entry) => entry.user_id === user)
    if (!updated) throw new Error('Utilisateur absent après enregistrement')
    for (const field of Object.keys(quotaLabels)) form.elements[field].value = updated.limits[field]
    form.dataset.dirty = '0'
    state.quotaDirty = Boolean($('tab-quotas').querySelector('[data-dirty="1"]'))
    status.textContent = reset ? 'Valeurs par défaut restaurées' : 'Limites enregistrées'
    setError('')
  } catch (error) {
    form.dataset.dirty = '1'
    state.quotaDirty = true
    status.textContent = error.message
  } finally {
    state.quotaSaving = false
    form.querySelectorAll('button, input').forEach((control) => { control.disabled = false })
  }
}

// ── Models ───────────────────────────────────────────────────────────────────

async function renderModels() {
  const generation = state.authGeneration
  const data = await api('/api/admin/models')
  if (generation !== state.authGeneration) return
  const modes = {
    simulated: { label: 'Simulation locale', tone: 'warn', note: 'Le moteur d’inférence simule les réponses ; aucun modèle GPU n’est chargé.' },
    'vllm-proxy': { label: 'Proxy vLLM', tone: 'ok', note: 'Le moteur d’inférence relaie vers un serveur vLLM configuré.' },
    unknown: { label: 'Moteur indisponible', tone: 'bad', note: 'L’état du moteur d’inférence n’a pas pu être confirmé.' },
  }
  const mode = modes[data.inferenceMode] ?? { label: data.inferenceMode, tone: 'warn', note: '' }
  $('tab-models').innerHTML = `
    ${renderPageHeader('Modèles disponibles', `Catalogue publié par ${esc(data.source)}. Ces identifiants sont sélectionnables dans le harnais.`, `<span class="mila-badge ${mode.tone}">${esc(mode.label)}</span>`)}
    <div class="mila-section">
      ${mode.note ? `<div class="mila-callout ${mode.tone}">${esc(mode.note)}</div>` : ''}
      ${data.models.length ? `<div class="mila-table-wrap"><table class="mila-table"><thead><tr><th>Identifiant du modèle</th><th>Propriétaire déclaré</th></tr></thead><tbody>
        ${data.models.map((model) => `<tr><td><code>${esc(model.id)}</code></td><td>${esc(model.owned_by || '—')}</td></tr>`).join('')}
      </tbody></table></div>` : emptyState('Aucun modèle publié', 'LiteLLM n’expose aucun modèle pour le moment.')}
    </div>`
}

// ── Local models ─────────────────────────────────────────────────────────────

const modelStatusLabels = {
  registered: 'enregistré', downloading: 'téléchargement', downloaded: 'téléchargé',
  starting: 'démarrage', running: 'en service', stopped: 'arrêté', error: 'erreur',
}

function modelStatusBadge(status) {
  const cls = status === 'running' || status === 'downloaded' ? 'ok' : status === 'error' ? 'bad' : 'warn'
  return `<span class="mila-badge ${cls}">${esc(modelStatusLabels[status] ?? status)}</span>`
}

// Loading parameters an admin can select per engine, mirroring the model
// manager API. `default: true` marks the engine's own default for a boolean,
// which registration reproduces even when the caller does not choose.
const ENGINE_PARAMS = {
  vllm: [
    { key: 'max_model_len', label: 'Contexte max (tokens)', type: 'number', min: 1, placeholder: 'défaut' },
    { key: 'tensor_parallel_size', label: 'Parallélisme tensoriel', type: 'number', min: 1, placeholder: 'défaut' },
    { key: 'gpu_memory_utilization', label: 'Mémoire GPU (0–1)', type: 'number', min: 0.01, max: 1, step: 0.01, placeholder: 'défaut' },
    { key: 'max_num_seqs', label: 'Séquences simultanées', type: 'number', min: 1, placeholder: 'défaut' },
    { key: 'dtype', label: 'Précision (dtype)', type: 'select', options: ['auto', 'half', 'float16', 'bfloat16', 'float', 'float32'] },
    { key: 'kv_cache_dtype', label: 'Cache KV (dtype)', type: 'select', options: ['auto', 'fp8', 'fp8_e5m2', 'fp8_e4m3', 'fp8_inc', 'fp8_ds'] },
    { key: 'quantization', label: 'Quantization', type: 'text', placeholder: 'ex. awq, gptq, fp8' },
    { key: 'enforce_eager', label: 'Mode eager (sans graph CUDA)', type: 'bool' },
    { key: 'enable_prefix_caching', label: 'Cache de préfixes', type: 'bool' },
  ],
  llamacpp: [
    { key: 'ctx_size', label: 'Contexte (tokens)', type: 'number', min: 512, max: 131072, placeholder: '2048' },
    { key: 'n_gpu_layers', label: 'Couches GPU (« all » ou entier)', type: 'text', placeholder: 'all' },
    { key: 'threads', label: 'Threads CPU', type: 'number', min: 1, placeholder: 'défaut' },
    { key: 'batch_size', label: 'Taille de lot', type: 'number', min: 1, placeholder: 'défaut' },
    { key: 'flash_attn', label: 'Attention flash', type: 'bool', default: true },
    { key: 'mmap', label: 'Mappage mémoire (mmap)', type: 'bool', default: true },
    { key: 'mlock', label: 'Verrouillage RAM (mlock)', type: 'bool', default: false },
  ],
}

function modelParamsHtml(engine, values = {}) {
  return (ENGINE_PARAMS[engine] ?? []).map((field) => {
    const value = values[field.key]
    if (field.type === 'bool') {
      const checked = value === undefined ? field.default === true : Boolean(value)
      return `<label class="mila-check"><input type="checkbox" name="${esc(field.key)}" ${checked ? 'checked' : ''}> ${esc(field.label)}</label>`
    }
    if (field.type === 'select') {
      const options = field.options.map((option) =>
        `<option value="${esc(option)}" ${value === option ? 'selected' : ''}>${esc(option)}</option>`).join('')
      return `<label>${esc(field.label)}<select class="mila-input" name="${esc(field.key)}"><option value="">— défaut —</option>${options}</select></label>`
    }
    const attrs = [
      `type="${field.type}"`,
      `name="${esc(field.key)}"`,
      field.min !== undefined ? `min="${field.min}"` : '',
      field.max !== undefined ? `max="${field.max}"` : '',
      field.step !== undefined ? `step="${field.step}"` : '',
      `placeholder="${esc(field.placeholder ?? '')}"`,
    ].join(' ')
    return `<label>${esc(field.label)}<input class="mila-input" ${attrs} ${value !== undefined && value !== null ? `value="${esc(value)}"` : ''}></label>`
  }).join('')
}

function readModelParams(form, engine, { register = false } = {}) {
  const body = {}
  for (const field of ENGINE_PARAMS[engine] ?? []) {
    const input = form.elements[field.key]
    if (!input) continue
    if (field.type === 'bool') {
      // At registration a vLLM boolean without an API default is only sent
      // when explicitly ticked, so it never silently overrides the operator
      // config file. llama.cpp booleans always mirror their API defaults.
      if (register && field.default === undefined && !input.checked) continue
      body[field.key] = input.checked
      continue
    }
    const raw = String(input.value ?? '').trim()
    if (raw === '') {
      body[field.key] = null
      continue
    }
    if (field.key === 'n_gpu_layers' && /^\d+$/.test(raw)) {
      body[field.key] = Number(raw)
      continue
    }
    if (field.type === 'number') {
      const parsed = Number(raw)
      if (!Number.isFinite(parsed)) continue
      body[field.key] = parsed
      continue
    }
    body[field.key] = raw
  }
  return body
}

function diffModelParams(current, initial) {
  const body = {}
  for (const key of new Set([...Object.keys(initial), ...Object.keys(current)])) {
    if (JSON.stringify(current[key] ?? null) !== JSON.stringify(initial[key] ?? null)) {
      body[key] = current[key] ?? null
    }
  }
  return body
}

async function renderLocalModels() {
  if (state.localModelsDirty || state.localModelsBusy) return
  const generation = state.authGeneration
  const data = await api('/api/admin/local-models')
  if (generation !== state.authGeneration) return
  const models = data.models ?? []

  const attention = models.filter((model) => model.status === 'error' || model.status === 'downloading').length
  const attentionTone = models.some((model) => model.status === 'error') ? 'bad' : 'warn'
  updateNavBadge('local-models', attention, attention > 0 ? attentionTone : null)

  const rows = models.map((model) => {
    const server = model.server ?? {}
    const running = model.status === 'running'
    const started = ['downloaded', 'stopped', 'running', 'error'].includes(model.status)
    return `<tr>
      <td><code>${esc(model.name)}</code><br><span class="mila-muted">${esc(model.hf_repo)}${model.revision ? `@${esc(model.revision)}` : ''}</span></td>
      <td>${modelStatusBadge(model.status)}${model.last_error ? `<br><span class="mila-muted">${esc(model.last_error)}</span>` : ''}</td>
      <td class="numeric">${esc(fmtBytes(model.size_bytes))}</td>
      <td>${server.port ? `<span class="mila-muted">:${esc(server.port)} · pid ${esc(server.pid ?? '—')}</span>` : '—'}</td>
      <td>
        <div class="mila-row">
          ${['registered', 'error'].includes(model.status) ? `<button class="mila-button secondary small" data-model="${esc(model.name)}" data-action="download">Télécharger</button>` : ''}
          ${started && !running ? `<button class="mila-button small" data-model="${esc(model.name)}" data-action="start">Démarrer</button>` : ''}
          ${running ? `<button class="mila-button secondary small" data-model="${esc(model.name)}" data-action="restart">Redémarrer</button>` : ''}
          ${running ? `<button class="mila-button danger small" data-model="${esc(model.name)}" data-action="stop">Arrêter</button>` : ''}
          <button class="mila-button ghost small" data-model="${esc(model.name)}" data-action="logs">Journal</button>
          ${!running && model.status !== 'starting' && model.status !== 'downloading' ? `<button class="mila-button ghost small" data-model="${esc(model.name)}" data-action="params">Paramètres</button>` : ''}
          ${!running && model.status !== 'starting' ? `<button class="mila-button danger ghost small" data-model="${esc(model.name)}" data-action="delete">Supprimer</button>` : ''}
        </div>
      </td></tr>`
  }).join('')

  $('tab-local-models').innerHTML = `
    ${renderPageHeader('Modèles locaux', 'Enregistrer un dépôt HuggingFace, le télécharger, puis démarrer un serveur vLLM local. Un modèle n’est sélectionnable qu’une fois <em>en service</em>.')}
    <div class="mila-section">
      <form id="local-model-form" class="quota-card">
        <div class="quota-card-head"><h3>Enregistrer un modèle</h3></div>
        <label>Dépôt HuggingFace ou lien<input class="mila-input" name="hf_repo" placeholder="org/model ou https://huggingface.co/org/model" required></label>
        <div class="mila-grid-2">
          <label>Nom du service (optionnel)<input class="mila-input" name="name" placeholder="déduit du dépôt"></label>
          <label>Révision (optionnel)<input class="mila-input" name="revision" placeholder="main"></label>
        </div>
        <div class="mila-grid-2">
          <label>Moteur
            <select class="mila-input" name="engine">
              <option value="vllm">vLLM</option>
              <option value="llamacpp">llama.cpp (GGUF)</option>
            </select>
          </label>
          <label id="gguf-field" hidden>Fichier GGUF<input class="mila-input" name="gguf_file" placeholder="model.Q6_K.gguf"></label>
        </div>
        <label class="mila-check"><input type="checkbox" id="register-params-toggle"> Paramètres de chargement avancés</label>
        <div id="register-params" class="mila-grid-2" hidden></div>
        <p class="mila-muted" id="register-params-hint" hidden>Les champs vides laissent le moteur ou la configuration opérateur décider. Vous pourrez ajuster ces paramètres plus tard avec le bouton « Paramètres ».</p>
        <button class="mila-button" type="submit">Enregistrer</button>
      </form>
      <div id="local-model-log" class="mila-log-card" hidden></div>
      ${models.length ? `<div class="mila-table-wrap"><table class="mila-table"><thead><tr><th>Modèle</th><th>État</th><th>Taille</th><th>Serveur</th><th>Actions</th></tr></thead><tbody>
        ${rows}
      </tbody></table></div>` : emptyState('Aucun modèle local', 'Enregistrez un dépôt HuggingFace pour commencer.')}
    </div>`

  $('local-model-form').addEventListener('input', () => { state.localModelsDirty = true })

  const registerEngine = () => {
    const engine = $('local-model-form').elements.engine.value
    $('gguf-field').hidden = engine !== 'llamacpp'
    $('register-params').innerHTML = modelParamsHtml(engine)
  }
  registerEngine()
  $('local-model-form').elements.engine.addEventListener('change', registerEngine)
  $('register-params-toggle').addEventListener('change', () => {
    const show = $('register-params-toggle').checked
    $('register-params').hidden = !show
    $('register-params-hint').hidden = !show
  })

  $('local-model-form').addEventListener('submit', async (event) => {
    event.preventDefault()
    const form = event.target
    const engine = form.elements.engine.value
    const body = {
      hf_repo: form.elements.hf_repo.value,
      name: form.elements.name.value || undefined,
      revision: form.elements.revision.value || undefined,
      engine,
    }
    if (engine === 'llamacpp') {
      body.gguf_file = form.elements.gguf_file.value.trim() || undefined
    }
    for (const [key, value] of Object.entries(readModelParams(form, engine, { register: true }))) {
      if (value !== null) body[key] = value
    }
    state.localModelsBusy = true
    try {
      await api('/api/admin/local-models', { method: 'POST', body })
      state.localModelsDirty = false
      setError('')
    } catch (error) {
      setError(error.message)
    } finally {
      state.localModelsBusy = false
      renderLocalModels().catch((error) => setError(error.message))
    }
  })

  for (const button of $('tab-local-models').querySelectorAll('[data-model]')) {
    button.addEventListener('click', () => {
      if (button.dataset.action === 'params') {
        const model = models.find((entry) => entry.name === button.dataset.model)
        if (model) openModelParamsDialog(model)
        return
      }
      runModelAction(button.dataset.model, button.dataset.action)
    })
  }
}

async function runModelAction(name, action) {
  if (action === 'delete') {
    const choice = await confirmDialog({
      title: 'Supprimer le modèle',
      message: `Supprimer le modèle ${name} ? Les fichiers téléchargés seront effacés.`,
      confirmLabel: 'Supprimer',
      danger: true,
    })
    if (!choice.ok) return
  }
  if (action === 'logs') {
    try {
      const data = await api(`/api/admin/local-models/${encodeURIComponent(name)}/logs`)
      const view = $('local-model-log')
      view.hidden = false
      view.innerHTML = `<h3>Journal vLLM — ${esc(name)}</h3><pre class="mila-log">${esc(data.output || '(vide)')}</pre>`
      setError('')
    } catch (error) { setError(error.message) }
    return
  }
  state.localModelsBusy = true
  try {
    const query = action === 'delete' ? '?delete_files=1' : ''
    await api(`/api/admin/local-models/${encodeURIComponent(name)}/${action}${query}`, { method: 'POST', body: {} })
    setError('')
  } catch (error) {
    setError(error.message)
  } finally {
    state.localModelsBusy = false
    renderLocalModels().catch((error) => setError(error.message))
  }
}

async function openModelParamsDialog(model) {
  const dialog = $('model-params-dialog')
  if (dialog.open) return
  const engine = model.engine === 'llamacpp' ? 'llamacpp' : 'vllm'
  const values = {}
  for (const field of ENGINE_PARAMS[engine] ?? []) {
    const raw = model[field.key]
    values[field.key] = field.type === 'bool'
      ? (raw === undefined ? field.default === true : Boolean(raw))
      : (raw ?? null)
  }
  $('model-params-title').textContent = `Paramètres de chargement — ${model.name}`
  $('model-params-engine').textContent = engine === 'llamacpp' ? 'llama.cpp (GGUF)' : 'vLLM'
  $('model-params-fields').innerHTML = modelParamsHtml(engine, values)
  dialog._initial = values
  dialog._engine = engine
  dialog._name = model.name
  dialog.showModal()
}

function bindModelParamsDialog() {
  const dialog = $('model-params-dialog')
  $('model-params-cancel').addEventListener('click', () => dialog.close())
  $('model-params-form').addEventListener('submit', async (event) => {
    event.preventDefault()
    const current = readModelParams($('model-params-form'), dialog._engine)
    const body = diffModelParams(current, dialog._initial ?? {})
    if (Object.keys(body).length === 0) {
      dialog.close()
      return
    }
    state.localModelsBusy = true
    try {
      await api(`/api/admin/local-models/${encodeURIComponent(dialog._name)}`, { method: 'PATCH', body })
      setError('')
      dialog.close()
    } catch (error) {
      setError(error.message)
    } finally {
      state.localModelsBusy = false
      renderLocalModels().catch((error) => setError(error.message))
    }
  })
}

// ── Hardware survey ──────────────────────────────────────────────────────────

async function renderHardware(refresh = false) {
  const generation = state.authGeneration
  const data = await api(`/api/admin/survey${refresh ? '?refresh=1' : ''}`)
  if (generation !== state.authGeneration) return
  const gpus = data.gpus ?? []
  const pci = data.pci_accelerators ?? []
  const drivers = data.driver_stack ?? {}
  const nvidia = drivers.nvidia ?? {}
  const versions = data.software_versions ?? {}
  const storage = data.model_storage ?? {}

  const totalVram = gpus.reduce((sum, gpu) => sum + (Number(gpu.vram_total_mb) || 0), 0)
  const freeVram = gpus.reduce((sum, gpu) => sum + (Number(gpu.vram_free_mb) || 0), 0)

  const gpuRows = gpus.map((gpu) => `<tr><td class="numeric">${esc(gpu.index)}</td><td>${esc(gpu.name)}</td><td>${esc(gpu.architecture)} ${esc(gpu.compute_capability)}</td><td class="numeric">${esc(gpu.vram_total_mb)} Mo</td><td class="numeric">${esc(gpu.vram_free_mb)} Mo</td><td><code>${esc(gpu.driver_version)}</code></td></tr>`).join('')
  const pciRows = pci.map((device) => `<tr><td><code>${esc(device.slot)}</code></td><td>${esc(device.description)}</td><td>${esc(device.vendor_device_ids ?? '—')}</td><td>${esc(device.kernel_driver ?? '—')}</td></tr>`).join('')

  const versionChips = [
    ['Python', versions.python],
    ['torch', versions.torch],
    ['vLLM', versions.vllm],
    ['huggingface_hub', versions.huggingface_hub],
    ['litellm', versions.litellm],
  ].filter(([, value]) => value)
    .map(([label, value]) => `<span class="mila-chip">${esc(label)} <code>${esc(value)}</code></span>`)
    .join('')

  const storageUsed = (Number(storage.total_bytes) || 0) - (Number(storage.free_bytes) || 0)

  $('tab-hardware').innerHTML = `
    ${renderPageHeader('Matériel & pilotes', `Relevé ${esc(data.survey_timestamp ?? '')} · hôte ${esc(data.hostname ?? '')}. Mis en cache 10 s ; actualiser pour relancer les sondes.`, '<button class="mila-button secondary" id="survey-refresh" type="button">Actualiser</button>')}
    <div class="mila-section">
      <div class="mila-stats">
        <div class="mila-stat">
          <span class="mila-stat-label">GPU NVIDIA</span>
          <span class="mila-stat-value">${esc(gpus.length)}</span>
          <span class="mila-stat-sub">accélérateurs détectés</span>
        </div>
        <div class="mila-stat">
          <span class="mila-stat-label">VRAM libre</span>
          <span class="mila-stat-value">${esc(fmtBytes(freeVram * 1024 * 1024))}</span>
          <span class="mila-stat-sub">sur ${esc(fmtBytes(totalVram * 1024 * 1024))}</span>
        </div>
        <div class="mila-stat">
          <span class="mila-stat-label">CUDA runtime</span>
          <span class="mila-stat-value">${esc(nvidia.cuda_runtime ?? '—')}</span>
          <span class="mila-stat-sub">pilote ${esc(nvidia.smi_driver_version ?? '—')}</span>
        </div>
        <div class="mila-stat">
          <span class="mila-stat-label">Noyau</span>
          <span class="mila-stat-value">${esc(drivers.kernel_release ?? '—')}</span>
          <span class="mila-stat-sub">modules ${esc((drivers.loaded_modules ?? []).join(', ') || '—')}</span>
        </div>
      </div>

      <h3>Accélérateurs NVIDIA</h3>
      ${gpus.length ? `<div class="mila-table-wrap"><table class="mila-table"><thead><tr><th>#</th><th>Nom</th><th>Architecture</th><th>VRAM</th><th>Libre</th><th>Pilote</th></tr></thead><tbody>${gpuRows}</tbody></table></div>` : emptyState('Aucun GPU NVIDIA détecté', 'nvidia-smi n’a trouvé aucun accélérateur.')}

      <h3>Périphériques PCI</h3>
      ${pci.length ? `<div class="mila-table-wrap"><table class="mila-table"><thead><tr><th>Emplacement</th><th>Description</th><th>IDs</th><th>Pilote noyau</th></tr></thead><tbody>${pciRows}</tbody></table></div>` : emptyState('Aucun accélérateur PCI', 'Aucun périphérique accélérateur détecté sur le bus PCI.')}

      <h3>Versions logicielles</h3>
      ${versionChips ? `<div class="mila-chip-row">${versionChips}</div>` : '<span class="mila-muted">Aucune version relevée.</span>'}

      <h3>Stockage des modèles</h3>
      <div class="mila-storage">
        <div class="quota-meter-label">
          <span><code>${esc(storage.path ?? '—')}</code></span>
          <span class="numeric">${esc(fmtBytes(storage.free_bytes))} libres / ${esc(fmtBytes(storage.total_bytes))}</span>
        </div>
        ${usageBar(storageUsed, storage.total_bytes)}
      </div>
      ${(storage.entries ?? []).length ? `<div class="mila-table-wrap"><table class="mila-table"><thead><tr><th>Répertoire</th><th>Taille</th></tr></thead><tbody>${(storage.entries ?? []).map((entry) => `<tr><td><code>${esc(entry.name)}</code></td><td class="numeric">${esc(fmtBytes(entry.size_bytes))}</td></tr>`).join('')}</tbody></table></div>` : ''}
    </div>`
  $('survey-refresh').addEventListener('click', () => renderHardware(true).catch((error) => setError(error.message)))
}

// ── Instances ────────────────────────────────────────────────────────────────

async function renderInstances() {
  const generation = state.authGeneration
  const [{ instances: known }, { users }] = await Promise.all([api('/api/admin/instances'), api('/api/admin/users')])
  if (generation !== state.authGeneration) return
  const instances = [...known, ...users.filter((user) => !known.some((instance) => instance.userId === user.userId))
    .map((user) => ({ userId: user.userId, state: 'stopped', restarts: 0 }))]

  updateNavBadge('instances', instances.filter((instance) => instance.state === 'failed').length, 'bad')

  const rows = instances.map((instance) => {
    const running = instance.state === 'running'
    const restartable = ['running', 'failed', 'starting'].includes(instance.state)
    const hasStartedAt = Number.isFinite(Number(instance.startedAt)) && instance.startedAt > 0
    return `
    <tr>
      <td><code class="mila-mono">${esc(instance.userId)}</code></td>
      <td>${instanceBadge(instance)}${instance.port ? ` <span class="mila-muted">:${esc(instance.port)}</span>` : ''}</td>
      <td>${hasStartedAt ? esc(fmtAgo(instance.startedAt)) : '—'}</td>
      <td class="numeric">${instance.restarts > 0 ? `<span class="mila-badge warn">${esc(instance.restarts)}</span>` : esc(instance.restarts ?? 0)}</td>
      <td class="mila-failure-cell">${instance.failureReason ? esc(instance.failureReason) : '<span class="mila-muted">—</span>'}</td>
      <td>
        <div class="mila-row">
          <button class="mila-button secondary small" data-action="start" data-user="${esc(instance.userId)}" ${running ? 'disabled' : ''}>Démarrer</button>
          <button class="mila-button secondary small" data-action="restart" data-user="${esc(instance.userId)}" ${restartable ? '' : 'disabled'}>Redémarrer</button>
          <button class="mila-button danger small" data-action="stop" data-user="${esc(instance.userId)}" ${running ? '' : 'disabled'}>Arrêter</button>
          <button class="mila-button ghost small" data-logs="${esc(instance.userId)}">Journaux</button>
        </div>
      </td>
    </tr>`
  }).join('')

  $('tab-instances').innerHTML = `
    ${renderPageHeader('Runtimes', 'Une instance dsh par utilisateur connecté. Les journaux sont limités aux 32 derniers Kio par défaut et tournent à 2 Mio.')}
    <div class="mila-section">
      ${instances.length ? `<div class="mila-table-wrap"><table class="mila-table">
        <thead><tr><th>Utilisateur</th><th>État</th><th>Démarrée</th><th>Redémarrages</th><th>Dernière erreur</th><th>Actions</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>` : emptyState('Aucune instance', 'Les utilisateurs démarrent une instance à la première connexion.')}
    </div>
    <div class="mila-section" id="log-section" ${state.logUser ? '' : 'hidden'}>
      <div class="mila-log-toolbar">
        <h3>Journaux — <span id="log-user"></span></h3>
        <button class="mila-button ghost small" id="log-wrap" type="button" aria-pressed="${state.logNowrap ? 'true' : 'false'}">${state.logNowrap ? 'Activer le retour à la ligne' : 'Désactiver le retour à la ligne'}</button>
        <button class="mila-button secondary small" id="log-refresh">Rafraîchir</button>
        <button class="mila-button ghost small" id="log-close">Fermer</button>
      </div>
      <pre class="mila-log${state.logNowrap ? ' nowrap' : ''}" id="log-output"></pre>
    </div>`

  for (const button of $('tab-instances').querySelectorAll('[data-action]')) {
    button.addEventListener('click', async () => {
      button.disabled = true
      try {
        await api(`/api/admin/instances/${encodeURIComponent(button.dataset.user)}/${button.dataset.action}`, { method: 'POST', body: {} })
        setError('')
        await renderInstances()
      } catch (error) {
        setError(error.message)
        button.disabled = false
      }
    })
  }
  for (const button of $('tab-instances').querySelectorAll('[data-logs]')) {
    button.addEventListener('click', () => openLogs(button.dataset.logs))
  }
  $('log-refresh')?.addEventListener('click', () => loadLogs(true))
  $('log-close')?.addEventListener('click', () => stopLogPolling(true))
  $('log-wrap')?.addEventListener('click', () => {
    state.logNowrap = !state.logNowrap
    const output = $('log-output')
    output.classList.toggle('nowrap', state.logNowrap)
    const button = $('log-wrap')
    button.setAttribute('aria-pressed', String(state.logNowrap))
    button.textContent = state.logNowrap ? 'Activer le retour à la ligne' : 'Désactiver le retour à la ligne'
  })
  if (state.logUser) loadLogs(false)
}

async function openLogs(userId) {
  state.logUser = userId
  $('log-section').hidden = false
  $('log-user').textContent = userId
  await loadLogs(true)
  stopLogPolling()
  state.logTimer = setInterval(() => {
    if (document.visibilityState === 'visible') loadLogs(false)
  }, 5000)
}

function stopLogPolling(hide = false) {
  if (state.logTimer) clearInterval(state.logTimer)
  state.logTimer = null
  if (hide) {
    state.logUser = null
    const section = $('log-section')
    if (section) section.hidden = true
  }
}

async function loadLogs(scroll) {
  if (!state.logUser) return
  try {
    const response = await fetch(`/api/admin/instances/${encodeURIComponent(state.logUser)}/logs?tail=32768`, {
      headers: { 'x-sysadmin-admin': '1' },
    })
    const text = await response.text()
    const output = $('log-output')
    if (!output) return
    const user = $('log-user')
    if (user) user.textContent = state.logUser
    output.textContent = text
    if (scroll) output.scrollTop = output.scrollHeight
  } catch (error) {
    setError(error.message)
  }
}

// ── Approvals ────────────────────────────────────────────────────────────────

async function renderApprovals() {
  const generation = state.authGeneration
  const data = await api('/api/admin/approvals')
  if (generation !== state.authGeneration) return
  const approvals = data.pending_approvals ?? []

  updateNavBadge('approvals', approvals.length, approvals.length ? 'warn' : null)

  const rows = approvals.map((approval) => {
    const expiresMs = (approval.expires_at ?? 0) * 1000
    return `
    <tr>
      <td><code class="mila-mono">${esc(approval.approval_id)}</code></td>
      <td>${esc(approval.user_id)}<br><span class="mila-muted">session ${esc(approval.session_id)}</span></td>
      <td><pre class="mila-code-block">${esc(approval.command)}</pre></td>
      <td>${esc(approval.reason)}</td>
      <td class="mila-countdown" data-countdown="${esc(expiresMs)}">${esc(fmtCountdown(expiresMs))}</td>
      <td>
        <div class="mila-row">
          <button class="mila-button small" data-decide="approved" data-id="${esc(approval.approval_id)}">Approuver</button>
          <button class="mila-button danger small" data-decide="rejected" data-id="${esc(approval.approval_id)}">Rejeter</button>
        </div>
      </td>
    </tr>`
  }).join('')

  $('tab-approvals').innerHTML = `
    ${renderPageHeader('Approbations en attente', 'Décisions transmises au backend avec le jeton maître ; store partagé requis (Valkey).')}
    <div class="mila-section">
      ${approvals.length ? `<div class="mila-table-wrap"><table class="mila-table">
        <thead><tr><th>ID</th><th>Utilisateur</th><th>Commande</th><th>Raison</th><th>Expire</th><th>Décision</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>` : emptyState('Aucune approbation en attente', 'Les commandes destructives en attente de validation apparaîtront ici.')}
    </div>`
  for (const button of $('tab-approvals').querySelectorAll('[data-decide]')) {
    button.addEventListener('click', async () => {
      button.disabled = true
      try {
        await api('/api/admin/approvals/decide', {
          method: 'POST',
          body: { approval_id: button.dataset.id, approved: button.dataset.decide === 'approved' },
        })
        await renderApprovals()
      } catch (error) {
        setError(error.message)
        button.disabled = false
      }
    })
  }
}

// ── Audit ────────────────────────────────────────────────────────────────────

const AUDIT_CHIPS = ['*', 'action:approval_required', 'action:policy_blocked', 'user_id:sysadmin-01']

$('tab-audit').innerHTML = `
  ${renderPageHeader('Flux d’audit (VictoriaLogs)', 'Interrogez le journal structuré des actions de la plateforme. Les événements restent dans la file locale si le collecteur est indisponible.')}
  <div class="mila-section">
    <div class="mila-row">
      <input class="mila-input mila-audit-query" id="audit-query" value="*" placeholder="Requête LogsQL, ex: user_id:sysadmin-01">
      <button class="mila-button" id="audit-run">Rechercher</button>
      <span class="mila-muted" id="audit-status"></span>
    </div>
    <div class="mila-chip-row">
      ${AUDIT_CHIPS.map((chip) => `<button class="mila-chip" type="button" data-audit-chip="${esc(chip)}">${esc(chip)}</button>`).join('')}
    </div>
    <div id="audit-results" class="mila-audit-results"></div>
  </div>`

$('audit-run').addEventListener('click', runAudit)
$('audit-query').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') runAudit()
})
for (const chip of $('tab-audit').querySelectorAll('[data-audit-chip]')) {
  chip.addEventListener('click', () => {
    $('audit-query').value = chip.dataset.auditChip
    runAudit()
  })
}

async function runAudit() {
  const query = $('audit-query').value.trim() || '*'
  $('audit-status').textContent = 'Recherche…'
  const generation = state.authGeneration
  try {
    const data = await api(`/api/admin/audit?query=${encodeURIComponent(query)}&limit=100`)
    if (generation !== state.authGeneration) return
    $('audit-status').textContent = `${data.count} événement(s)`
    const keys = ['_time', 'user_id', 'session_id', 'action', 'tool_name', 'cmd']
    const rows = data.events.map((event) => {
      const raw = JSON.stringify(event, null, 2) ?? ''
      return `
      <tr class="mila-audit-row" tabindex="0" role="button" aria-expanded="false" data-audit-row>
        ${keys.map((key) => `<td>${esc(event[key] ?? '')}</td>`).join('')}
      </tr>
      <tr class="mila-audit-detail" hidden><td colspan="${keys.length}"><pre class="mila-json">${esc(raw)}</pre></td></tr>`
    }).join('')
    $('audit-results').innerHTML = `
      ${data.events.length ? `<div class="mila-table-wrap"><table class="mila-table">
        <thead><tr>${keys.map((key) => `<th>${esc(key)}</th>`).join('')}</tr></thead>
        <tbody>${rows}</tbody>
      </table></div>` : emptyState('Aucun événement', 'Aucune entrée ne correspond à cette requête.')}`
    setError('')
    for (const row of $('audit-results').querySelectorAll('[data-audit-row]')) {
      row.addEventListener('click', () => {
        const detail = row.nextElementSibling
        const expand = detail.hidden
        detail.hidden = !expand
        row.setAttribute('aria-expanded', String(expand))
      })
      row.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          row.click()
        }
      })
    }
  } catch (error) {
    if (generation !== state.authGeneration) return
    $('audit-status').textContent = ''
    setError(error.message)
  }
}

// ── Services ─────────────────────────────────────────────────────────────────

async function renderServices() {
  const generation = state.authGeneration
  const { services } = await api('/api/admin/services')
  if (generation !== state.authGeneration) return
  const rows = services.map((service) => `
    <tr>
      <td><span class="mila-state"><span class="mila-dot ${service.running ? 'ok' : 'bad'}"></span><code class="mila-mono">${esc(service.name)}</code></span></td>
      <td>${service.running ? '<span class="mila-badge ok">actif</span>' : '<span class="mila-badge bad">arrêté</span>'}</td>
      <td class="numeric">${esc(service.port ?? '—')}</td>
      <td class="numeric">${esc(service.pid ?? '—')}</td>
      <td>${service.name === 'harness_gateway' ? '<span class="mila-muted">Cette console · gestion depuis l’hôte</span>' : `
        <div class="mila-row">
          <button class="mila-button secondary small" data-service="${esc(service.name)}" data-action="start" ${service.running ? 'disabled' : ''}>Démarrer</button>
          <button class="mila-button secondary small" data-service="${esc(service.name)}" data-action="restart" ${service.running ? '' : 'disabled'}>Redémarrer</button>
          <button class="mila-button danger small" data-service="${esc(service.name)}" data-action="stop" ${service.running ? '' : 'disabled'}>Arrêter</button>
        </div>`}</td>
    </tr>`).join('')
  $('tab-services').innerHTML = `
    ${renderPageHeader('Services', 'Contrôle via <code>platform.sh service</code>, sérialisé par service, délai maximum 30 s. Arrêter un service interrompt ses requêtes en cours.')}
    <div class="mila-section">
      <div class="mila-table-wrap"><table class="mila-table">
        <thead><tr><th>Service</th><th>État</th><th>Port</th><th>PID</th><th>Action</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      <pre class="mila-log" id="service-output" ${state.serviceOutput ? '' : 'hidden'}>${esc(state.serviceOutput)}</pre>
    </div>`
  for (const button of $('tab-services').querySelectorAll('[data-service]')) {
    button.addEventListener('click', async () => {
      const name = button.dataset.service
      const action = button.dataset.action
      if (action !== 'start') {
        const choice = await confirmDialog({
          title: action === 'stop' ? 'Arrêter le service' : 'Redémarrer le service',
          message: `${action === 'stop' ? 'Arrêter' : 'Redémarrer'} le service ${name} ?`,
          confirmLabel: action === 'stop' ? 'Arrêter' : 'Redémarrer',
          danger: action === 'stop',
        })
        if (!choice.ok) return
      }
      button.disabled = true
      state.serviceOutput = `${action} ${name}…`
      $('service-output').hidden = false
      $('service-output').textContent = state.serviceOutput
      try {
        const result = await api(`/api/admin/services/${encodeURIComponent(name)}/${action}`, { method: 'POST', body: {} })
        state.serviceOutput = result.output || 'OK'
        setError('')
      } catch (error) {
        state.serviceOutput = error.message
        setError(error.message)
      } finally {
        button.disabled = false
        await renderServices()
      }
    })
  }
}

// ── Renderer registry ────────────────────────────────────────────────────────

RENDERERS.overview = renderOverview
RENDERERS.users = renderUsers
RENDERERS.quotas = renderQuotas
RENDERERS.instances = renderInstances
RENDERERS.models = renderModels
RENDERERS['local-models'] = renderLocalModels
RENDERERS.approvals = renderApprovals
RENDERERS.services = renderServices
RENDERERS.hardware = renderHardware

// ── Boot ─────────────────────────────────────────────────────────────────────

async function boot() {
  // Normalize the initial hash before the first render so deep links work and
  // a bare load lands on #overview.
  bindModelParamsDialog()
  if (!TABS.includes(location.hash.slice(1))) {
    history.replaceState(null, '', '#overview')
  }
  state.activeTab = currentHashTab()
  syncTabUI()
  try {
    const session = await api('/api/admin/session')
    state.user = session.user ?? null
    setAuthenticated(Boolean(session.authenticated))
    if (session.authenticated) runAudit()
  } catch {
    setAuthenticated(false)
  }
}

boot()
