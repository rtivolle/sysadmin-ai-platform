# ADR-0013: Audit without an independent integrity/retention-locking archive

- **Date:** 2026-09-24
- **Status:** Proposed — owner acceptance pending
- **Source of divergence:** `docs/specs/05` ("traçabilité médico-légale immuable"); `docs/plans/DEVELOPMENT_PLAN.md` §4 ("Audit outbox -> VictoriaLogs … + independent integrity archive") and §8 (AUD-01).

## Context

Spec 05 calls the audit trail "forensically immutable". The plan §4/§8 requires a
durable outbox into VictoriaLogs **plus** an independent integrity archive —
chained/batched hashes anchored to independently controlled storage with
restricted deletion and tested verification — and explicitly warns that a hash
stored beside mutable logs does not prevent an administrator rewriting both.

The implementation has the durable outbox and VictoriaLogs ingest with 90-day
retention, and an `event_id` dedup, but **no independent integrity archive**.
`docs/security.md` §7 states: "VictoriaLogs is configured with a 90-day
retention period. That is a storage setting, not proof of immutability… The
design calls for an independent integrity archive; it is not implemented here."
This is tracked as PR-D2 in the roadmap.

## Decision

Keep VictoriaLogs (90-day retention) plus the durable outbox as the search and
delivery layer, but do **not** claim immutability. Defer the independent
integrity archive and off-host anchoring to PR-D2.

## Consequences

- **Positive:** Honest labelling — no false "immutable" claim; outbox durability
  is real (at-least-once, `event_id` dedup, poison-pill quarantine).
- **Negative:** Tamper-evident archival and off-host integrity anchoring are
  absent; retention is a storage setting without retention locking or restricted
  deletion.

## Evidence

- **Outbox durability and replay:**
  `backend/tests/tier4_recovery/test_outbox_resilience.py::test_outbox_fallback_on_collector_outage`,
  `::test_outbox_drain_and_replay_worker`,
  `::test_partial_replay_retains_failed_record_and_tail`,
  `::test_concurrent_fallback_writes_preserve_every_record`;
  `backend/tests/tier2_sandbox/test_m4_empirical_challenger.py::test_outbox_complete_transport_outage_durable_buffering`,
  `::test_outbox_poison_pill_quarantine_resilience`.
- **Canonical schema pinned:** `backend/tests/tier1_unit/test_audit_census.py::test_writer_spools_canonical_schema`
  (Python and JS writers).
- **No qualifying test for tamper-evident archival / off-host anchoring** — no
  independent integrity archive exists to test; the 90-day retention is not
  asserted by a test (only configured in `backend/platform.sh`).

## Corrective work

- PR-D2: add chained/batched audit hashes anchored to independently controlled
  storage with restricted deletion and tested verification; obtain the owner's
  retention decision; use the word "immutable" only once storage policy proves it.
