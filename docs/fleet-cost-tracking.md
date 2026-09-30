# Fleet GPU cost tracking (chargeback)

How the platform turns GPU-node runtime into per-model and per-team costs.
Implementation: `backend/services/fleet/cost_tracker.py`,
`backend/services/fleet/cost_router.py`, prices in
`backend/config/fleet/gpu_pricing.yaml`.

## Cost model

**Accrual.** A periodic job calls `CostTracker.accrue(node_name, gpu_class,
hours, day)` once per node per accounting interval (e.g. hourly). Each call
adds one row increment to the durable `gpu_cost_ledger` table, keyed by
`(node_name, day)`:

```
usd(node, day) = Σ hours × price_per_hour(gpu_class)
```

**Attribution.** `cost_summary(scope_type, scope_id, day)` spreads each
node's daily cost across the models hosted on that node, then rolls model
costs up to teams via the `team_id` field of the `fleet_desired_state`
policies.

Node cost → models:

```
share(model, node) = tokens(model, day) / Σ tokens(models on node, day)
cost(model)        = Σ_nodes share(model, node) × usd(node, day)
```

Team cost:

```
cost(team) = Σ cost(model)  for models whose policy team_id == team
```

Fallback: when per-model token usage is unavailable (chantier 1's
`quota_usage_daily` not present, or no tokens recorded that day), shares fall
back to replica counts: `replicas(model, node) / Σ replicas(on node)`.
The returned `attribution` field always says which method was used
(`token_prorata` | `replica_prorata` | `direct` for node scope).

## Limits of the method

- **Token prorata is a proxy, not a measurement.** Tokens are not
  GPU-seconds: a 70B model burns far more compute per token than a 7B one on
  the same node. The attribution answers "who drove usage", not "who burned
  FLOPs".
- **Placements are current, not historical.** Attribution applies today's
  `model_placements` to any requested day. A model moved mid-day is
  attributed as if it had been on the current node all day.
- **Idle cost is fully attributed.** A node that accrued hours but hosted
  nothing known stays visible at node scope only; its cost is not
  redistributed.
- **Prices are indicative.** They come from `gpu_pricing.yaml` and are meant
  to be calibrated against real invoices (see below). Until then, treat every
  USD figure as an estimate.
- **Caller owns interval discipline.** `accrue` is idempotent by
  `(node, day)` in the sense that rows accumulate instead of multiplying —
  but re-accruing the same interval twice counts it twice.

## Adjusting the prices

Edit `backend/config/fleet/gpu_pricing.yaml`:

- `default`: fallback USD/hour for any unlisted class.
- `prices`: per-class USD/hour (`h100-80gb`, `a100-80gb`, `a100-40gb`,
  `l40s`, `rtx4090`, `v100`).
- `aliases`: alternative spellings the node-agent may report as `gpu_model`.

Matching is case-insensitive with non-alphanumerics stripped, plus substring
matching (`NVIDIA H100 80GB HBM3` → `h100-80gb`). Set
`FLEET_PRICING_PATH` to point at a different file. The loader is lazy and
tolerant: a missing or invalid file logs a warning and returns the fallback
price — it never crashes the tracker.

Lab qualification checklist before using these numbers for real chargeback:
replace the indicative prices with measured ones (provider invoices or
hardware amortization + power), reconcile one full day of `gpu_cost_ledger`
against the invoice, and confirm `quota_usage_daily` is populated so the
token-prorata path (not the replica fallback) is the one actually used.
