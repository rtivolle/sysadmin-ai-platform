# ADR-0011: P1 elevation is time-bounded admission priority, not an "unlimited" quota-exempt key

- **Date:** 2026-09-24
- **Status:** Proposed — owner acceptance pending (divergence already recorded in `docs/development.md` §5 and `DEVELOPMENT_PLAN.md` §3)

- **Source of divergence:** `docs/specs/02 — Service Quotas & Passerelle API` (P1 key "exemptée de quota", "priorité maximale et aucun plafond de concurrence"); `docs/plans/DEVELOPMENT_PLAN.md` §3 (S: "P1 'unlimited priority' is neither unlimited nor scheduled").

## Context

Spec 02 provisions a `sk-emergency-incident-p1` key "exempt from quota" with
"maximum priority and no concurrency ceiling" (`max_parallel_requests: 6` in the
example but described as priority without limit). The plan §3 flags that a
metadata label alone is not scheduling priority and requires identity-bound,
expiring P1 elevation with a tested admission policy.

The implementation makes P1 a **time-bounded elevation** of an existing identity,
not a permanent exempt key: `p1_elevation.py` issues a token bound to a named
on-call user and a mandatory `incident_id` (3–32 alphanumeric/hyphen), TTL
≤ 3600 s (Valkey key expiry), with higher-but-finite limits (6 in-flight, 200 RPM,
500k TPM, 10M daily tokens). Revocation deletes the elevation key and every
issued token. P1 never bypasses the sandbox, the command filter, human approval,
or audit; if the backend cannot prioritize running work, P1 is admission
priority, not GPU preemption.

## Decision

Model P1 as expiring, incident-bound, quota-raised (not unlimited) elevation with
single and multi-token revocation, enforced fail-closed against Valkey. Do not
issue a permanent, quota-exempt emergency key.

## Consequences

- **Positive:** No shared permanent emergency credential; elevation is auditable
  and revocable; standard users retain service under P1 load.
- **Negative:** Diverges from the spec's "unlimited/exempt" wording; P1 is not a
  hard scheduling priority unless the backend supports it.

## Evidence

- **Lifecycle and safety invariants:**
  `backend/tests/tier1_unit/test_m3_p1_elevation.py::test_p1_elevation_gate_lifecycle`,
  `::test_p1_mandatory_incident_id_validation`,
  `::test_p1_elevation_expired_fails_closed`,
  `::test_p1_safety_invariants_preserved`,
  `::test_p1_concurrency_reserved_slots`.
- **Revocation (single and multi-token):**
  `backend/tests/tier4_recovery/test_m3_adversarial_p1_dr.py::test_p1_elevation_token_revocation_lifecycle`,
  `::test_p1_elevation_multi_token_revocation`,
  `::test_p1_elevation_ttl_expiration_boundaries`.
- **Does not bypass destructive/approval controls:**
  `backend/tests/tier3_concurrency/test_p1_elevation.py::test_p1_elevation_does_not_bypass_destructive_interceptor`,
  `::test_p1_elevation_does_not_bypass_human_approval`;
  `backend/tests/tier4_recovery/test_m3_adversarial_p1_dr.py::test_destructive_commands_unconditionally_blocked_under_p1`.
- **Fail-closed store outage:** `backend/tests/tier1_unit/test_quota_fail_closed.py::test_shared_quota_outage_rejects_all_admission_checks`.

## Corrective work

- None; the divergence is already documented (`docs/development.md` §5). Record
  the owner's acceptance of bounded, expiring P1 over a permanent exempt key.
