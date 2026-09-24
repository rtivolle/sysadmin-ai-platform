# Sysadmin AI Platform — Development Plan

Date: 23 September 2026. Status: proposed implementation baseline.

## 1. Decision and delivery target

Build a local, authenticated assistant for ten sysadmins, delivering log investigation, runbook lookup, configuration validation and reviewed artifact creation first. Production-changing actions follow only after identity, confinement, approval and audit controls pass independent tests.

Start with standalone vLLM on the actual RTX 8000 hardware. Treat Dynamo, prefill/decode disaggregation and advanced KV routing as a separate experiment, not a prerequisite for a useful release. NVIDIA's published supported GPU architectures exclude Turing; this is a support limitation, not proof that every Dynamo component is technically impossible to run. vLLM lists compute capability 7.5 as a minimum, but the exact model, kernels, CUDA build and driver combination still require hardware validation. [S1, S2]

Planning envelope: **10–12 calendar weeks with two full-time engineers**, plus a sysadmin product owner and part-time security reviewer. The backlog below totals **85 engineering person-days before contingency**. Reserve another 20–30% for integration and hardware issues. A single engineer should plan approximately 20–24 weeks. These estimates begin when hardware and identity access are available; they exclude hardware procurement and an unsupported Dynamo port.

The working directory currently contains six Word specifications and no implementation. All six were read. This plan is based on document review and upstream documentation, not on a deployed-system inspection. No GPU benchmark, deployment or security test has been performed.

## 2. Scope and source traceability

| Source document | Requirements retained | Implementation work |
|---|---|---|
| 00 — Plan Directeur & Architecture Globale | On-premises operation, ten users, human approval, fair access | Whole plan; identity, capacity and release gates |
| 01 — Service Inférence | RTX 8000 hardware, 14B/32B models, FP16, streaming | INF-01/02; optional OPT-01 |
| 02 — Service Quotas & Passerelle API | TLS, identity, two in-flight calls/user, RPM/TPM/daily limits, P1 | AUTH-01, QUO-01/02, OPS-02 |
| 03 — Service Agent & Outils | Harness, log search, lint/diff, runbooks | AGT-01/02, TOOL-01/02, ACT-01 |
| 04 — Service Sécurité & Confinement | Network-denied execution, cgroups, approval | SEC-01/02, ACT-01, QA-01 |
| 05 — Services Stockage & Audit | S3, per-user workspaces, audit and retention | DATA-01, AUD-01, OPS-01 |

**MVP:** browser login; persistent isolated sessions; local fast/heavy model selection; streaming and cancellation; uploaded log search; Markdown runbook lookup; JSON/YAML/systemd validation; real unified diffs; approved writes to a user's workspace; per-user quotas; correlated audit; backup/restore.

**Production extension:** narrowly scoped actions on explicitly registered test and production targets, starting with one configuration deployment or service restart workflow. Approval must authorize an exact target and exact change.

**Deferred:** arbitrary privileged shell, unrestricted SSH, autonomous remediation, Kubernetes migration, high availability, PDF/OCR runbook extraction, fine-tuning, vector search and mandatory Dynamo disaggregation. These need separate estimates. Preserve the proposed technologies where validated; do not silently replace them when a gate fails.

## 3. Corrections required before coding

| Finding | Evidence and consequence | Planned correction |
|---|---|---|
| Dynamo is assumed to support all eight Turing GPUs | Published supported architecture list starts at Ampere. The examples use mutable images and cannot establish compatibility. [S1] | Ship the validated vLLM baseline; investigate Dynamo independently. |
| KV-aware benefits are promised without a working event/discovery path | Document 01 combines file discovery and disabled router KV events. Current local-install documentation distinguishes file mode from the event-capable discovery path. [S3] | Require a release-specific configuration and measured cache hits before claiming routing benefits. |
| Driver `>=535` and `latest` images are treated as sufficient | A selected container has its own CUDA/driver requirements. | Pin the entire tested stack, image digests and model revisions; retain a rollback bundle. |
| Valkey is configured as LiteLLM's primary database | Virtual-key setup requires PostgreSQL. [S4] | Add local PostgreSQL for keys and durable control state; use Valkey for compatible distributed counters/leases. |
| `max_budget: 2000` is interpreted as two million tokens/day | LiteLLM budgets are monetary; this configuration does not express the requested token cap. [S5] | Specify and test a separate token ledger and midnight rollover. |
| P1 “unlimited priority” is neither unlimited nor scheduled | The example has limits; a metadata label alone is not evidence of scheduling priority. | Use identity-bound, expiring P1 elevation and a tested admission policy. |
| Authentication is only named | No IdP, LDAP integration, trusted identity propagation or route policy is implemented. | Integrate an existing on-prem IdP through a validated auth adapter; Traefik ForwardAuth delegates authentication. [S6] |
| One service account is assumed to isolate every user's files | `0700` does not separate processes sharing the same UID. | Default to one runtime UID and Harness home per user, with authenticated routing. |
| Tool examples can bypass the proposed shell sandbox | Direct `spawn()` and filesystem reads are present; regex interception covers only certain tool names. | Route every tool through an enforced execution/file-access boundary. |
| Security features are described but absent from the runner | The shell script attaches no cgroup, installs no seccomp filter and implements no timeout. | Implement these explicitly and prove their enforcement. Bubblewrap alone is not a complete security policy. [S7] |
| Lint-and-diff is a stub | Only JSON parsing and byte counts are implemented; YAML/systemd validation and a diff are missing. | Implement actual validators, bounded input and structured diff output. |
| Log output is unbounded | `journalctl` lacks the advertised match cap; output is accumulated in memory, without time or byte limits. | Bound scanning, matches, output, runtime and memory; distinguish no matches from execution failure. |
| Audit storage is called immutable | A logging endpoint and 90-day retention do not establish tamper resistance, access separation or a legal retention requirement. | Add durable delivery, independent archival and integrity verification; have the owner approve retention. |
| Local addresses cross network boundaries incorrectly | `127.0.0.1:8000` inside a bridged LiteLLM container does not address a host-network inference process. | Define service networks and real endpoints; test connectivity from each caller's namespace. |
| “100% FOSS” includes the entire infrastructure | Open application components do not establish the licensing status of GPU drivers, runtime dependencies or model weights. | Produce a component/license inventory and clarify whether the requirement applies to application software or the entire stack. |

Performance claims such as 50% TTFT reduction, sub-10 ms sandbox startup, fixed SeaweedFS memory use and 128 GB of available KV memory are **hypotheses**, not acceptance evidence.

## 4. Proposed implementation architecture

```text
Browser / approved CLI
         |
     TLS :443
         |
Traefik + on-prem authentication adapter / IdP
         |
Trusted user-to-runtime routing
         |
Per-user Harness runtime + home + session store
         |                         |
Admission / token policy           Tool policy + approval state
         |                         |
LiteLLM ---- PostgreSQL            Constrained runner / per-user UID
         |       |                 |              |
         |    durable ledger       bwrap+cgroups   scoped target adapter (later)
         |
Standalone vLLM fast/heavy endpoints

Valkey: shared atomic counters and renewable leases
SeaweedFS: authorized artifacts/runbooks; local workspaces: scratch/editing
Audit outbox -> VictoriaLogs for search + independent integrity archive
```

Use Docker Compose for shared services and systemd for per-user runtimes and controlled sandbox launch. Keep configuration in one repository with idempotent provisioning. A privileged provisioning step creates UIDs, directories and cgroup delegation; the agent receives neither root nor the Docker socket.

Only the front door is reachable from user networks. PostgreSQL, Valkey, model APIs, S3 management, Harness runtime ports and audit ingestion remain on explicit private interfaces/networks. Prefer Traefik's file provider for this small static deployment to avoid mounting the Docker socket. Verify WebSocket/SSE access, cookies, CSRF and Origin handling along with normal HTTP routes.

Use the existing on-prem OIDC provider if present. If only LDAP exists, choose and validate a local adapter in AUTH-01; do not assume Traefik itself implements the whole login flow. Identity comes from the authenticated session, never a model argument or client-supplied user header.

### GPU baseline and capacity

First prove the 14B FP16 model and streaming on a supported build; then load the 32B model. Candidate initial allocation: GPUs 0–3 for heavy TP=4, GPUs 4–5 for fast TP=2, GPUs 6–7 reserved for comparative benchmarks or additional capacity. This assignment is provisional: select actual IDs after topology inspection and compare heavy TP=2 against TP=4. Do not assume all eight GPUs form one NVLink fabric.

Weights alone require roughly 64 GB decimal for 32B FP16 and 28 GB for 14B FP16, before runtime allocations and KV cache. Total board VRAM is not the usable capacity of an individual worker. Record memory per device, allocated KV blocks, startup overhead and peak runtime consumption.

Begin with bounded 8K/16K context trials, then evaluate 32K on heavy requests. Ten users with two allowed calls each means up to twenty admitted calls, not a guarantee of twenty long-context GPU generations at once. Add a bounded global queue and per-model active limits determined by benchmarks. Search logs outside the LLM and send excerpts; never feed multi-gigabyte logs directly into a prompt.

### Quota contract

- User identity aggregates all sessions, keys and model aliases. Default: two in-flight LLM calls, 60 RPM, 150,000 total tokens/minute and 2,000,000 total tokens/day.
- Specify minute limits as rolling 60-second windows. Daily accounting uses an explicit configurable timezone; proposed default `America/Toronto`, requiring owner confirmation. Test daylight-saving transitions.
- Count model input and output, including agent iterations. Reserve prompt tokens plus the allowed maximum output before dispatch; reconcile actual usage on completion. Bound missing `max_tokens` server-side.
- Admission is atomic across workers. Leases have ownership, heartbeat and release semantics. Do not free a slot solely because the client disconnects while generation still runs.
- Cancellation reaches inference. Retries carry attempt identifiers; executed retries consume resources and usage, while duplicate completion events cannot double-charge one attempt. Do not retry a streamed response invisibly after output has begun.
- Record usage durably and reconcile after restart. If final usage is unavailable, retain a conservative charge/reservation until reconciliation; never silently count zero. Attribute a crossing-midnight request to its admission day.
- When enforcement state is unavailable, reject new work with a clear temporary error. Do not bypass limits. Distinguish quota exhaustion (429) from unavailable admission state (503).
- Reuse supported OSS LiteLLM features when they pass the contract tests. Implement only missing policy in an admission component; do not assume commercial features are available.

### Execution and approval contract

Every tool call carries server-assigned `user_id`, `session_id`, `request_id`, `tool_call_id` and policy version. Tools receive validated structured arguments and permitted artifact IDs rather than arbitrary host paths.

Use approved mounts and a minimal runtime filesystem, clean environment, dropped capabilities, no network, no inherited secrets and a reviewed seccomp policy. A cgroup must contain the process **before** untrusted code executes. Initial per-job limits: memory.max 4 GiB, memory.high approximately 3.5 GiB, pids.max 128 and CPU quota equivalent to two CPUs. CPU quota is a time ceiling, not two reserved physical cores. Add aggregate limits so many sandboxes cannot exhaust the host.

Initial deadline: 15 seconds wall time, terminate, then kill the entire process group/cgroup by 20 seconds. Longer tasks require an explicit tool policy. Impose output and disk limits as well. Fail launch when containment cannot be established; do not fall back to direct host execution.

Arbitrary shell is disabled in the first pilot. Read tools operate on approved data; changes require approval. Regex command lists may provide warnings but are not a security boundary. Treat instructions embedded in logs/runbooks as untrusted data.

Approval state: `proposed -> pending -> approved/rejected/expired -> executing -> succeeded/failed/unknown`. Bind approval to user, target, normalized arguments, content hash, original version/hash, expiry and policy version. Recheck authorization and file state at execution. Changed content requires a new approval. Replayed approval cannot launch a second execution. After a crash, investigate `unknown` operations before retrying a side effect.

Workspace editing uses a staged file, real diff, approved content hash and atomic replacement with conflict detection. Production actions use a separate constrained adapter and credential boundary; never grant the sandbox host-control privileges to make `systemctl` work.

## 5. Deliverable-driven backlog

Effort is engineering person-days, excluding contingency. P = platform/inference engineer; A = agent/backend engineer; R = security reviewer; O = sysadmin owner. Review/support roles are additional part-time capacity.

| ID | Deliverable | Owner | Days | Dependencies | Acceptance evidence |
|---|---|---|---:|---|---|
| DISC-01 | Hardware inventory, use cases, licenses and decision records | P+A, O | 3 | None | OS/RAM/disk/topology known; ten-user corpus and scope agreed |
| INF-01 | Turing compatibility spike and pinned manifest | P | 4 | DISC-01 | Actual 14B/32B load, streamed completion, cancellation; selected versions recorded |
| AGT-01 | Harness extension/API feasibility spike | A | 3 | DISC-01 | Pinned build loads a local model adapter, one tool and approval flow; no external provider traffic |
| BASE-01 | Repository, CI, reproducible host/service setup | P | 4 | DISC-01 | Clean staging install, secret-free config, readiness checks and rollback skeleton |
| AUTH-01 | Login, trusted identity and per-user runtime routing | A | 5 | AGT-01, BASE-01 | Two-user spoofing/cross-session tests fail closed; logout and revoked access enforced |
| INF-02 | Fast/heavy serving and load harness | P | 4 | INF-01, BASE-01 | Reproducible benchmark, stable model aliases, bounded context and active limits |
| DATA-01 | Workspaces, S3 policies and artifact access | P | 3 | BASE-01, AUTH-01 | Cross-user access denied; upload caps, path/symlink and archive traversal tests pass |
| QUO-01 | LiteLLM/PostgreSQL/Valkey integration and key lifecycle | P | 3 | BASE-01, AUTH-01 | Keys survive restart; rotation/revocation work; container endpoint tests pass |
| QUO-02 | User-wide admission and token ledger | A | 5 | QUO-01, INF-02 | Concurrent-worker, cancellation, rollover, retry and restart tests pass |
| SEC-01 | Enforced sandbox, cgroups and bounded runner | P, R | 5 | BASE-01 | Network/host access denied; memory/PID/CPU/deadline limits observed in kernel state |
| SEC-02 | Central tool/file policy and isolation tests | A, R | 4 | SEC-01, AUTH-01, DATA-01 | All tool entry points confined; no cross-user file/session/credential access |
| AUD-01 | Audit schema, durable outbox, redaction and archive | P | 4 | BASE-01, DATA-01 | Events survive collector outage; deduplication, restricted reads and integrity check work |
| TOOL-01 | Bounded log search and Markdown runbook reader | A | 4 | SEC-02, AUD-01 | Multi-GB fixture searched without full buffering; bounded cited results; failures distinct |
| TOOL-02 | JSON/YAML/systemd validation and actual diff | A | 4 | SEC-02, AUD-01 | Invalid fixtures rejected; correct unified diff; target hash returned |
| AGT-02 | Browser/session integration and read-only pilot | A | 4 | TOOL-01/02, QUO-02, INF-02, AUTH-01 | Login-to-answer workflow, source links, cancel, restart and error UI demonstrated |
| ACT-01 | Approval UI/state and safe workspace editor | A, R | 5 | AGT-02, AUD-01 | Reject/expire/replay/stale-file cases fail safely; approved exact edit succeeds |
| OPS-01 | Metrics, alerts, offline bundle and recovery | P | 4 | DATA-01, QUO-02, AUD-01, INF-02 | Clean-machine restore, local-only startup and failed-service alerts demonstrated |
| OPS-02 | Expiring P1 role and fair emergency admission | P+A, O | 3 | QUO-02, AUTH-01, AUD-01 | Incident ID, expiry and audit enforced; standard users retain service under P1 load |
| ACT-02 | One narrowly scoped target action | A, R+O | 5 | ACT-01, OPS-01 | Staging target allowlist, approval, least privilege, failure recovery and rollback demonstrated |
| QA-01 | Security, ten-user load, recovery and soak qualification | P+A, R+O | 6 | All release features | Signed test report; no unresolved critical isolation or data-integrity failures |
| REL-01 | Pilot feedback, operator training and release bundle | P+A, O | 3 | QA-01 | Release manifest, runbooks, ownership, restore evidence and pilot acceptance |

Optional **OPT-01**, after INF-02: a maximum five-person-day Dynamo feasibility experiment. Required evidence: viable hardware/software combination, correct discovery and KV events, cancellation, cache behavior and transport compatibility. Investigate TP=2 prefill / TP=4 decode only if the selected connector explicitly supports that topology. Stop and record a no-go if the supported configuration cannot be established; do not spend the release contingency on an unbounded port. An A/B improvement target of at least 20% in the agreed latency or throughput metric is proposed before accepting the extra operational complexity. No quality, fairness or stability regression is allowed.

## 6. Milestones and release gates

The weeks below are planning targets with parallel work by two engineers, not fixed delivery promises.

| Milestone | Target | Exit gate |
|---|---|---|
| M0 — Feasibility | Weeks 1–2 | Hardware access; one validated inference stack; real Harness integration; documented go/no-go decisions |
| M1 — Secure foundation | Weeks 3–5 | Authenticated separate users, private services, enforced sandbox, persistent keys, initial audit |
| M2 — Read-only vertical slice | Weeks 6–7 | Browser -> identity -> quota -> model -> bounded tool -> cited answer -> audit, including cancellation |
| M3 — Reviewed changes | Weeks 8–9 | Workspace approval/editing; one staging target adapter; P1 and recovery controls |
| M4 — Production qualification | Weeks 10–12 | Security/load/restore gates pass; staged pilot and release accepted |

Critical dependency chain: DISC-01 -> AGT-01/BASE-01 -> AUTH-01 -> DATA-01/SEC-02 -> TOOL-01/02 -> AGT-02 -> ACT-01 -> ACT-02 -> QA-01 -> REL-01. Inference and quota work must converge before AGT-02 completes.

If M0 inference fails, retain application work against a local fake OpenAI-compatible endpoint while resolving the stack; do not claim the product is ready. If Harness extension or isolation cannot be established, document the blocker and estimate a narrowly scoped custom runtime alternative before committing to it. If capacity fails, lower admitted concurrency/context or revisit hardware with measured evidence; retain per-user fairness.

## 7. Verification plan and measurable acceptance

### Functional and model quality

Create a versioned evaluation pack with 30 anonymized tasks: ten incident/log investigations, ten configuration/script tasks and ten runbook retrieval tasks. The sysadmin owner supplies expected outcomes and required evidence. Initial target: at least 24/30 meet the rubric, with zero unauthorized executions; this is a proposed product threshold, not a measured model capability. Validate tool-call parsing and malformed arguments, not only natural-language completion.

Tool results include source/artifact ID, line range or journal cursor, truncation status, return code and execution duration. `config_lint_and_diff` runs real validators in containment. Disable third-party plugin/network access in validators. Limit archive extraction and uploads; do not automatically load user-supplied code or model repositories.

### Security gates — mandatory

- User A cannot access B's sessions, runtime endpoints, workspace, S3 objects, approvals or virtual keys, including guessed IDs and symlink races.
- Forged identity headers, unapproved origins and direct backend access do not bypass authentication.
- Every tool path, including direct filesystem and subprocess helpers, passes the same authorization and containment policy.
- Host writes, network access, privilege acquisition and access to service credentials fail inside the sandbox.
- Bounded process/memory/CPU/disk abuse tests leave the host and other users functional. Run adversarial tests only on disposable staging systems; never copy the document's fork bomb onto an unverified production runner.
- Approval content/target substitution, replay, expiry and concurrent modifications are rejected. Prompt injection in documents cannot authorize an action.
- Secrets are absent from responses, logs and artifacts. Security logs identify denied operations without storing unnecessary confidential payloads.

### Load and latency gates

Use recorded prompt lengths of 1K, 8K and 16K tokens, plus a separate 32K exploratory case. Cap test output at 512 tokens. Test fast-only, heavy-only and a 70/30 fast/heavy mix with 1, 5 and 10 users, then twenty admitted calls with overload queued or rejected predictably. Include cold and warm cache runs.

Measure end-to-end TTFT (including admission wait), decode rate, completion latency, queue duration, per-user served work, VRAM/RAM, error rate and cancellation cleanup. Provisional targets for ten-user mixed load: p95 TTFT <=10 seconds for fast requests up to 1K input and <=30 seconds for heavy requests up to 8K input; successful admitted requests >=99% excluding deliberate cancellations and quota rejections; zero OOMs. Confirm or revise targets with O after INF-02, before making operational commitments. Do not hide queue time or exclude slow requests from latency results.

Run a two-hour repeatable load test and a 24-hour pilot soak. Verify that no user exceeds two active calls across sessions and that restarting a worker does not leak quota slots indefinitely. Set the stale-lease recovery interval from measured cancellation behavior and test it explicitly.

### Failure, sovereignty and recovery gates

Inject inference failure, Valkey unavailability, PostgreSQL restart, lost SSE connections, audit outage, full disk and agent restart. New privileged actions must fail closed when required authorization or durable audit recording is unavailable. Audit collector outage may use the durable outbox; outbox exhaustion blocks new actions.

Start and operate with public egress blocked, using mirrored images/models/packages, local DNS/NTP/PKI and disabled telemetry/cloud fallbacks. Distinguish a controlled artifact-import process from runtime data egress. Capture firewall evidence for the acceptance report.

Proposed recovery objectives: RPO <=24 hours for durable data and RTO <=4 hours, subject to owner acceptance and actual dataset size. Back up PostgreSQL, Harness session stores, S3 data/metadata, audit archives, configuration and protected secrets to a separate failure domain. Restore into a clean staging environment and measure actual recovery time. One server remains a single point of failure; backups do not provide high availability.

## 8. Audit, operations and P1 details

Use a versioned event schema: event ID, UTC timestamp, user/session/request/tool/attempt IDs, target, policy version, approval ID and disposition, input/output hashes, exit status, duration, model revision and token usage. Redact before persistence; store large sensitive output as an authorized artifact with a reference. Retention is configurable; treat 90 days as the proposed requirement awaiting the data owner's decision.

VictoriaLogs provides query access. Integrity requires an additional trust boundary: chained/batched hashes anchored to an independently controlled archive, restricted deletion and tested verification. A hash stored beside mutable logs does not prevent an administrator from rewriting both. Do not label the result immutable unless storage policy and retention enforcement prove that property. Verify the selected archival mechanism rather than assuming S3 compatibility includes retention locking.

P1 elevation requires a named on-call identity, incident ID and short expiry. Proposed default: 60 minutes, explicit renewal, higher finite quotas and one reserved execution slot when capacity allows. It never bypasses tool approval, isolation or audit. If the selected backend cannot prioritize running work, describe P1 as admission priority, not GPU preemption. Do not share a permanent emergency API key.

Expose operational metrics through a small local collector and an approved local viewer/alert destination; select exact components during BASE-01 with the license inventory. Monitor queue delay, errors, quota rejects, GPU/RAM/disk saturation, expired leases, audit backlog and backup age. Define an owner and response runbook for each actionable alert.

## 9. Repository and implementation conventions

Proposed tree; these paths are deliverables to create during development, not existing implementation:

```text
infra/ansible/                 host/users/cgroups/firewall provisioning
infra/compose/                 shared services and private networks
infra/systemd/                 per-user runtimes and runner services
config/                        validated templates; no secrets
packages/harness-integration/  version-pinned adapters and profile
packages/tool-policy/          schemas, authorization and approval
packages/sysadmin-tools/       search, runbooks, lint, diff, editor
services/admission/            only policy missing from validated LiteLLM
services/target-adapter/       tightly scoped operations
schemas/                      tool, approval and audit contracts
benchmarks/                   workloads, driver and measured results
tests/{unit,integration,e2e,security}/
docs/adr/                     choices, alternatives and evidence
docs/runbooks/                deploy, restore, rotate, upgrade, incident
releases/                     manifests and evidence references
```

Use TypeScript for Harness integration and tool contracts, matching the pinned upstream APIs; shell/systemd and Ansible for provisioning. Select the admission-service language based on the verified LiteLLM extension point in QUO-01. Avoid copying the sample `ctx.*` signatures without compiling against the chosen Harness revision: upstream identifies it as a changing developer preview. [S8]

CI must validate types, schemas, service configuration, secret scanning and meaningful unit/integration tests. A separate Linux staging runner performs sandbox and namespace tests; a GPU runner performs model and load tests. macOS-only checks cannot establish Linux confinement or GPU correctness. Every release records image digests, dependency lockfiles, model/tokenizer revisions, kernel/driver versions, migrations and rollback instructions.

## 10. First ten working days

| Window | Platform engineer | Agent/backend engineer | Reviewable output |
|---|---|---|---|
| Days 1–2 | Inventory host/topology/resources and collect driver state | Convert requirements into contracts and select evaluation fixtures with owner | Inventory, threat boundary sketch, open decisions |
| Days 3–5 | Prove 14B then 32B load; record CUDA/kernel failures | Pin Harness, wire local adapter, compile one actual plugin and approval test | Compatibility evidence and integration demonstration |
| Days 6–8 | Compare TP layouts and package repeatable baseline | Prove two isolated runtime homes/UIDs and authenticated routing approach | Draft manifests, isolation test and architecture records |
| Days 9–10 | Begin reproducible staging setup and endpoint connectivity tests | Specify admission/approval/audit schemas; establish integration tests | M0 review, prioritized M1 tickets and revised estimates |

Required inputs, to collect during DISC-01 without blocking independent code scaffolding: SSH access to the GPU host; OS/kernel/CPU/RAM/disk and GPU topology; IdP/LDAP details and test accounts; local DNS/certificates; representative anonymized logs/runbooks; allowed target systems/actions; team availability; license interpretation; retention and recovery expectations. Do not infer these from unrelated machines or earlier projects.

## 11. Upstream references checked

References were inspected on 23 September 2026. Version-specific implementation decisions must record the exact release used, because live documentation changes.

- **S1:** [NVIDIA Dynamo compatibility](https://docs.nvidia.com/dynamo/latest/reference/compatibility) and [local installation requirements](https://docs.nvidia.com/dynamo/cli/installation/install-dynamo): supported architectures and release-specific prerequisites.
- **S2:** [vLLM GPU installation](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/): minimum NVIDIA compute capability; not an end-to-end certification of the proposed deployment.
- **S3:** [Dynamo local installation](https://docs.nvidia.com/dynamo/cli/installation/install-dynamo): discovery modes and event/routing differences.
- **S4:** [LiteLLM virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys): PostgreSQL-backed virtual-key setup.
- **S5:** [LiteLLM budgets and rate limits](https://docs.litellm.ai/docs/proxy/users): monetary budget semantics and rate-limit mechanisms.
- **S6:** [Traefik ForwardAuth](https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/forwardauth/): delegated authentication.
- **S7:** [Bubblewrap security model](https://github.com/containers/bubblewrap): caller-defined sandbox policy and limitations.
- **S8:** [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) and [architecture](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md): developer-preview status and plugin/profile composition.

The NVIDIA skill catalog was also checked. Its Dynamo deployment/router/troubleshooting skills are execution-oriented; no additional skill installation is required for this planning deliverable.
