/* Admin console for the Mila sysadmin harness gateway. Vanilla JS, no build step. */
'use strict'

const state = {
  authenticated: false,
  activeTab: 'overview',
  pollTimer: null,
  logUser: null,
  logTimer: null,
  serviceOutput: '',
}

const $ = (id) => document.getElementById(id)

function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
}

function fmtTime(ms) {
  if (!ms) return '—'
  try {
    return new Date(Number(ms)).toLocaleString('fr-CA', { hour12: false })
  } catch {
    return String(ms)
  }
}

function fmtDuration(ms) {
  if (!Number.isFinite(ms)) return '—'
  const seconds = Math.floor(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
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

function setError(message) {
  $('console-error').textContent = message ?? ''
}

function setAuthenticated(authenticated) {
  state.authenticated = authenticated
  $('login-view').hidden = authenticated
  $('console-view').hidden = !authenticated
  $('logout').hidden = !authenticated
  $('admin-user').textContent = authenticated ? 'sysadmin-admin' : ''
  if (authenticated) {
    startPolling()
  } else {
    stopPolling()
    stopLogPolling()
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

function refreshActive() {
  const tab = state.activeTab
  const render = {
    overview: renderOverview,
    users: renderUsers,
    instances: renderInstances,
    approvals: renderApprovals,
    audit: () => {},
    services: renderServices,
  }[tab]
  if (render) render().catch((error) => setError(error.message))
}

// ── Login ───────────────────────────────────────────────────────────────────

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault()
  $('login-error').textContent = ''
  $('login-submit').disabled = true
  try {
    const result = await api('/api/admin/login', {
      method: 'POST',
      body: { token: $('token').value },
    })
    $('token').value = ''
    setAuthenticated(true)
    if (result.backendVerified === false) {
      setError('Jeton maître validé localement, mais le backend ne l\'a pas confirmé (service indisponible ?).')
    }
  } catch (error) {
    $('login-error').textContent = error.message
  } finally {
    $('login-submit').disabled = false
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

// ── Tabs ────────────────────────────────────────────────────────────────────

$('tabs').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-tab]')
  if (!button) return
  state.activeTab = button.dataset.tab
  for (const tabButton of $('tabs').querySelectorAll('button')) {
    tabButton.classList.toggle('active', tabButton === button)
  }
  for (const name of ['overview', 'users', 'instances', 'approvals', 'audit', 'services']) {
    $(`tab-${name}`).hidden = name !== state.activeTab
  }
  setError('')
  if (state.activeTab !== 'instances') stopLogPolling()
  refreshActive()
})

// ── Overview ────────────────────────────────────────────────────────────────

async function renderOverview() {
  const data = await api('/api/admin/overview')
  const gateway = data.gateway
  const cards = data.services.map((service) => `
    <div class="mila-status ${esc(service.status)}">
      <div class="name">${esc(service.name)} <span class="mila-badge ${service.status === 'up' ? 'ok' : service.status === 'down' ? 'bad' : 'warn'}">${esc(service.status)}</span></div>
      <div class="detail">${esc(service.detail ?? '')}</div>
      <div class="detail">${service.latencyMs != null ? `${esc(service.latencyMs)} ms` : ''}</div>
    </div>`).join('')
  $('tab-overview').innerHTML = `
    <div class="mila-section">
      <h2>Passerelle</h2>
      <table class="mila-table">
        <tr><th>Démarrée depuis</th><td>${esc(fmtDuration(gateway.uptimeMs))}</td>
            <th>Port</th><td>${esc(gateway.port)}</td>
            <th>Backend</th><td>${esc(gateway.backendUrl)}</td></tr>
        <tr><th>Sessions</th><td>${esc(gateway.sessions)}</td>
            <th>Instances actives</th><td>${esc(gateway.activeInstances)}</td>
            <th>Instances connues</th><td>${esc(gateway.knownInstances)}</td></tr>
        <tr><th>Sessions persistées</th><td colspan="5">${esc(gateway.persistence.sessionsFile)}</td></tr>
        <tr><th>Registre d'instances</th><td colspan="5">${esc(gateway.persistence.registryDir)}</td></tr>
      </table>
    </div>
    <div class="mila-section">
      <h2>Services backend</h2>
      <div class="mila-grid">${cards}</div>
    </div>`
}

// ── Users & sessions ────────────────────────────────────────────────────────

async function renderUsers() {
  const [{ users }, { sessions }] = await Promise.all([
    api('/api/admin/users'),
    api('/api/admin/sessions'),
  ])
  const userRows = users.map((user) => `
    <tr>
      <td>${esc(user.userId)}</td>
      <td>${user.hasKey ? '<span class="mila-badge ok">clé</span>' : '<span class="mila-badge bad">sans clé</span>'}</td>
      <td>${esc(user.activeSessions)}</td>
      <td>${renderInstanceBadge(user.instance)}</td>
      <td><button class="mila-button secondary" data-rotate="${esc(user.userId)}">Rotation du mot de passe</button></td>
    </tr>`).join('')
  const sessionRows = sessions.map((session) => `
    <tr>
      <td><code>${esc(session.id)}</code></td>
      <td>${esc(session.userId)}</td>
      <td>${esc(fmtTime(session.createdAt))}</td>
      <td>${esc(fmtTime(session.lastSeenAt))}</td>
      <td>${esc(fmtTime(session.expiresAt))}</td>
      <td><button class="mila-button danger" data-revoke="${esc(session.id)}" data-user="${esc(session.userId)}">Révoquer</button></td>
    </tr>`).join('')
  $('tab-users').innerHTML = `
    <div class="mila-section">
      <h2>Utilisateurs</h2>
      <p class="mila-muted">Utilisateurs provisionnés dans login-credentials.json. La rotation écrit le nouveau mot de passe dans le fichier initial-passwords.txt (jamais affiché ici).</p>
      <table class="mila-table">
        <tr><th>Utilisateur</th><th>Clé d'API</th><th>Sessions actives</th><th>Instance</th><th></th></tr>
        ${userRows}
      </table>
    </div>
    <div class="mila-section">
      <h2>Sessions de la passerelle</h2>
      <table class="mila-table">
        <tr><th>ID (haché)</th><th>Utilisateur</th><th>Créée</th><th>Dernière activité</th><th>Expire</th><th></th></tr>
        ${sessionRows || '<tr><td colspan="6" class="mila-muted">Aucune session active</td></tr>'}
      </table>
    </div>`

  for (const button of $('tab-users').querySelectorAll('[data-rotate]')) {
    button.addEventListener('click', async () => {
      const user = button.dataset.rotate
      if (!confirm(`Générer un nouveau mot de passe pour ${user} ?`)) return
      button.disabled = true
      try {
        const result = await api(`/api/admin/users/${encodeURIComponent(user)}/rotate-password`, { method: 'POST', body: {} })
        setError(`Mot de passe de ${user} renouvelé — à récupérer dans ${result.passwordFile}`)
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
      const stop = confirm(`Révoquer la session de ${button.dataset.user} ? OK = arrêter aussi son instance, Annuler = garder l'instance.`)
      button.disabled = true
      try {
        await api(`/api/admin/sessions/${encodeURIComponent(id)}/revoke`, { method: 'POST', body: { stopInstance: stop } })
        await renderUsers()
      } catch (error) {
        setError(error.message)
        button.disabled = false
      }
    })
  }
}

// ── Instances ───────────────────────────────────────────────────────────────

function renderInstanceBadge(instance) {
  if (!instance) return '<span class="mila-badge">arrêtée</span>'
  const status = String(instance.state)
  const cls = instance.state === 'running' ? 'ok' : instance.state === 'failed' ? 'bad' : 'warn'
  return `<span class="mila-badge ${cls}">${esc(status)}</span> <span class="mila-muted">:${esc(instance.port)} pid ${esc(instance.pid ?? '—')}</span>`
}

async function renderInstances() {
  const { instances } = await api('/api/admin/instances')
  const rows = instances.map((instance) => `
    <tr>
      <td>${esc(instance.userId)}</td>
      <td>${renderInstanceBadge(instance)}</td>
      <td>${esc(fmtTime(instance.startedAt))}</td>
      <td>${esc(instance.restarts)}</td>
      <td>${instance.failureReason ? `<span class="mila-muted">${esc(instance.failureReason)}</span>` : ''}</td>
      <td>
        <div class="mila-row">
          <button class="mila-button secondary" data-action="start" data-user="${esc(instance.userId)}">Démarrer</button>
          <button class="mila-button secondary" data-action="restart" data-user="${esc(instance.userId)}">Redémarrer</button>
          <button class="mila-button danger" data-action="stop" data-user="${esc(instance.userId)}">Arrêter</button>
          <button class="mila-button secondary" data-logs="${esc(instance.userId)}">Journaux</button>
        </div>
      </td>
    </tr>`).join('')
  $('tab-instances').innerHTML = `
    <div class="mila-section">
      <h2>Instances de harnais</h2>
      <p class="mila-muted">Une instance dsh par utilisateur connecté. Les journaux sont limités aux 32 derniers Kio par défaut et tournent à 2 Mio.</p>
      <table class="mila-table">
        <tr><th>Utilisateur</th><th>État</th><th>Démarrée</th><th>Redémarrages</th><th>Dernière erreur</th><th>Actions</th></tr>
        ${rows || '<tr><td colspan="6" class="mila-muted">Aucune instance active (les utilisateurs démarrent à la première connexion)</td></tr>'}
      </table>
    </div>
    <div class="mila-section" id="log-section" ${state.logUser ? '' : 'hidden'}>
      <h2>Journaux — <span id="log-user"></span></h2>
      <div class="mila-row"><button class="mila-button secondary" id="log-refresh">Rafraîchir</button><button class="mila-button secondary" id="log-close">Fermer</button></div>
      <pre class="mila-log" id="log-output"></pre>
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
    output.textContent = text
    if (scroll) output.scrollTop = output.scrollHeight
  } catch (error) {
    setError(error.message)
  }
}

// ── Approvals ───────────────────────────────────────────────────────────────

async function renderApprovals() {
  const data = await api('/api/admin/approvals')
  const approvals = data.pending_approvals ?? []
  const rows = approvals.map((approval) => `
    <tr>
      <td><code>${esc(approval.approval_id)}</code></td>
      <td>${esc(approval.user_id)}</td>
      <td>${esc(approval.session_id)}</td>
      <td><code>${esc(approval.command)}</code></td>
      <td>${esc(approval.reason)}</td>
      <td>${esc(fmtTime((approval.expires_at ?? 0) * 1000))}</td>
      <td>
        <div class="mila-row">
          <button class="mila-button" data-decide="approved" data-id="${esc(approval.approval_id)}">Approuver</button>
          <button class="mila-button danger" data-decide="rejected" data-id="${esc(approval.approval_id)}">Rejeter</button>
        </div>
      </td>
    </tr>`).join('')
  $('tab-approvals').innerHTML = `
    <div class="mila-section">
      <h2>Approbations en attente</h2>
      <p class="mila-muted">Décisions transmises au backend avec le jeton maître ; store partagé requis (Valkey).</p>
      <table class="mila-table">
        <tr><th>ID</th><th>Utilisateur</th><th>Session</th><th>Commande</th><th>Raison</th><th>Expire</th><th>Décision</th></tr>
        ${rows || '<tr><td colspan="7" class="mila-muted">Aucune approbation en attente</td></tr>'}
      </table>
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

// ── Audit ───────────────────────────────────────────────────────────────────

$('tab-audit').innerHTML = `
  <div class="mila-section">
    <h2>Flux d'audit (VictoriaLogs)</h2>
    <div class="mila-row">
      <input class="mila-input" id="audit-query" style="max-width:32rem" value="*" placeholder="Requête LogsQL, ex: user_id:sysadmin-01">
      <button class="mila-button" id="audit-run">Rechercher</button>
      <span class="mila-muted" id="audit-status"></span>
    </div>
    <div id="audit-results" style="margin-top:.75rem"></div>
  </div>`

$('audit-run').addEventListener('click', runAudit)
$('audit-query').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') runAudit()
})

async function runAudit() {
  const query = $('audit-query').value.trim() || '*'
  $('audit-status').textContent = 'Recherche…'
  try {
    const data = await api(`/api/admin/audit?query=${encodeURIComponent(query)}&limit=100`)
    $('audit-status').textContent = `${data.count} événement(s)`
    const keys = ['_time', 'user_id', 'session_id', 'action', 'tool_name', 'cmd']
    const rows = data.events.map((event) => `
      <tr>${keys.map((key) => `<td>${esc(event[key] ?? '')}</td>`).join('')}</tr>`).join('')
    $('audit-results').innerHTML = `
      <table class="mila-table">
        <tr>${keys.map((key) => `<th>${esc(key)}</th>`).join('')}</tr>
        ${rows || `<tr><td colspan="${keys.length}" class="mila-muted">Aucun événement</td></tr>`}
      </table>`
    setError('')
  } catch (error) {
    $('audit-status').textContent = ''
    setError(error.message)
  }
}

// ── Services ────────────────────────────────────────────────────────────────

async function renderServices() {
  const { services } = await api('/api/admin/services')
  const rows = services.map((service) => `
    <tr>
      <td>${esc(service.name)}</td>
      <td>${service.running ? `<span class="mila-badge ok">actif</span>` : '<span class="mila-badge bad">arrêté</span>'}</td>
      <td>${esc(service.port ?? '—')}</td>
      <td>${esc(service.pid ?? '—')}</td>
      <td><button class="mila-button secondary" data-service="${esc(service.name)}">Redémarrer</button></td>
    </tr>`).join('')
  $('tab-services').innerHTML = `
    <div class="mila-section">
      <h2>Services backend</h2>
      <p class="mila-muted">Redémarrage via <code>platform.sh service &lt;nom&gt; restart</code>, sérialisé, délai maximum 30 s.</p>
      <table class="mila-table">
        <tr><th>Service</th><th>État</th><th>Port</th><th>PID</th><th>Action</th></tr>
        ${rows}
      </table>
      <pre class="mila-log" id="service-output" ${state.serviceOutput ? '' : 'hidden'}>${esc(state.serviceOutput)}</pre>
    </div>`
  for (const button of $('tab-services').querySelectorAll('[data-service]')) {
    button.addEventListener('click', async () => {
      const name = button.dataset.service
      if (!confirm(`Redémarrer le service ${name} ?`)) return
      button.disabled = true
      state.serviceOutput = `Redémarrage de ${name}…`
      $('service-output').hidden = false
      $('service-output').textContent = state.serviceOutput
      try {
        const result = await api(`/api/admin/services/${encodeURIComponent(name)}/restart`, { method: 'POST', body: {} })
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

// ── Boot ────────────────────────────────────────────────────────────────────

async function boot() {
  try {
    const session = await api('/api/admin/session')
    setAuthenticated(Boolean(session.authenticated))
    if (session.authenticated) runAudit()
  } catch {
    setAuthenticated(false)
  }
}

boot()
