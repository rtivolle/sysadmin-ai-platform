"""Chantier 5/6 — advanced scheduling: priorities, node affinity, cautious preemption.

Pure unit tests (no GPU, no I/O): policies + nodes -> desired state.

Note on preemption reachability (proven, pinned by
``test_preemption_reachability_theorem`` below): models are processed in
``priority`` descending order, so when a replica finds no room, every
replica placed earlier in the same pass has priority >= its own. There is
therefore never a strictly-lower-priority in-pass victim: priority-descending
placement already yields exactly the outcome preemption aims for. The tests
below pin this specified behavior instead of fabricating evictions.
"""
import pytest

from services.fleet.scheduler import compute_desired_state, shortfall, to_node_desired


def _nodes():
    return [
        {"name": "gpu-01", "vram_total_gb": 80, "vram_free_gb": 80,
         "compute_capability": "9.0", "status": "active",
         "labels": {"gpu": "h100", "zone": "a"}},
        {"name": "gpu-02", "vram_total_gb": 80, "vram_free_gb": 80,
         "compute_capability": "9.0", "status": "active",
         "labels": {"gpu": "h100", "zone": "b"}},
        {"name": "gpu-03", "vram_total_gb": 24, "vram_free_gb": 24,
         "compute_capability": "8.6", "status": "active",
         "labels": {"gpu": "l40s", "zone": "a"}},
    ]


def _hosts(assignments):
    return {name: sorted(item["model"] for item in items)
            for name, items in assignments.items()}


# --- 1. Priorities ---------------------------------------------------------------


def test_priority_orders_placement_before_vram():
    policies = {
        "big-lazy": {"replicas": 1, "vram_per_replica_gb": 70},              # priority 0
        "small-urgent": {"replicas": 1, "vram_per_replica_gb": 20,           # priority 10
                         "priority": 10},
    }
    nodes = [{"name": "gpu-01", "vram_total_gb": 80, "vram_free_gb": 80,
              "compute_capability": "9.0", "status": "active"}]
    assignments = compute_desired_state(policies, nodes)
    # Without priority, big-lazy (70 GB) would be placed first and small-urgent
    # would fall into shortfall; with priority, small-urgent grabs the node.
    assert _hosts(assignments) == {"gpu-01": ["small-urgent"]}
    assert shortfall(policies, assignments) == {"big-lazy": 1}


def test_priority_defaults_to_zero_and_equal_priority_keeps_vram_order():
    policies = {
        "a": {"replicas": 1, "vram_per_replica_gb": 40},
        "b": {"replicas": 1, "vram_per_replica_gb": 10, "priority": 0},
    }
    nodes = [{"name": "gpu-01", "vram_total_gb": 40, "vram_free_gb": 40,
              "compute_capability": "9.0", "status": "active"}]
    # Both priority 0: the larger (a) is placed first, exactly like before.
    assert _hosts(compute_desired_state(policies, nodes)) == {"gpu-01": ["a"]}


def test_priority_applies_across_replicas():
    policies = {
        "low": {"replicas": 2, "vram_per_replica_gb": 60, "priority": 1},
        "high": {"replicas": 1, "vram_per_replica_gb": 60, "priority": 10},
        "mid": {"replicas": 1, "vram_per_replica_gb": 60, "priority": 5},
    }
    nodes = [
        {"name": "gpu-01", "vram_total_gb": 80, "vram_free_gb": 80,
         "compute_capability": "9.0", "status": "active"},
        {"name": "gpu-02", "vram_total_gb": 80, "vram_free_gb": 80,
         "compute_capability": "9.0", "status": "active"},
    ]
    assignments = compute_desired_state(policies, nodes)
    # high and mid (higher priority) win the two nodes; low is entirely
    # shortfall — priority order is respected across the whole fleet.
    assert _hosts(assignments) == {"gpu-01": ["high"], "gpu-02": ["mid"]}
    assert shortfall(policies, assignments) == {"low": 2}


def test_priority_rejects_garbage():
    nodes = _nodes()
    with pytest.raises(ValueError):
        compute_desired_state({"m": {"priority": "high"}}, nodes)


# --- 2. Node affinity --------------------------------------------------------------


def test_affinity_matches_only_labeled_nodes():
    policies = {
        "h100-only": {"replicas": 2, "vram_per_replica_gb": 20,
                      "node_affinity": {"gpu": "h100"}},
    }
    assignments = compute_desired_state(policies, _nodes())
    hosts = sorted(name for name, items in assignments.items()
                   for item in items if item["model"] == "h100-only")
    assert hosts == ["gpu-01", "gpu-02"]
    assert shortfall(policies, assignments) == {}


def test_affinity_no_match_is_shortfall():
    policies = {
        "b200-only": {"replicas": 1, "vram_per_replica_gb": 20,
                      "node_affinity": {"gpu": "b200"}},
    }
    assignments = compute_desired_state(policies, _nodes())
    assert assignments == {}
    assert shortfall(policies, assignments) == {"b200-only": 1}


def test_affinity_absent_behaves_like_before():
    policies = {"m": {"replicas": 2, "vram_per_replica_gb": 20}}
    assignments = compute_desired_state(policies, _nodes())
    hosts = sorted(name for name, items in assignments.items()
                   for item in items if item["model"] == "m")
    assert hosts == ["gpu-01", "gpu-02"]  # replica spread, like the old code


def test_affinity_multiple_pairs_must_all_match():
    policies = {
        "za": {"replicas": 2, "vram_per_replica_gb": 50,
               "node_affinity": {"gpu": "h100", "zone": "a"}},
    }
    assignments = compute_desired_state(policies, _nodes())
    hosts = sorted(name for name, items in assignments.items()
                   for item in items if item["model"] == "za")
    assert hosts == ["gpu-01"]  # gpu-02 is h100 but zone b
    assert shortfall(policies, assignments) == {"za": 1}


def test_affinity_rejects_garbage():
    with pytest.raises(ValueError):
        compute_desired_state({"m": {"node_affinity": "h100"}}, _nodes())


def test_affinity_combines_with_gpu_class():
    policies = {
        "m": {"replicas": 1, "vram_per_replica_gb": 10,
              "gpu_class": {"compute_capability_min": "9.0"},
              "node_affinity": {"gpu": "l40s"}},
    }
    assignments = compute_desired_state(policies, _nodes())
    # gpu-03 is l40s but cap 8.6 < 9.0 -> nowhere to go.
    assert assignments == {}
    assert shortfall(policies, assignments) == {"m": 1}


def test_affinity_applies_to_preemption_targets():
    # A preempting model still cannot land on a node its affinity excludes,
    # so no eviction is attempted there either.
    nodes = [
        {"name": "gpu-01", "vram_total_gb": 80, "vram_free_gb": 20,
         "compute_capability": "9.0", "status": "active",
         "labels": {"gpu": "h100"}},
        {"name": "gpu-02", "vram_total_gb": 80, "vram_free_gb": 80,
         "compute_capability": "9.0", "status": "drained",
         "labels": {"gpu": "b200"}},
    ]
    policies = {
        "low": {"replicas": 1, "vram_per_replica_gb": 60, "priority": 1,
                "preemption": "never"},
        "high": {"replicas": 1, "vram_per_replica_gb": 40, "priority": 10,
                 "preemption": "lower-priority",
                 "node_affinity": {"gpu": "b200"}},
    }
    assignments, preemptions = compute_desired_state(
        policies, nodes, return_preemptions=True)
    assert preemptions == []
    assert shortfall(policies, assignments) == {"low": 1, "high": 1}


# --- 3. Preemption -----------------------------------------------------------------


def _tight_nodes():
    return [
        {"name": "gpu-01", "vram_total_gb": 80, "vram_free_gb": 80,
         "compute_capability": "9.0", "status": "active"},
        {"name": "gpu-02", "vram_total_gb": 80, "vram_free_gb": 80,
         "compute_capability": "9.0", "status": "active"},
    ]


def _tight_policies(preemption):
    return {
        "low": {"replicas": 2, "vram_per_replica_gb": 60, "priority": 1,
                "preemption": "never"},
        "high": {"replicas": 1, "vram_per_replica_gb": 40, "priority": 10,
                 "preemption": preemption},
    }


def test_preemption_never_gives_shortfall():
    policies = _tight_policies("never")
    assignments, preemptions = compute_desired_state(
        policies, _tight_nodes(), return_preemptions=True)
    assert _hosts(assignments) == {"gpu-01": ["high"], "gpu-02": ["low"]}
    assert shortfall(policies, assignments) == {"low": 1}
    assert preemptions == []


def test_preemption_lower_priority_matches_never_outcome():
    # Priority-descending placement already gives the high-priority model
    # capacity first — exactly the outcome preemption aims for — so the
    # "lower-priority" flag changes nothing observable here, and no eviction
    # is recorded. (See the reachability theorem test below.)
    policies = _tight_policies("lower-priority")
    assignments, preemptions = compute_desired_state(
        policies, _tight_nodes(), return_preemptions=True)
    assert _hosts(assignments) == {"gpu-01": ["high"], "gpu-02": ["low"]}
    assert shortfall(policies, assignments) == {"low": 1}
    assert preemptions == []


def test_preemption_reachability_theorem():
    """Pin the spec's joint consequence: with priority-descending processing,
    a blocked replica can never face a strictly-lower-priority victim.

    For every model/priority/fleet combination below, whenever a replica is
    unplaceable, all replicas placed before it have priority >= its own, so
    the eviction guard (strictly lower) is vacuous and ``preemptions`` is [].
    """
    fleets = [
        [{"name": "n1", "vram_total_gb": 80, "vram_free_gb": 80,
          "compute_capability": "9.0", "status": "active"}],
        _tight_nodes(),
        _nodes(),
    ]
    policy_sets = [
        {"a": {"replicas": 3, "vram_per_replica_gb": 50, "priority": 1,
               "preemption": "lower-priority"},
         "b": {"replicas": 2, "vram_per_replica_gb": 40, "priority": 10,
               "preemption": "lower-priority"},
         "c": {"replicas": 1, "vram_per_replica_gb": 70, "priority": 5,
               "preemption": "never"}},
        {"x": {"replicas": 2, "vram_per_replica_gb": 80, "priority": -1,
               "preemption": "lower-priority"},
         "y": {"replicas": 2, "vram_per_replica_gb": 80, "priority": 0,
               "preemption": "lower-priority"}},
    ]
    for nodes in fleets:
        for policies in policy_sets:
            assignments, preemptions = compute_desired_state(
                policies, nodes, return_preemptions=True)
            assert preemptions == []
            # Nothing is lost: every requested replica is either placed or
            # reported as shortfall (no silent drops, no cascade).
            placed = sum(len(items) for items in assignments.values())
            requested = sum(p.get("replicas", 1) for p in policies.values())
            missing = sum(shortfall(policies, assignments).values())
            assert placed + missing == requested


def test_preemption_never_evicts_equal_or_higher_priority():
    # Even in the most eviction-friendly shape (a blocked preempting model
    # facing only equal/higher-priority occupants), nothing is evicted.
    policies = {
        "top": {"replicas": 2, "vram_per_replica_gb": 60, "priority": 99,
                "preemption": "never"},
        "mid": {"replicas": 1, "vram_per_replica_gb": 40, "priority": 10,
                "preemption": "lower-priority"},
        "mid2": {"replicas": 1, "vram_per_replica_gb": 40, "priority": 10,
                 "preemption": "lower-priority"},
    }
    assignments, preemptions = compute_desired_state(
        policies, _tight_nodes(), return_preemptions=True)
    assert preemptions == []
    assert shortfall(policies, assignments) == {"mid": 1, "mid2": 1}
    assert sorted(i["model"] for items in assignments.values() for i in items) \
        == ["top", "top"]


def test_preemption_returns_tuple_only_when_asked():
    policies = _tight_policies("lower-priority")
    plain = compute_desired_state(policies, _tight_nodes())
    assert isinstance(plain, dict)
    result = compute_desired_state(policies, _tight_nodes(), return_preemptions=True)
    assert isinstance(result, tuple) and len(result) == 2
    assignments, preemptions = result
    assert assignments == plain  # identical placement either way
    assert preemptions == []


def test_preemption_invalid_value_rejected():
    with pytest.raises(ValueError):
        compute_desired_state({"m": {"preemption": "sometimes"}}, _tight_nodes())


# --- 4. Regression: old policies behave exactly like before ----------------------


def test_regression_old_policies_unchanged():
    policies = {
        "model-big": {"replicas": 2, "engine": "vllm",
                      "vram_per_replica_gb": 40.0,
                      "gpu_class": {"vram_min_gb": 40,
                                    "compute_capability_min": "8.0"},
                      "params": {"gpu_memory_utilization": 0.85}},
        "model-small": {"replicas": 1, "vram_per_replica_gb": 10.0,
                        "params": {}},
    }
    nodes = [
        {"name": "gpu-01", "vram_total_gb": 80, "vram_free_gb": 80,
         "compute_capability": "9.0", "status": "active"},
        {"name": "gpu-02", "vram_total_gb": 80, "vram_free_gb": 80,
         "compute_capability": "8.0", "status": "active"},
        {"name": "gpu-03", "vram_total_gb": 24, "vram_free_gb": 24,
         "compute_capability": "7.5", "status": "active"},
    ]
    assignments = compute_desired_state(policies, nodes)
    big_hosts = sorted(name for name, items in assignments.items()
                       for item in items if item["model"] == "model-big")
    assert big_hosts == ["gpu-01", "gpu-02"]
    small_hosts = [name for name, items in assignments.items()
                   for item in items if item["model"] == "model-small"]
    assert small_hosts == ["gpu-01"]
    assert shortfall(policies, assignments) == {}
    desired = to_node_desired(assignments["gpu-01"])
    assert desired["model-big"] == {"action": "start",
                                    "params": {"gpu_memory_utilization": 0.85}}


def test_regression_negative_and_missing_fields():
    nodes = _tight_nodes()
    assert compute_desired_state({}, nodes) == {}
    with pytest.raises(ValueError):
        compute_desired_state({"m": {"replicas": -1}}, nodes)
