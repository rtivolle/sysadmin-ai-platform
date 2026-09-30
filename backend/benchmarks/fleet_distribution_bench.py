#!/usr/bin/env python3
"""Micro-benchmarks for the quota-aware inference distribution control plane.

Pure functions only — no GPU, DB, or network. Run:
    PYTHONPATH=backend:. /tmp/final-venv/bin/python backend/benchmarks/fleet_distribution_bench.py
"""
import sys, time, statistics

sys.path.insert(0, "backend")
sys.path.insert(0, ".")

from services.fleet.scheduler import compute_desired_state
from services.fleet.distribution import quota_weights
from services.fleet.autoscaler import decide


def make_nodes(n):
    return [
        {"name": f"gpu-{i:02d}", "vram_total_gb": 80.0, "vram_free_gb": 80.0,
         "compute_capability": "9.0", "status": "active",
         "labels": {"gpu": "h100" if i % 2 == 0 else "a100"}}
        for i in range(n)
    ]


def make_policies(m):
    return {
        f"model-{j}": {
            "replicas": 3, "engine": "vllm", "vram_per_replica_gb": 22.0,
            "gpu_class": {"vram_min_gb": 40, "compute_capability_min": "8.0"},
            "priority": j % 3, "team_id": f"team-{j % 4}",
            "min_replicas": 1, "max_replicas": 8,
        }
        for j in range(m)
    }


def bench(fn, *args, rounds=50):
    fn(*args)  # warmup
    times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        fn(*args)
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


print(f"{'case':<28} {'median ms':>10}")
for n_nodes, n_models in [(2, 2), (4, 4), (10, 8), (32, 16)]:
    nodes, policies = make_nodes(n_nodes), make_policies(n_models)
    ms = bench(compute_desired_state, policies, nodes)
    print(f"scheduler {n_nodes}n/{n_models}m{'':<14} {ms:10.2f}")

    assignments = compute_desired_state(policies, nodes)
    per_model = {}
    for node, items in assignments.items():
        for it in items:
            per_model.setdefault(it["model"], []).append({"node": node})
    team_state = {m: {"team_id": f"team-{j % 4}", "remaining_ratio": 0.5,
                      "exhausted": False}
                  for j, m in enumerate(per_model)}
    ms = bench(quota_weights, per_model, team_state)
    print(f"quota_weights {n_nodes}n/{n_models}m{'':<10} {ms:10.2f}")

    metrics = {m: {"queue_depth": 12, "ttft_p99_s": 0.8, "latency_p99_s": 1.2}
               for m in policies}
    headroom = {m: 8 for m in policies}
    ms = bench(decide, policies, metrics, headroom, time.time(), {})
    print(f"autoscaler.decide {n_models}m{'':<14} {ms:10.2f}")
print("done: all pure control-plane functions, sub-millisecond to ms scale")
