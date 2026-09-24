# Platform Disaster Recovery & Cold Restore Playbook

## 1. Disaster Recovery Objectives
- Recovery Time Objective (RTO): < 4 hours.
- Recovery Point Objective (RPO): < 24 hours.

## 2. Phase 2: Service Restoration Sequence
When restoring the platform onto a clean host from backup, restore services in this mandatory order:
1. Host cgroups & sandbox layout (Bubblewrap and delegation setup).
2. Valkey & PostgreSQL state stores (restore dump.rdb and basebackup).
3. SeaweedFS storage engine (restore master and volume metadata).
4. VictoriaLogs audit engine (restore storage directory and outbox).
5. Inference engine (start local vLLM / mock server).
6. LiteLLM gateway & quotas (start LiteLLM proxy).
7. Traefik front door & ForwardAuth (start ingress routing).

## 3. Post-Restore Verification Drill
Execute the automated test suite to confirm operational readiness across all tiers.
