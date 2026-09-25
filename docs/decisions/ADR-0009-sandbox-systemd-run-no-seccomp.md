# ADR-0009: Sandbox via systemd-run / manual cgroup v2, no seccomp, 15s+5s deadline

- **Date:** 2026-09-24
- **Status:** Proposed — owner acceptance pending
- **Source of divergence:** `docs/specs/04 — Service Sécurité & Confinement` (plain bwrap runner, no cgroup, claims a seccomp filter, 15 s SIGTERM / 20 s SIGKILL, <10 ms startup); `docs/plans/DEVELOPMENT_PLAN.md` §3 (S7), §4.

## Context

Spec 04's `bwrap-runner.sh` attaches no cgroup and installs no seccomp, yet
claims cgroup limits and a seccomp-BPF filter; it also advertises a 15 s/20 s
timeout and "<10 ms" startup. The plan §3 (S7) calls this out: "The shell script
attaches no cgroup, installs no seccomp filter and implements no timeout.
Implement these explicitly and prove their enforcement."

The implementation's `backend/config/sandbox/bwrap-runner.sh` enforces real
limits on two legs: (1) `systemd-run --user --scope` with `MemoryMax=4G`,
`MemorySwapMax=0`, `TasksMax=128`, `CPUQuota=200%` when available; (2) a manual
cgroup v2 subtree (`memory.max`, `memory.swap.max=0`, `pids.max=128`,
`cpu.max=200000 100000`) otherwise. It still installs **no seccomp filter**
(`docs/security.md` §3: "does not install a seccomp filter"). The deadline is
`timeout --kill-after=5s 15s` — a 15 s run with a 5 s kill grace, **not** the
spec's 20 s SIGKILL. The runner aborts (`126`) if any limit cannot be installed
and read back, and never falls back to unbounded execution.

## Decision

Enforce cgroup v2 resource ceilings via systemd-run when available, otherwise a
manually created subtree, always with `memory.swap.max=0`. Keep a 15 s wall
deadline with 5 s kill grace. Fail closed (exit `126`) when limits cannot be
installed/read back. Do not install a seccomp filter in this prototype.

## Consequences

- **Positive:** Memory/pid/cpu/deadline/network confinement are real and
  kernel-qualified, and `memory.swap.max=0` closes the swap-escape gap
  (`memory.max` alone let total memory grow past the 4 GiB envelope).
- **Negative:** No seccomp syscall filtering — a reviewed seccomp policy from the
  plan is unfulfilled, so the sandbox relies on namespaces + cgroups + cap-drop
  rather than syscall-level restrictions. The 15s/5s timing differs from the
  spec's 20 s. The raw cgroup-delegation leg is verified fail-closed only, not
  measured under a delegated subtree.

## Evidence

- **Runner fail-closed contract:**
  `backend/tests/tier2_sandbox/test_cgroups_limits.py::test_runner_enforces_limits_or_fails_closed`,
  `::test_child_is_in_limited_cgroup_when_host_delegates`,
  `::test_pids_max_bounding_behavior`.
- **Isolation and ceiling behaviour (M4 challenger pack):**
  `backend/tests/tier2_sandbox/test_m4_empirical_challenger.py::test_sandbox_readonly_usr_barrier_fails_closed`,
  `::test_sandbox_network_isolation_external_unreachable`,
  `::test_sandbox_dropped_capabilities_all_zero`,
  `::test_sandbox_etc_write_behavior_discrepancy`.
- **Kernel-backed stress:** `backend/tests/qualification/sandbox_stress.sh`
  (`make stress-sandbox`) measured the OOM kill at the 4 GiB ceiling with
  `memory.swap.max=0`, the 128-task ceiling, the 2-CPU throttle, the 15 s
  deadline, and network denial — on the systemd-run leg.
- **No qualifying seccomp test** — no seccomp filter exists; the raw
  cgroup-delegation leg is fail-closed only (PR-B2 outstanding).

## Corrective work

- PR-B2: run `make stress-sandbox` under the platform account's delegated
  writable subtree to qualify the raw leg.
- Decide whether a seccomp filter is required before production; if so, implement
  and test it (plan S7), or record the owner's acceptance of no-seccomp.
