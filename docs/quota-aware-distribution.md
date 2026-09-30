# Distribution quota-aware

> Chantier 1/6 — les quotas sont la seule limite dure du système.
> Modules : `backend/services/control_store/quota_scopes.py`,
> `backend/services/fleet/distribution.py`,
> `backend/services/fleet/quota_router.py`,
> extension additive de `backend/services/auth_gateway/quota_manager.py`.

## 1. Principes

Deux mécanismes distincts, deux responsabilités :

| Mécanisme | Rôle | Réponse quand le budget est épuisé |
|---|---|---|
| **Admission** (quota gate) | Décide si une requête peut consommer du budget | **429** (+ `Retry-After`), jamais de 500 |
| **Distribution** (poids de routage) | Répartit la capacité restante entre les réplicas placés | Dégrade proprement, **jamais de panne totale** |

**Les quotas bornent, la distribution dégrade proprement.** Un budget d'équipe
épuisé ne doit jamais faire disparaître un modèle de la table de routage :
la distribution retire le trafic des réplicas concernés (poids 0) mais
conserve une capacité résiduelle (poids epsilon) pour que le refus s'exprime
au bon endroit — un 429 propre au gate d'admission — plutôt qu'une erreur
« modèle introuvable » côté LiteLLM.

## 2. Scopes : user / team / project

Les quotas per-user existants (Valkey : 2 in-flight, 60 RPM, 150k TPM,
2M tokens/jour) sont inchangés. Les scopes `team` et `project` bornent la
consommation **agrégée** de tous les utilisateurs d'une équipe ou d'un projet.
Un scope sans limites configurées est **illimité** : la fonctionnalité est
opt-in, un déploiement existant se comporte exactement comme avant tant
qu'aucun budget team/project n'est posé.

Limites configurables par scope (toutes optionnelles, au moins une requise) :
`daily_tokens`, `monthly_tokens`, `rpm`, `tpm`.

### Stockage durable (PostgreSQL)

Deux tables créées par `QuotaScopes.ensure_schema()` (`CREATE TABLE IF NOT
EXISTS`, dans le module — `schema.py` n'est pas touché) :

- `quota_scopes(scope_type, scope_id, limits JSONB, updated_at)` — les budgets.
- `quota_usage_daily(scope_type, scope_id, day, tokens, requests, cost_usd)` —
  l'usage journalier durable (reconciliation après restart, comme le ledger).

Fail-closed comme tout le leg durable : store injoignable → `ConnectionError`
→ **503**, jamais de fallback silencieux vers « budget illimité ».

### API `QuotaScopes`

`set_limits` / `get_limits` / `delete_limits` / `list_scopes`,
`check_budget(scope_type, scope_id, tokens) -> (admis, info)`,
`record_usage(scope_type, scope_id, tokens, model=None, team_id=None)`,
`usage_summary(scope_type, scope_id, day)`, `chargeback(day)`.

`check_budget` retourne `info = {window, limit, used, remaining,
reset_in_seconds}` : `reset_in_seconds` alimente directement l'en-tête
`Retry-After` des réponses 429 (secondes jusqu'à minuit / fin de mois, fuseau
`QUOTA_TIMEZONE` comme le `QuotaManager`).

`record_usage(..., team_id="alpha")` attribue en plus l'usage à la ligne du
scope `team` : un budget d'équipe borne l'agrégat sans second appel.

## 3. Poids de distribution (`quota_weights`)

Producteur des `routing_weights` consommés par
`litellm_sync.sync_from_fleet(..., routing_weights={model: [{"node", "weight"}]})`
(implémenté par un autre chantier) :

```python
from services.fleet.distribution import quota_weights, team_state_for_models

team_state = team_state_for_models(policies, quota_scopes)  # {model: {team_id, remaining_ratio, exhausted}}
weights = quota_weights(assignments, team_state)            # {model: [{"node", "weight"}]}
```

- `assignments = {model: [{"node": ...}]}` : placement per-modèle (issu du scheduler).
- `team_state = {model: {"team_id": str, "remaining_ratio": 0..1, "exhausted": bool}}`.
- Équipe épuisée → poids **0** (réplica retiré du routage).
- Si ça viderait complètement un modèle → **poids epsilon (1e-6)** sur ses
  réplicas : dégradation, jamais de panne totale. Le gate d'admission
  continuant d'appliquer le budget, ce filet se traduit par des 429, pas par
  du trafic gratuit.
- Sinon poids proportionnel à `remaining_ratio`.
- Modèle sans signal quota → poids neutre **1.0** (on ne pénalise jamais ce
  qu'on ne voit pas).
- Tous les poids émis sont **> 0** (les poids nuls sont filtrés), conformément
  au contrat `routing_weights`.

`team_state_for_models` lit le `team_id` de chaque politique
`fleet_desired_state` et interroge `QuotaScopes` ; une panne du store lève
`ConnectionError` au lieu de fabriquer des ratios (l'appelant garde les poids
précédents ou répond 503).

## 4. Chargeback

`QuotaScopes.chargeback(day)` et `GET /api/v1/quotas/chargeback` agrègent
tokens / requêtes / `cost_usd` par scope. `cost_usd` est une **estimation**
pour le showback : prix configurés par modèle via `set_model_price()`
(défaut 0.0 — aucun prix n'est inventé), jamais du billing.

## 5. Refus : comportement actuel et alignement

Constaté sur le code actuel (2026-09-30) :

- **ForwardAuth** (`auth_gateway/server.py::verify`) : `QuotaExceededException`
  → **429** JSON `{"error": "quota_exhausted", "user_id", "consumed_tokens",
  "limit", "message"}` ; `ConnectionError` → **503**
  `{"error": "quota_state_unavailable"}`. **Pas d'en-tête `Retry-After`
  aujourd'hui.**
- **LiteLLM admission** (`auth_gateway/litellm_auth.py`) :
  `QuotaExceededException` → `HTTPException` **429** ; `ConnectionError` → 503.

Alignement du design de ce chantier :

- `POST /api/v1/quotas/check` : 200 si admis, **429 + `Retry-After:
  <secondes>`** si épuisé, **503** si le store est injoignable. Jamais de 500.
- **Recommandation d'intégration** (hors périmètre de ce chantier — ne pas
  modifier `server.py`/`litellm_auth.py` ici) : ajouter `Retry-After` aux 429
  existants, en le calculant depuis `check_budget(...)[1]["reset_in_seconds"]`
  pour les scopes, ou jusqu'à minuit (`QUOTA_TIMEZONE`) pour le per-user.

## 6. Branchement (intégration, hors périmètre)

L'extension de `quota_manager.py` est **purement additive** : aucune méthode
existante n'a été modifiée, la suite per-user est inchangée. Le branchement
se fait en 3 endroits, à l'intégration :

1. **Monter le router** : `app.include_router(quota_router.router)` dans
   `services/agent_tools/server.py` (le module expose `router`, `require_admin`
   et `quota_scopes` ; le store s'ouvre paresseusement à la première requête).
2. **Attacher le store** au démarrage du gateway :
   `quota_mgr.attach_quota_scopes(open_quota_scopes())`.
3. **Rattacher la clé à son scope** : mapping opérateur clé → `team_id` /
   `project_id` (ex. Valkey `quota:scope-attachment:<user_id>` ou métadonnée
   du KeyStore — **jamais** depuis un en-tête client, cf. guardrail
   « l'identité vient des credentials »), puis dans le chemin d'admission,
   après le check per-user existant :

```python
scope = get_scope_attachment(user_id)  # ex. ("team", "alpha") ou None
if scope is not None:
    scope_type, scope_id = scope
    try:
        quota_mgr.check_scoped_token_budget(scope_type, scope_id, estimated_tokens)
    except QuotaExceededException as qe:
        return Response(status_code=429, ...,
                        headers={"Retry-After": str(info_reset_seconds)})
```

et après exécution, dans le chemin de settlement existant :

```python
if scope is not None:
    quota_mgr.record_scoped_usage(scope_type, scope_id, total_tokens,
                                  model=model, team_id=team_id)
```

## 7. Reste à qualifier en lab

- **Concurrence** : `record_usage` repose sur l'upsert atomique PostgreSQL
  (`ON CONFLICT DO UPDATE`) — à valider sous charge concurrente (test
  `tier3_concurrency` à écrire à l'intégration).
- **Prix du chargeback** : `cost_usd` n'est significatif qu'avec des prix
  opérateur (`set_model_price`) ; le modèle économique réel (électricité /
  amortissement GPU) reste à définir avec l'équipe.
- **Fenêtre mensuelle** : `monthly_tokens` sommé depuis le 1er du mois en
  `QUOTA_TIMEZONE` ; vérifier le comportement aux changements d'heure si
  `QUOTA_TIMEZONE != UTC`.
- **Retry-After per-user** : le 429 ForwardAuth actuel n'en envoie pas
  (recommandation §5) — à brancher avec le rattachement des scopes.
- **Monture du router** : `agent_tools/server.py` non modifié (interdit par
  le périmètre) — le montage reste à faire à l'intégration.
