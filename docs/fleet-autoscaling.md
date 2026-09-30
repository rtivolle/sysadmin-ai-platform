# Fleet autoscaling (desired-state driven)

The fleet control loop (`backend/services/fleet/litellm_daemon.py`) converges
nodes toward a declarative desired state: per-model policies in
`fleet_desired_state` carry a `replicas` count and the scheduler bin-packs that
many replicas onto healthy nodes. Autoscaling closes the loop one level up —
it watches serving signals and adjusts the `replicas` field itself, so the
next converge cycle (≤ `FLEET_SYNC_INTERVAL_S`, default 10 s) places more or
fewer replicas with no operator action.

Implementation: `backend/services/fleet/autoscaler.py`
(`decide()` — pure, unit-tested; cooldown ledger `autoscale_events`;
quota guardrails). Kill-switch: `FLEET_AUTOSCALE_ENABLED` (default `"1"`).

## Policy fields (per model, in `fleet_desired_state` JSON)

| Field | Default | Meaning |
|---|---|---|
| `replicas` | `min_replicas` | Current desired replica count (written by the autoscaler). |
| `min_replicas` | 1 | Floor — the autoscaler never goes below. |
| `max_replicas` | 4 | Policy ceiling. |
| `target_ttft_s` | 0.5 | TTFT p99 objective, seconds. |
| `scale_up_queue_depth` | 8 | Queue-depth threshold for scale-up. |
| `scale_up_cooldown_s` | 300 | Min seconds between two scale-ups of the same model. |
| `scale_down_cooldown_s` | 900 | Min seconds between two scale-downs (deliberately slower). |
| `team_id` | — | Owning team; drives quota headroom and routing weights. |

## Signals and rules

- **Scale-up** (+1 replica per cycle) when `queue_depth > scale_up_queue_depth`
  **or** `ttft_p99_s > target_ttft_s`, the scale-up cooldown has elapsed, and
  the result stays within bounds.
- **Scale-down** (−1 replica per cycle) when `queue_depth == 0`
  **and** `ttft_p99_s <= target_ttft_s`, the scale-down cooldown has elapsed,
  and the result stays at or above `min_replicas`.
- **No signal → no action.** A model absent from the metrics map is left
  alone; the autoscaler never scales on a guess.
- **Bounds**: every decision is clamped to
  `min_replicas <= new <= min(max_replicas, quota_headroom)`.

Cooldowns are per model and persisted in the `autoscale_events(model, action,
at)` table (created by `autoscaler.ensure_schema()` with
`CREATE TABLE IF NOT EXISTS`; `services/control_store/schema.py` untouched),
so a daemon restart cannot reset the clocks and flap the fleet.

## Quota guardrails (hard)

`quota_headroom = {model: max replicas the quota allows}` is a **hard
ceiling**: `decide()` never returns a count above it. Derivation (chantier 1,
`services/control_store/quota_scopes.py`): the quota model is budget-based
(`daily_tokens`, `monthly_tokens`, `rpm`, `tpm`), so the ceiling is admission —
a team whose budget is exhausted (`check_budget("team", team_id, 0)` denies)
has its headroom **frozen at the current replica count**: the autoscaler may
not add replicas a team cannot pay for. Anything else (no `team_id`, store
unavailable, file mode) falls back to the policy's own `max_replicas`, which
is itself a bound.

Replicas already *above* the ceiling are clamped down immediately, bypassing
cooldowns (recorded as `quota_clamp` in the ledger): overshooting quota is
worse than flapping.

Routing: each cycle also computes quota-aware routing weights via
`distribution.quota_weights(assignments_per_model, team_state)` (chantier 1)
and passes them to `litellm_sync.sync_from_fleet(..., routing_weights=...)`,
so traffic drains away from quota-exhausted teams while the replica count
stays frozen.

## Required observability metrics (collector contract)

The collector (`backend/services/observability/collector.py`) **must** emit
these per-model series — the autoscaler consumes them through
`collector.latest_fleet_metrics()`, expected to return:

```python
{model: {"queue_depth": int, "ttft_p99_s": float, "latency_p99_s": float}}
```

Until that accessor exists (or when the collector is unreachable),
`read_fleet_metrics()` returns `{}` and the autoscaler takes no action.

| Metric | Labels | Source / notes |
|---|---|---|
| `observability_fleet_queue_depth` | `model` | Pending requests across the model's replicas (gauge). Primary scale-up signal. |
| `observability_fleet_ttft_p99_seconds` | `model` | p99 time-to-first-token over the last 60 s per model (gauge). Scale-up/down signal. |
| `observability_fleet_latency_p99_seconds` | `model` | p99 end-to-end latency over the last 60 s (gauge). Recorded for dashboards; not currently a scaling signal. |

These extend the per-node `observability_fleet_*` table in
`docs/runbooks/gpu-fleet.md` §8 (alerting). Suggested alert:
`fleet_queue_saturated` when `observability_fleet_queue_depth > 4 ×
scale_up_queue_depth` for 5 min with no scale-up recorded — means the
autoscaler is capped by `max_replicas` or quota and needs an operator.

## Tuning notes (lab qualification still open)

- The defaults (8 queued requests, 0.5 s TTFT, 300/900 s cooldowns) are
  starting points, **not** measured values — no GPU host was available to
  calibrate them. Qualify against real vLLM serving profiles before trusting
  them in production.
- Scale steps are ±1 per cycle on purpose: with a 10 s loop and a 300 s
  cooldown, a flash crowd ramps at most ~1 replica / 5 min per model. If that
  proves too slow, prefer lowering `scale_up_cooldown_s` per model over
  multi-step jumps.
- `target_ttft_s` should reflect the model's SLO, not the hardware: a 70B
  model will never hit a 0.2 s TTFT, and the autoscaler would then pin it at
  `max_replicas` forever.
