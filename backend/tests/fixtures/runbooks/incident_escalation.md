# Incident Escalation & On-Call Protocol

## 1. Severity Classifications
- P1: Total service outage or data corruption.
- P2: Degraded performance affecting multiple sysadmins.

## 2. P1 Critical Escalation
During active P1 outages, on-call engineers may obtain temporary priority admission:
- Key ID: `emergency-p1-oncall`
- Prerequisites: Active PagerDuty or Jira incident ID and named on-call sysadmin.
- Time-to-Live (TTL): Automatically expires after 60 minutes.
- Limits: Elevated ceiling (6 concurrent calls, 500,000 TPM).
- Audit Tagging: All actions are tagged with `priority: P1-CRITICAL` in VictoriaLogs.
- Safety Boundary: P1 elevation bypasses concurrency limits but DOES NOT bypass Bubblewrap sandboxing or human approval for mutating commands.

## 3. Post-Incident Review
A post-mortem document must be drafted within 24 hours of incident resolution.
