# Source specifications

The six French design documents that state the original intent for this
platform. Each is available as the original `.docx` and as extracted
`.txt`; the text version is what tools and agents should read.

| # | Specification | Covers | Implemented by | Describing docs |
|---|---|---|---|---|
| 00 | [Plan Directeur & Architecture Globale](00%20-%20Plan%20Directeur%20%26%20Architecture%20Globale.txt) | Global architecture, ten sysadmins, security and fairness principles | Whole platform | [architecture.md](../architecture.md), [glossary.md](../glossary.md) |
| 01 | [Service Inférence (NVIDIA Dynamo & vLLM)](01%20-%20Service%20Infe%CC%81rence%20%28NVIDIA%20Dynamo%20%26%20vLLM%29.txt) | GPU inference, model routing, vLLM/Dynamo | `backend/services/inference_engine/`, LiteLLM upstream config | [services.md](../services.md), [configuration.md](../configuration.md) |
| 02 | [Service Quotas & Passerelle API (LiteLLM, Valkey, Traefik)](02%20-%20Service%20Quotas%20%26%20Passerelle%20API%20%28LiteLLM%2C%20Valkey%2C%20Traefik%29.txt) | TLS front door, identity, concurrency/RPM/TPM/daily quotas, P1 | `backend/services/auth_gateway/`, `backend/config/{traefik,valkey,litellm}/` | [services.md](../services.md), [http-api.md](../http-api.md) |
| 03 | [Service Agent & Outils (DeepSeek Harness & Plugins)](03%20-%20Service%20Agent%20%26%20Outils%20%28DeepSeek%20Harness%20%26%20Plugins%29.txt) | Agent runtime, tool suite, plugin surface | `backend/services/agent_runtime/`, `backend/services/agent_tools/`, `packages/harness-integration/` | [tools.md](../tools.md), [harness-integration.md](../harness-integration.md) |
| 04 | [Service Sécurité & Confinement (Bubblewrap & cgroups v2)](04%20-%20Service%20Se%CC%81curite%CC%81%20%26%20Confinement%20%28Bubblewrap%20%26%20cgroups%20v2%29.txt) | Sandboxing, resource ceilings, workspace isolation, approval | `backend/config/sandbox/`, `backend/services/approval_gate/`, `backend/services/target_adapter/` | [security.md](../security.md) |
| 05 | [Services Stockage & Audit (SeaweedFS & VictoriaLogs)](05%20-%20Services%20Stockage%20%26%20Audit%20%28SeaweedFS%20%26%20VictoriaLogs%29.txt) | Object storage, audit trail, retention, backup/restore | `backend/services/agent_tools/audit.py`, `backend/services/resilience/`, `backend/config/{seaweedfs,victorialogs}/` | [backup-restore.md](../backup-restore.md), [operations.md](../operations.md) |

Every entry also exists beside its `.txt` as the original `.docx`.

Where the implemented system is narrower than these documents, the difference is
recorded deliberately in [development.md §5](../development.md#5-deviations-from-the-design-specifications)
rather than silently patched over.
