# Fleet preemption policy (scheduler)

How `backend/services/fleet/scheduler.py` decides which model replicas may
displace others when GPU VRAM runs out. See `docs/gpu-fleet.md` for the
overall fleet architecture.

## The three scheduling knobs (per-model policy fields)

```yaml
qwen2.5-coder-32b:
  replicas: 2
  vram_per_replica_gb: 40
  priority: 10                       # int, default 0 — higher wins
  node_affinity: {gpu: "h100"}       # default: no constraint
  preemption: "lower-priority"       # "never" (default) | "lower-priority"
```

* **Priority.** Models are processed `priority` descending, then
  `vram_per_replica_gb` descending. A priority model is placed first, so on
  a tight fleet it wins capacity before lower-priority models are even
  considered. Default `0`; equal priorities keep the historical
  biggest-first order.
* **Node affinity.** A node is eligible only if its `labels` dict contains
  every `node_affinity` key/value pair. No affinity = every node eligible,
  exactly like before.
* **Preemption.** What happens when a replica finds no eligible node with
  enough free VRAM.

## When preemption triggers

Only when **all** of these hold:

1. The model's policy sets `preemption: "lower-priority"` (`"never"` is the
   default and keeps the historical behavior: the replica is dropped and
   reported via `shortfall()`).
2. At least one replica of the model cannot be placed on any eligible node
   (placeable status `approved`/`active`, `gpu_class`, `node_affinity`,
   free VRAM all satisfied except capacity).
3. Some already-placed replica has **strictly lower** `priority` and
   evicting the smallest such set from a single eligible node frees enough
   VRAM.

Evicted replicas are dropped from the desired state — they surface as
`shortfall()` for their own model and the daemon converges the node away
from them. Every eviction is returned for audit when the caller passes
`return_preemptions=True` (default `False`, return shape unchanged):

```python
assignments, preemptions = compute_desired_state(policies, nodes,
                                                 return_preemptions=True)
# preemptions: [{"node", "evicted_model", "evicted_priority",
#                "for_model", "for_priority", "vram_freed_gb"}, ...]
```

## Safeguards — why "cautious"

* **Strictly lower priority only.** Equal or higher priority is never
  evicted, no matter how much VRAM it would free.
* **Single level, no cascade.** An evicted replica is dropped, never
  re-placed, so it cannot evict anyone else. There is no chain reaction.
* **Minimal eviction.** The scheduler evicts the fewest replicas that free
  enough VRAM, preferring the lowest-priority ones on one node.
* **Eligibility still applies.** Eviction targets only nodes that are
  otherwise eligible for the preempting model (status, `gpu_class`,
  `node_affinity`); a drained node or a wrong-GPU node is never touched.
* **Default-off.** `preemption: "never"` is the default: existing policies
  behave byte-for-byte like before this change.

## Honest limitation: reachability in a stateless pass

`compute_desired_state` is a pure, stateless recompute: it does not know
what is *running*, only what is *requested*. Because models are processed in
priority-descending order, a blocked replica's already-placed competitors in
the same pass always have priority >= its own — so the in-pass eviction
above cannot trigger from placement order alone. In practice the
priority-descending order already produces the outcome preemption aims for:
the higher-priority model is placed first and lower-priority models take the
shortfall.

Observable preemption of *running* lower-priority replicas (the real
production scenario: a new high-priority model arrives while the fleet is
full of lower-priority work from previous loops) requires the caller to
expose current placements to the scheduler. That contract lives on the
daemon side (`litellm_daemon.py`, owned by another workstream) and is
tracked as follow-up work, not implemented here.

## How to enable per model

```yaml
# fleet_desired_state (JSON/YAML), written by the operator
critical-model:
  replicas: 2
  vram_per_replica_gb: 40
  priority: 100
  preemption: "lower-priority"   # may displace strictly-lower-priority replicas
```

Keep `preemption: "never"` (or omit it) for everything that must never
displace another model. Prefer raising `priority` over enabling preemption:
priority decides placement order deterministically, while preemption is the
last-resort lever for capacity contention.
