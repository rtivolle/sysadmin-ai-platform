# Versioned model rollout: staging → canary → prod

The model manager versions every build of a model and walks it through a
promotion chain before it serves production traffic. The registry
(`backend/services/model_manager/registry.py`) stores one `versions` map per
model — `{version_id: {version, hf_repo, revision, engine, stage,
canary_percent, created_at, updated_at}}` — plus an append-only
`promotion_history` used for rollback. Models registered before versions
existed have no `versions` key and behave exactly as before.

## 1. Stage machine

`staging → canary → prod`, and any stage may move to `archived`. Two
deliberate extensions, both explicit operator actions:

* **demote** (`prod`/`canary → staging`): emergency pull-back;
* **re-qualify** (`archived → staging`/`canary`/`prod`): re-test an archived
  build — rollback uses this to re-promote the previous prod version.

Anything else (e.g. `staging → prod`, skipping canary; `prod → canary`,
going backwards) raises `ValueError`. `canary` and `prod` are
single-occupant stages: promoting a version into one archives the previous
occupant automatically.

## 2. Promotion flow

```
register version (staging)
        │  POST /api/v1/models/{model}/versions   {"version","hf_repo","revision","engine"}
        ▼
promote → canary      POST /api/v1/models/{model}/promote  {"version","target_stage":"canary","canary_percent":10}
        │  registry: stage=canary, canary_percent recorded
        │  fleet policy (read-modify-write): canary_version=<v>, canary_traffic_percent=<n>
        ▼
adjust traffic        POST /api/v1/models/{model}/canary   {"version","percent":25}   (0-100, bounded)
        ▼
promote → prod        POST /api/v1/models/{model}/promote  {"version","target_stage":"prod"}
        │  registry: previous prod archived, history appended
        │  fleet policy: canary_* cleared, version=<v>
        ▼
rollback (on incident) POST /api/v1/models/{model}/rollback
        │  restores the newest prod version from history that is not the current one;
        │  policy version=<previous>, canary_* cleared. A second rollback walks
        │  one step further back through the append-only history.
```

`GET /api/v1/models/{model}/versions` lists versions and the history tail.
All endpoints are admin-only (same `require_admin` pattern as the lifecycle
API) and audited. Promotion fails closed: when the fleet control store is
unreachable the API returns 503 instead of writing a half-synced state.

## 3. Canary contract (shared with the fleet scheduler and LiteLLM sync)

The fleet `desired_state` policy of a model may carry:

* `canary_version` (string) — the version id receiving canary traffic;
* `canary_traffic_percent` (0–100) — its traffic share;
* `version` (string) — the current prod version.

Only `promotion.py` writes these fields, always read-modify-write: every
other policy field (`replicas`, `engine`, `gpu_class`, …) survives a
promotion untouched. The LiteLLM sync derives a `{model}-canary` entry from
the canary fields while they are present and drops it when promotion to
prod clears them.

## 4. What the node-agent must do for a canary (GPU side)

> **Status: to qualify in lab.** The platform side (registry, policy fields,
> LiteLLM `{model}-canary` entry) is implemented and unit-tested; the GPU
> node behaviour below is specified but not yet exercised on hardware.

When a model's policy carries `canary_version`, the node-agent on each
assigned GPU node must:

1. Read `canary_version` from the model's `fleet_desired_state` policy
   (alongside the prod `version` field).
2. Serve **both** builds: the prod version under the usual local endpoint
   and the canary version under a distinct local endpoint/port, reporting
   both in its heartbeat (`serving: {prod: <v>, canary: <v>}`).
3. Respect node-local resource caps: the canary shares the node's GPU
   budget with prod — if it does not fit, the node reports
   `canary: "unschedulable"` and the convergence loop must surface it
   rather than silently dropping the canary.

Open questions for the lab: how the node-agent fetches two weight sets
without doubling disk (shared layer cache?); the exact heartbeat schema;
whether the canary gets a dedicated GPU slice or time-shares with prod.

## 5. Implementation notes

* `backend/services/model_manager/registry.py` — version CRUD
  (`register_version`, `set_stage`, `set_canary_percent`, `get_version`,
  `list_versions`, `active_version`, `promotion_history`) on the existing
  atomic JSON store; `versions`/`promotion_history` added to
  `ALLOWED_FIELDS`.
* `backend/services/model_manager/promotion.py` — `promote`,
  `set_canary_traffic`, `rollback`, `demote_to_staging`,
  `promotion_summary`; the only writer of the canary/prod version fields
  in the fleet policy.
* `backend/services/model_manager/router.py` — the five admin endpoints
  above.
* Tests: `backend/tests/tier1_unit/test_model_promotion.py` (31 tests:
  transitions, canary bounds, rollback history, policy read-modify-write,
  endpoints, 503 fail-closed).
