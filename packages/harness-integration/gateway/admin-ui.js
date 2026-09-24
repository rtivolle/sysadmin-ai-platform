/* Admin console for the Mila sysadmin harness gateway. Vanilla JS, no build step. */
'use strict'

const state = {
  authenticated: false,
  activeTab: 'overview',
  pollTimer: null,
  logUser: null,
  logTimer: null,
  serviceOutput: '',
  quotaDirty: false,
  quotaLoading: false,
  quotaSaving: false,
  localModelsDirty: false,
  localModelsBusy: false,
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
    quotas: renderQuotas,
    instances: renderInstances,
    models: renderModels,
    'local-models': renderLocalModels,
    approvals: renderApprovals,
    audit: () => {},
    services: renderServices,
    hardware: renderHardware,
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
  for (const name of ['overview', 'users', 'quotas', 'instances', 'models', 'local-models', 'approvals', 'audit', 'services', 'hardware']) {
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

// ── Quotas ──────────────────────────────────────────────────────────────────

const quotaLabels = { concurrency: 'Requêtes simultanées', rpm: 'Requêtes / minute', tpm: 'Tokens / minute', daily_tokens: 'Tokens / jour' }

async function renderQuotas() {
  if (state.quotaDirty || state.quotaLoading || state.quotaSaving) return
  state.quotaLoading = true
  try {
    const data = await api('/api/admin/quotas')
    if (state.quotaDirty || state.quotaSaving) return
    $('tab-quotas').innerHTML = `
      <div class="mila-section">
        <h2>Quotas par utilisateur</h2>
        <p class="mila-muted">Limites partagées dans Valkey, appliquées aux prochaines admissions et requêtes LiteLLM. Les requêtes en cours et les compteurs restent inchangés. Une limite personnalisée s’applique aussi pendant une élévation P1.</p>
        <p class="mila-muted">Usage : requêtes actives de l’agent, tokens consommés et réservés aujourd’hui. Les limites RPM/TPM sont appliquées par LiteLLM ; ses compteurs ne sont pas exposés ici. Le plafond de concurrence global reste indépendant.</p>
        <div class="quota-grid">${data.users.map((user) => `
          <form class="quota-card" data-quota-user="${esc(user.user_id)}">
            <h3>${esc(user.user_id)} ${user.p1_elevated ? '<span class="mila-badge warn">P1</span>' : ''}</h3>
            <p class="mila-muted">${esc(user.day)} · ${esc(user.timezone)}<br>
              Actives : ${esc(user.usage.concurrency)} · Consommés : ${esc(user.usage.daily_tokens.toLocaleString())}<br>
              Réservés : ${esc(user.usage.reserved_tokens.toLocaleString())}</p>
            ${Object.entries(quotaLabels).map(([field, label]) => `
              <label>${label}<input class="mila-input" type="number" name="${field}" min="${data.bounds[field][0]}" max="${data.bounds[field][1]}" step="1" value="${user.limits[field]}" required></label>`).join('')}
            <div class="mila-row">
              <button class="mila-button" type="submit">Enregistrer</button>
              <button class="mila-button secondary" type="button" data-quota-reset>Valeurs par défaut</button>
            </div>
            <p class="mila-muted quota-status" role="status">${Object.keys(user.overrides).length ? 'Limites personnalisées' : 'Valeurs par défaut'}</p>
          </form>`).join('')}</div>
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

// ── Models ──────────────────────────────────────────────────────────────────

async function renderModels() {
  const data = await api('/api/admin/models')
  const modes = { simulated: 'Simulation locale', 'vllm-proxy': 'Proxy vLLM configuré', unknown: 'État du moteur indisponible' }
  $('tab-models').innerHTML = `
    <div class="mila-section">
      <h2>Modèles disponibles <span class="mila-badge ${data.inferenceMode === 'simulated' ? 'warn' : ''}">${esc(modes[data.inferenceMode] ?? data.inferenceMode)}</span></h2>
      <p class="mila-muted">Catalogue publié par ${esc(data.source)}. Ces identifiants sont sélectionnables dans le harnais. Une entrée dans le catalogue ne prouve pas qu’un modèle GPU est chargé.</p>
      <table class="mila-table"><thead><tr><th>Identifiant du modèle</th><th>Propriétaire déclaré</th></tr></thead><tbody>
        ${data.models.map((model) => `<tr><td><code>${esc(model.id)}</code></td><td>${esc(model.owned_by || '—')}</td></tr>`).join('') || '<tr><td colspan="2">Aucun modèle publié par LiteLLM</td></tr>'}
      </tbody></table>
    </div>`
}

// ── Local models ──────────────────────────────────────────────────────────────

const modelStatusLabels = {
  registered: 'enregistré', downloading: 'téléchargement', downloaded: 'téléchargé',
  starting: 'démarrage', running: 'en service', stopped: 'arrêté', error: 'erreur',
}

function modelStatusBadge(status) {
  const cls = status === 'running' || status === 'downloaded' ? 'ok' : status === 'error' ? 'bad' : 'warn'
  return `<span class="mila-badge ${cls}">${esc(modelStatusLabels[status] ?? status)}</span>`
}

function fmtBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '—'
  const units = ['o', 'Kio', 'Mio', 'Gio', 'Tio']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1 }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`
}

async function renderLocalModels() {
  if (state.localModelsDirty || state.localModelsBusy) return
  const data = await api('/api/admin/local-models')
  const rows = data.models.map((model) => {
    const server = model.server ?? {}
    const running = model.status === 'running'
    const started = ['downloaded', 'stopped', 'running', 'error'].includes(model.status)
    return `<tr>
      <td><code>${esc(model.name)}</code><br><span class="mila-muted">${esc(model.hf_repo)}${model.revision ? `@${esc(model.revision)}` : ''}</span></td>
      <td>${modelStatusBadge(model.status)}${model.last_error ? `<br><span class="mila-muted">${esc(model.last_error)}</span>` : ''}</td>
      <td>${esc(fmtBytes(model.size_bytes))}</td>
      <td>${server.port ? esc(`:${server.port} pid ${server.pid ?? '—'}`) : '—'}</td>
      <td>
        <div class="mila-row">
          ${['registered', 'error'].includes(model.status) ? `<button class="mila-button secondary" data-model="${esc(model.name)}" data-action="download">Télécharger</button>` : ''}
          ${started && !running ? `<button class="mila-button" data-model="${esc(model.name)}" data-action="start">Démarrer</button>` : ''}
          ${running ? `<button class="mila-button secondary" data-model="${esc(model.name)}" data-action="restart">Redémarrer</button>` : ''}
          ${running ? `<button class="mila-button danger" data-model="${esc(model.name)}" data-action="stop">Arrêter</button>` : ''}
          <button class="mila-button secondary" data-model="${esc(model.name)}" data-action="logs">Journal</button>
          ${!running && model.status !== 'starting' ? `<button class="mila-button danger" data-model="${esc(model.name)}" data-action="delete">Supprimer</button>` : ''}
        </div>
      </td></tr>`
  }).join('')
  $('tab-local-models').innerHTML = `
    <div class="mila-section">
      <h2>Modèles locaux</h2>
      <p class="mila-muted">Enregistrer un dépôt HuggingFace, le télécharger, puis démarrer un serveur vLLM local. Un modèle n’est sélectionnable qu’une fois <em>en service</em>. Le téléchargement et le démarrage prennent plusieurs minutes.</p>
      <form id="local-model-form" class="quota-card">
        <label>Dépôt HuggingFace ou lien<input class="mila-input" name="hf_repo" placeholder="org/model ou https://huggingface.co/org/model" required></label>
        <label>Nom du service (optionnel)<input class="mila-input" name="name" placeholder="déduit du dépôt"></label>
        <label>Révision (optionnel)<input class="mila-input" name="revision" placeholder="main"></label>
        <button class="mila-button" type="submit">Enregistrer</button>
      </form>
      <div id="local-model-log" hidden></div>
      <table class="mila-table"><thead><tr><th>Modèle</th><th>État</th><th>Taille</th><th>Serveur</th><th>Actions</th></tr></thead><tbody>
        ${rows || '<tr><td colspan="5">Aucun modèle local enregistré</td></tr>'}
      </tbody></table>
    </div>`

  $('local-model-form').addEventListener('input', () => { state.localModelsDirty = true })
  $('local-model-form').addEventListener('submit', async (event) => {
    event.preventDefault()
    const form = event.target
    const body = {
      hf_repo: form.elements.hf_repo.value,
      name: form.elements.name.value || undefined,
      revision: form.elements.revision.value || undefined,
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
    button.addEventListener('click', () => runModelAction(button.dataset.model, button.dataset.action))
  }
}

async function runModelAction(name, action) {
  if (action === 'delete' && !confirm(`Supprimer le modèle ${name} ? Les fichiers téléchargés seront effacés.`)) return
  if (action === 'logs') {
    try {
      const data = await api(`/api/admin/local-models/${encodeURIComponent(name)}/logs`)
      const view = $('local-model-log')
      view.hidden = false
      view.innerHTML = `<h3>Journal vLLM — ${esc(name)}</h3><pre>${esc(data.output || '(vide)')}</pre>`
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

// ── Hardware survey ───────────────────────────────────────────────────────────

async function renderHardware(refresh = false) {
  const data = await api(`/api/admin/survey${refresh ? '?refresh=1' : ''}`)
  const gpuRows = (data.gpus ?? []).map((gpu) => `<tr><td>${esc(gpu.index)}</td><td>${esc(gpu.name)}</td><td>${esc(gpu.architecture)} ${esc(gpu.compute_capability)}</td><td>${esc(gpu.vram_total_mb)} Mo</td><td>${esc(gpu.vram_free_mb)} Mo</td><td>${esc(gpu.driver_version)}</td></tr>`).join('')
  const pciRows = (data.pci_accelerators ?? []).map((device) => `<tr><td><code>${esc(device.slot)}</code></td><td>${esc(device.description)}</td><td>${esc(device.vendor_device_ids ?? '—')}</td><td>${esc(device.kernel_driver ?? '—')}</td></tr>`).join('')
  const drivers = data.driver_stack ?? {}
  const nvidia = drivers.nvidia ?? {}
  const versions = data.software_versions ?? {}
  const storage = data.model_storage ?? {}
  $('tab-hardware').innerHTML = `
    <div class="mila-section">
      <h2>Matériel &amp; pilotes</h2>
      <p class="mila-muted">Relevé ${esc(data.survey_timestamp ?? '')} · hôte ${esc(data.hostname ?? '')}. Mis en cache 10 s ; actualiser pour relancer les sondes.</p>
      <button class="mila-button secondary" id="survey-refresh" type="button">Actualiser</button>
      <h3>Accélérateurs NVIDIA</h3>
      <table class="mila-table"><thead><tr><th>#</th><th>Nom</th><th>Architecture</th><th>VRAM</th><th>Libre</th><th>Pilote</th></tr></thead><tbody>${gpuRows || '<tr><td colspan="6">Aucun GPU NVIDIA détecté</td></tr>'}</tbody></table>
      <h3>Périphériques PCI</h3>
      <table class="mila-table"><thead><tr><th>Emplacement</th><th>Description</th><th>IDs</th><th>Pilote noyau</th></tr></thead><tbody>${pciRows || '<tr><td colspan="4">Aucun accélérateur PCI détecté</td></tr>'}</tbody></table>
      <h3>Pilotes &amp; outils</h3>
      <p class="mila-muted">Noyau ${esc(drivers.kernel_release ?? '—')} · modules ${esc((drivers.loaded_modules ?? []).join(', ') || '—')}<br>
        NVIDIA présent : ${nvidia.present ? 'oui' : 'non'} · pilote ${esc(nvidia.smi_driver_version ?? '—')} · CUDA runtime ${esc(nvidia.cuda_runtime ?? '—')} · CUDA toolkit ${esc(nvidia.cuda_toolkit ?? '—')}</p>
      <h3>Versions logicielles</h3>
      <p class="mila-muted">Python ${esc(versions.python ?? '—')} · torch ${esc(versions.torch ?? 'absent')} · vLLM ${esc(versions.vllm ?? 'absent')} · huggingface_hub ${esc(versions.huggingface_hub ?? 'absent')} · litellm ${esc(versions.litellm ?? 'absent')}</p>
      <h3>Stockage des modèles</h3>
      <p class="mila-muted">${esc(storage.path ?? '—')} · libre ${esc(fmtBytes(storage.free_bytes))} / ${esc(fmtBytes(storage.total_bytes))}</p>
      <table class="mila-table"><thead><tr><th>Répertoire</th><th>Taille</th></tr></thead><tbody>${(storage.entries ?? []).map((entry) => `<tr><td><code>${esc(entry.name)}</code></td><td>${esc(fmtBytes(entry.size_bytes))}</td></tr>`).join('') || '<tr><td colspan="2">Aucun modèle téléchargé</td></tr>'}</tbody></table>
    </div>`
  $('survey-refresh').addEventListener('click', () => renderHardware(true).catch((error) => setError(error.message)))
}

// ── Instances ───────────────────────────────────────────────────────────────

function renderInstanceBadge(instance) {
  if (!instance) return '<span class="mila-badge">arrêtée</span>'
  const status = String(instance.state)
  const cls = instance.state === 'running' ? 'ok' : instance.state === 'failed' ? 'bad' : 'warn'
  return `<span class="mila-badge ${cls}">${esc(status)}</span> <span class="mila-muted">:${esc(instance.port)} pid ${esc(instance.pid ?? '—')}</span>`
}

async function renderInstances() {
  const [{ instances: known }, { users }] = await Promise.all([api('/api/admin/instances'), api('/api/admin/users')])
  const instances = [...known, ...users.filter((user) => !known.some((instance) => instance.userId === user.userId))
    .map((user) => ({ userId: user.userId, state: 'stopped', restarts: 0 }))]
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
      <h2>Runtimes par utilisateur</h2>
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
      <td>${service.name === 'harness_gateway' ? '<span class="mila-muted">Cette console · gestion depuis l’hôte</span>' : `
        <div class="mila-row">
          <button class="mila-button secondary" data-service="${esc(service.name)}" data-action="start" ${service.running ? 'disabled' : ''}>Démarrer</button>
          <button class="mila-button secondary" data-service="${esc(service.name)}" data-action="restart">Redémarrer</button>
          <button class="mila-button danger" data-service="${esc(service.name)}" data-action="stop" ${service.running ? '' : 'disabled'}>Arrêter</button>
        </div>`}</td>
    </tr>`).join('')
  $('tab-services').innerHTML = `
    <div class="mila-section">
      <h2>Services backend</h2>
      <p class="mila-muted">Contrôle via <code>platform.sh service</code>, sérialisé par service, délai maximum 30 s. Arrêter un service interrompt ses requêtes en cours.</p>
      <table class="mila-table">
        <tr><th>Service</th><th>État</th><th>Port</th><th>PID</th><th>Action</th></tr>
        ${rows}
      </table>
      <pre class="mila-log" id="service-output" ${state.serviceOutput ? '' : 'hidden'}>${esc(state.serviceOutput)}</pre>
    </div>`
  for (const button of $('tab-services').querySelectorAll('[data-service]')) {
    button.addEventListener('click', async () => {
      const name = button.dataset.service
      const action = button.dataset.action
      if (action !== 'start' && !confirm(`${action === 'stop' ? 'Arrêter' : 'Redémarrer'} le service ${name} ?`)) return
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
