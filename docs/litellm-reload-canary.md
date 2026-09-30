# LiteLLM hot-reload qualification, routing weights, canary entries

Date: 2026-09-30. Scope: `backend/services/model_manager/litellm_sync.py`
(chantier 4/6). Status: qualified by source inspection + unit tests; the live
proxy behaviour still has to be qualified in the lab (checklist at the end).

## 1. Hot-reload qualification

**Target version:** litellm **1.103.1** — the version `install.sh` installs
today (`pip install "litellm[proxy]"`, wheel inspected 2026-09-30). Our
deployment runs the proxy **config-only** (`litellm --config config.yaml`,
no database, `--num_workers 2`).

### What was checked (in the 1.103.1 sources)

| Candidate | Verdict | Evidence |
|---|---|---|
| `POST /config/update` | **Unusable config-only.** The handler requires a connected database: `proxy_server.py:17206` raises `Exception("No DB Connected")` when `prisma_client is None`. Even DB-backed, it writes `LiteLLM_Config` rows and calls `add_deployment` — it does not re-read the YAML file. Requires `proxy_admin` role. | `litellm/proxy/proxy_server.py:17156` (`@router.post("/config/update")`), `:17206` (`raise Exception("No DB Connected")`) |
| SIGHUP | **Does not exist.** No SIGHUP handler anywhere in `litellm/proxy/`. | `grep -rn SIGHUP litellm/proxy/` → no hits |
| Config file watcher | **Does not exist.** Nothing re-reads `model_list` from YAML at runtime. | periodic jobs below |
| Periodic APScheduler reload | **DB-only.** `add_deployment` is scheduled only when `store_model_in_db is True`; the unconditional `check_periodic_reloads` job only refreshes the model cost map and Anthropic beta headers. | `litellm/proxy/proxy_server.py` ~`:10184-10207` |
| `litellm --reload` (proxy_cli) | **Dev-only process restart**, not a hot reload: it extends uvicorn's `reload_dirs`/`reload_includes` so saving the YAML restarts the worker (BerriAI/litellm#27274). Drops in-flight requests; not for production. | `litellm/proxy/proxy_cli.py` `_get_reload_options()` |

**Conclusion:** in config-only mode there is no reliable hot-reload of the
YAML `model_list` in litellm 1.103.1. The restart through `platform.sh`
(`platform.sh service litellm restart`) remains the safe, documented path.

### `reload_config()` — the seam for the future

`litellm_sync.reload_config()` implements the policy *hot attempt first,
restart fallback* without changing the default behaviour:

1. Hot attempt only when explicitly enabled (`hot_reload=True` or
   `LITELLM_HOT_RELOAD=1`) **and** a master key is available
   (`master_key=` or `LITELLM_MASTER_KEY`, injected by `platform.sh` at
   startup — never logged, only sent as a `Bearer` header on loopback):
   `POST {LITELLM_URL}/config/update` with the on-disk `model_list`, then a
   verification probe (`GET /v1/models`, injectable via `verify_fn`).
2. On any failure — disabled, no key, connection error, non-2xx, failed
   verification — fall back to the existing restart via `platform.sh`.

The function is idempotent (re-posting an unchanged `model_list` is a proxy
no-op; restarting twice is harmless) and fully injectable (`http_post`,
`verify_fn`, `run_fn`) so the policy is unit-tested without a proxy
(`test_litellm_reload_canary.py`: hot OK → no restart; hot 500/exception/
verify-fail → restart; disabled → straight to restart).

`sync()` and `sync_from_fleet()` gained an opt-in `hot_reload=False` kwarg.
In `sync_from_fleet`, the hot attempt runs on change and only the **restart
fallback** goes through the anti-flap cooldown (`_maybe_hot_reload_litellm`);
a failed hot attempt inside the cooldown window is deferred exactly like a
restart. Default (`False`) = today's restart behaviour, byte for byte.

## 2. `routing_weights`

Signature (retro-compatible, all consumers keep working unchanged):

```python
sync_from_fleet(..., routing_weights=None)
# routing_weights = {model: [{"node": str, "weight": float}]}  # weight > 0
```

A matching `(model, node)` deployment gets `litellm_params.weight`.
Validation is fail-fast: non-numeric / `<= 0` / non-finite weights,
malformed shapes → `ValueError` (a silent bad weight would skew production
traffic). Entries matching no placement are **ignored** — fleet membership
changes every sync cycle, so a weight for a drained node must not fail the
sync. `None` produces byte-identical output to the previous version (no
`weight` key).

**Routing-strategy constraint (verified in 1.103.1 sources):** the router
consults `litellm_params.weight` **only** under the `simple-shuffle`
strategy (`litellm/router.py:13931-13940` → `simple_shuffle()`, which reads
the `weight`/`rpm`/`tpm` metrics at `litellm/router_strategy/simple_shuffle.py:54`).
The shipped `backend/config/litellm/config.yaml` uses
`routing_strategy: "least-busy"` (`least_busy.py` never reads `weight`), so
**weights are accepted in the generated config but currently ignored by the
router**. Decision required (out of this file's scope): switch the fleet
`router_settings.routing_strategy` to `simple-shuffle` for weight-aware
routing, or keep `least-busy` (load-spread) and treat weights as inert
metadata until then. Either way the generated `weight` keys are forward-
compatible — no regeneration needed when the strategy flips.

## 3. Canary entries (`{model}-canary`)

New optional inputs (from the fleet control plane, which owns
`fleet_desired_state` policies and node versions):

```python
sync_from_fleet(...,
    node_versions={"gpu-01": "v1", "gpu-02": "v2"},       # {node: version}
    canary_policies={"model-a": {"canary_version": "v2",  # from fleet_desired_state
                                 "canary_traffic_percent": 10}})
```

Mechanics, exactly as implemented:

- A policy is **active** when `canary_version` is a non-empty string and
  `0 < canary_traffic_percent <= 100`. `percent == 0`, a missing version, or
  no node carrying the version → no canary entry (stable pool untouched).
  Out-of-range (`> 100`) or non-numeric percents → `ValueError`.
- For each active policy, every placed `(model, node)` whose
  `node_versions[node] == canary_version` gets an entry under
  `model_name = "{model}-canary"` pointing at that node's `api_base`, with
  `model_info = {managed_by: "sysadmin-fleet-manager", canary_of: model,
  canary_version, canary_traffic_percent}`.
- **Canary isolation:** those nodes are *excluded* from the plain `{model}`
  pool while the canary is active, so stable traffic can never land on the
  canary version by accident.
- **The percent is enforced upstream, not by LiteLLM.** LiteLLM exposes two
  names (`model-a` and `model-a-canary`); the gateway/agent layer sends
  `canary_traffic_percent` % of requests to the `-canary` name. There is no
  native percent-split between two model names in the LiteLLM router.
- `routing_weights` keys apply to the generated entry name, so canary
  deployments are weighted with `{"model-a-canary": [...]}`.
- The result dict carries a `canary` summary:
  `{model: {canary_version, canary_traffic_percent, nodes: [...]}}` for the
  daemon/consumers.

## 4. Proposal: how the node-agent serves a canary version (to validate in lab)

`sync_from_fleet` only *consumes* versions; producing them is the control
plane's job. This module implements the contract from the task: one version
per (model, node) — `node_versions = {node: version}` — and the
`{model}-canary` entry routes to the **nodes carrying the canary version**.
Those nodes are excluded from the stable `{model}` pool while the canary is
active (canary isolation).

> **Topology open question — reconcile with `model-promotion.md` §4.**
> That doc (parallel chantier) proposes the node-agent serve *both* builds on
> each assigned node (prod + canary on distinct local endpoints). This module
> implements the alternative: dedicated canary nodes (one version per node),
> which is what "`{model}-canary` routed to the nodes carrying this version"
> means literally. The two topologies differ in `node_versions` shape
> (`{node: version}` vs per-node `{prod, canary}` serving map) and in how
> `{model}-canary` `api_base` values are built (node address vs node address +
> canary port). **Decision needed before lab qualification**; the code path is
> isolated in `_fleet_managed_entry` / `canary_pairs` so either topology is a
> small, contained change.

Proposed wiring for the dedicated-canary-node topology (not yet implemented):

1. `fleet_desired_state` policy gains `canary_version` /
   `canary_traffic_percent` (schema already allows arbitrary policy JSON;
   `promotion.py` is the designated writer, read-modify-write).
2. The daemon's per-node push payload (heartbeat `POST`) gains a per-model
   desired version: `desired_state[model]["version"] = canary_version` on
   canary nodes, stable version elsewhere.
3. The node-agent converges to it (pull weights / set vLLM `--model` or HF
   revision), and reports the **actual** version per model in the heartbeat
   reply (`actual.models[].version`); the registry stores it as
   `node_versions`.
4. The daemon passes `node_versions` (actual, not desired) + `canary_policies`
   to `sync_from_fleet`. A node that hasn't converged to `canary_version`
   yet is simply absent from the `-canary` entries — canary membership is
   driven by observed state, never by intent.
5. Rollback = set `canary_traffic_percent: 0` (or drop `canary_version`):
   the `-canary` entries disappear on the next sync and the nodes rejoin the
   stable pool.

## 5. Still to qualify in the lab (real proxy, real nodes)

- [ ] `POST /config/update` hot path against a real proxy (config-only →
      expect the documented `500 "No DB Connected"`; DB-backed → verify the
      router picks up the new `model_list` without restart).
- [ ] With `--num_workers 2`, confirm an admin-endpoint write reaches **all**
      workers (each worker owns its router state) — expected gap, to be
      measured.
- [ ] Measure restart downtime through `platform.sh` (seconds of proxy
      unavailability per fleet change) to size the anti-flap window.
- [ ] Decide `routing_strategy`: `simple-shuffle` (weights honoured) vs
      `least-busy` (current, weights ignored) — see §2.
- [ ] End-to-end canary: node serving `v2`, `{model}-canary` reachable,
      `canary_traffic_percent` enforced by the gateway, rollback by zeroing
      the percent.
- [ ] Node-agent per-model version reporting (`actual.models[].version`)
      and per-model desired version in the push payload (§4).

## 6. Measurements (this session)

- `backend/tests/tier1_unit/test_litellm_reload_canary.py`: **23 passed**
  (routing_weights applied / `None` unchanged / unknown entries ignored /
  invalid rejected / weight change triggers rewrite; canary present/absent/
  invalid / canary weights; `reload_config` hot-OK / hot-KO→restart /
  disabled / no-key; `sync_from_fleet` hot opt-in + cooldown interplay;
  `sync()` default unchanged).
- Regression: `test_fleet_control_loop.py` + `test_fleet_integration.py` +
  `test_litellm_reload_canary.py`: **37 passed**. `test_model_manager.py`:
  10 failures **pre-existing in this environment** — identical count with the
  pristine `litellm_sync.py` (httpx/TestClient URL-parsing mismatch in the
  scratch venv, unrelated to this change); the `test_litellm_sync_*` tests in
  that file pass.
- Hot-reload qualification: source inspection of litellm 1.103.1 (wheel from
  PyPI, same artifact `install.sh` installs); **no live proxy was available
  on this host** — the hot path itself is not measured, only the fallback
  policy around it (mocked).
