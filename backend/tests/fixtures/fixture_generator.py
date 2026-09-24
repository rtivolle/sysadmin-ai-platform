"""
Fixture Generator for Sysadmin AI Platform Automated Test Suite.
Generates all synthetic log files, configuration files, and Markdown runbooks
adhering strictly to spec_miner_survey_3/analysis.md and PROJECT.md.
"""
import os
import hashlib
import json

FIXTURES_DIR = os.path.abspath(os.path.dirname(__file__))
LOGS_DIR = os.path.join(FIXTURES_DIR, "logs")
CONFIG_DIR = os.path.join(FIXTURES_DIR, "config")
RUNBOOKS_DIR = os.path.join(FIXTURES_DIR, "runbooks")

# Also project backend/data paths for runtime lookup
PROJECT_ROOT = os.path.abspath(os.path.join(FIXTURES_DIR, "../../.."))
BACKEND_LOGS = os.path.join(PROJECT_ROOT, "backend/data/logs")
BACKEND_RUNBOOKS = os.path.join(PROJECT_ROOT, "backend/data/runbooks")
BACKEND_CONFIG = os.path.join(PROJECT_ROOT, "backend/config")

def generate_log_fixtures():
    os.makedirs(LOGS_DIR, exist_ok=True)
    os.makedirs(BACKEND_LOGS, exist_ok=True)

    # 1. nginx_error.log (TASK-LOG-01)
    nginx_error = """2026-09-23 10:14:01 [notice] 1420#1420: using the "epoll" event method
2026-09-23 10:14:01 [notice] 1420#1420: nginx/1.24.0
2026-09-23 10:14:01 [notice] 1420#1420: OS: Linux 6.8.0-sysadmin
2026-09-23 10:15:30 [warn] 1423#1423: *118 an upstream response is buffered to a temporary file
2026-09-23 10:15:32 [error] 1423#1423: *120 connect() to 127.0.0.1:9000 failed (111: Connection refused) while connecting to upstream, client: 192.168.1.50, server: ops.sysadmin.internal, request: "GET /api/v1/data HTTP/1.1", upstream: "fastcgi://127.0.0.1:9000", host: "ops.sysadmin.internal"
2026-09-23 10:15:33 [error] 1423#1423: *122 connect() to 127.0.0.1:9000 failed (111: Connection refused) while connecting to upstream, client: 192.168.1.51, server: ops.sysadmin.internal, request: "GET /api/v1/data HTTP/1.1", upstream: "fastcgi://127.0.0.1:9000", host: "ops.sysadmin.internal"
2026-09-23 10:16:00 [notice] 1420#1420: signal 17 (SIGCHLD) received from 1423
"""
    _write(os.path.join(LOGS_DIR, "nginx_error.log"), nginx_error)
    _write(os.path.join(BACKEND_LOGS, "nginx_error.log"), nginx_error)

    # 2. journal_oom.log (TASK-LOG-02)
    journal_oom = """[ 1420.104920] systemd-journald[412]: Suppressed 42 messages from sysadmin-worker.service
[ 1420.105100] kernel: sysadmin-worker invoked oom-killer: gfp_mask=0x1100cca(GFP_HIGHUSER_MOVABLE), order=0, oom_score_adj=0
[ 1420.105115] kernel: CPU: 3 PID: 8410 Comm: sysadmin-worker Not tainted 6.8.0-sysadmin #1
[ 1420.105120] kernel: Hardware name: Mila Sysadmin Server Pro/Standard, BIOS 2.4.0 01/15/2026
[ 1420.105125] kernel: Call Trace:
[ 1420.105130] kernel:  dump_header+0x4a/0x1e0
[ 1420.105135] kernel:  oom_kill_process+0x102/0x240
[ 1420.105140] kernel:  out_of_memory+0x215/0x4e0
[ 1420.105150] kernel: Tasks state (memory values in pages):
[ 1420.105160] kernel: [  pid  ]   uid  tgid total_vm      rss pgtables_bytes swapents oom_score_adj name
[ 1420.105170] kernel: [   1200]     0  1200    12400     1100        98304        0             0 systemd-journal
[ 1420.105180] kernel: [   8410]  1001  8410   240000    35000      1843200        0             0 sysadmin-worker
[ 1420.105190] kernel: [   8492]  1001  8492  1048576   917504      8388608        0             0 python3
[ 1420.105210] kernel: Out of memory: Killed process 8492 (python3) total-vm:4194304kB, anon-rss:3670016kB, file-rss:2048kB, shmem-rss:0kB, UID:1001 pgtables:8192kB oom_score_adj:0
[ 1420.105220] kernel: oom_reaper: reaped process 8492 (python3), now anon-rss:0kB, file-rss:0kB, shmem-rss:0kB
"""
    _write(os.path.join(LOGS_DIR, "journal_oom.log"), journal_oom)

    # 3. postgresql.log (TASK-LOG-03)
    postgresql_log = """2026-09-23 14:20:00.100 UTC [14200] LOG:  checkpoint starting: time
2026-09-23 14:21:45.300 UTC [14200] LOG:  checkpoint complete: wrote 452 buffers (2.8%); 0 WAL file(s) added, 0 removed, 1 recycled
2026-09-23 14:22:00.120 UTC [14210] LOG:  process 14210 still waiting for ShareLock on transaction 889123 after 1000.082 ms
2026-09-23 14:22:00.121 UTC [14218] LOG:  process 14218 still waiting for ExclusiveLock on relation 16402 of database 16384 after 1000.075 ms
2026-09-23 14:22:01.412 UTC [14210] ERROR:  deadlock detected
2026-09-23 14:22:01.412 UTC [14210] DETAIL:  Process 14210 waits for ShareLock on transaction 889123; blocked by process 14218.
	Process 14218 waits for ExclusiveLock on relation 16402 of database 16384; blocked by process 14210.
	Process 14210: UPDATE accounts SET balance = balance - 100 WHERE id = 42;
	Process 14218: UPDATE accounts SET balance = balance + 100 WHERE id = 99;
2026-09-23 14:22:01.412 UTC [14210] HINT:  See server log for query details.
2026-09-23 14:22:01.412 UTC [14210] STATEMENT:  UPDATE accounts SET balance = balance - 100 WHERE id = 42;
"""
    _write(os.path.join(LOGS_DIR, "postgresql.log"), postgresql_log)

    # 4. journal_systemd.log (TASK-LOG-04)
    journal_systemd = """Sep 23 12:00:01 ops-node systemd[1]: Starting Traefik Gateway Service...
Sep 23 12:00:01 ops-node traefik[2980]: 2026-09-23T12:00:01Z INF Traefik version 3.0.0 built on 2026-04-10
Sep 23 12:00:01 ops-node traefik[2980]: 2026-09-23T12:00:01Z ERR command failed error="error while starting server: bind tcp 0.0.0.0:80: address already in use"
Sep 23 12:00:01 ops-node systemd[1]: traefik.service: Main process exited, code=exited, status=1/FAILURE
Sep 23 12:00:01 ops-node systemd[1]: traefik.service: Failed with result 'exit-code'.
Sep 23 12:00:01 ops-node systemd[1]: Failed to start Traefik Gateway Service.
Sep 23 12:00:02 ops-node systemd[1]: traefik.service: Scheduled restart job, restart counter is at 5.
Sep 23 12:00:02 ops-node systemd[1]: traefik.service: Start request repeated too quickly.
Sep 23 12:00:02 ops-node systemd[1]: traefik.service: Failed with result 'start-limit-hit'.
Sep 23 12:00:02 ops-node systemd[1]: Stopped trying to start Traefik Gateway Service.
"""
    _write(os.path.join(LOGS_DIR, "journal_systemd.log"), journal_systemd)

    # 5. auth.log (TASK-LOG-05) - 60 brute-force lines to verify >50 match capping
    auth_lines = [
        "Sep 23 10:55:00 bastion systemd-logind[780]: New session 12 of user sysadmin-01.",
        "Sep 23 10:55:01 bastion sshd[18000]: Accepted publickey for sysadmin-01 from 10.0.0.15 port 54120 ssh2"
    ]
    for i in range(60):
        auth_lines.append(f"Sep 23 11:00:{i:02d} bastion sshd[{18200+i}]: Failed password for invalid user admin from 198.51.100.42 port {48000+i} ssh2")
    auth_lines.append("Sep 23 11:01:05 bastion sshd[18300]: Failed password for root from 198.51.100.42 port 48065 ssh2")
    _write(os.path.join(LOGS_DIR, "auth.log"), "\n".join(auth_lines) + "\n")

    # 6. traefik_debug.log (TASK-LOG-06)
    traefik_debug = """2026-09-23T08:30:10Z INF Traefik entryPoint web listening on :80
2026-09-23T08:30:10Z INF Traefik entryPoint websecure listening on :443
2026-09-23T08:30:15Z DBG github.com/traefik/traefik/v3/pkg/tls/tls.go:124 > Error while creating certificate store: certificate has expired for domain ops.sysadmin.internal
2026-09-23T08:30:16Z ERR http: TLS handshake error from 10.0.2.15:52104: remote error: tls: bad certificate
2026-09-23T08:30:17Z ERR http: TLS handshake error from 10.0.2.16:52108: remote error: tls: bad certificate
"""
    _write(os.path.join(LOGS_DIR, "traefik_debug.log"), traefik_debug)
    _write(os.path.join(LOGS_DIR, "traefik_access.log"), '10.0.2.15 - - [23/Sep/2026:08:30:16 +0000] "GET / HTTP/2.0" 495 0 "-" "-" 1 "-" "-"\n')

    # 7. victorialogs_error.log (TASK-LOG-07)
    victorialogs_error = """2026-09-23T16:44:50.000Z INFO VictoriaLogs server is running at http://127.0.0.1:9428
2026-09-23T16:45:00.120Z FATAL cannot create part file: no space left on device; partition: /vl-data
2026-09-23T16:45:00.121Z ERROR failed to flush in-memory rows to storage part on /vl-data: write /vl-data/parts/20260923: no space left on device
2026-09-23T16:45:00.125Z PANIC cannot commit transaction: read-only file system remount triggered
"""
    _write(os.path.join(LOGS_DIR, "victorialogs_error.log"), victorialogs_error)

    # 8. haproxy.log (TASK-LOG-08)
    haproxy_log = """Sep 23 13:00:00 lb-01 haproxy[1120]: Proxy inference-cluster started.
Sep 23 13:00:10 lb-01 haproxy[1120]: Health check for server inference-cluster/srv-gpu-02 failed, reason: Layer7 check failed: HTTP 500, code: 500, check duration: 45ms
Sep 23 13:00:15 lb-01 haproxy[1120]: Server inference-cluster/srv-gpu-02 is DOWN, reason: Layer7 check failed
Sep 23 13:00:20 lb-01 haproxy[1120]: 10.0.1.20:41200 [23/Sep/2026:13:00:20.100] http-in inference-cluster/<NOSRV> -/-/-/-/+45 503 +212 - - SC-- 1/0/0/0/0 0/0 "POST /v1/chat/completions HTTP/1.1"
"""
    _write(os.path.join(LOGS_DIR, "haproxy.log"), haproxy_log)

    # 9. massive_access_5gb.log (TASK-LOG-09)
    # Generate high-volume text log file with target markers (streaming text file without nulls)
    # and a sparse 5GB companion
    massive_path = os.path.join(LOGS_DIR, "massive_access_5gb.log")
    if not os.path.exists(massive_path) or os.path.getsize(massive_path) < 1000000:
        with open(massive_path, "w", encoding="utf-8") as f:
            filler = "2026-09-23T12:00:00Z [INFO] 192.168.1.1 - GET /api/v1/health HTTP 200 - OK\n"
            f.write(filler * 5000)
            f.write("2026-09-23T12:00:01Z [ERROR] 10.244.15.89 - GET /api/v1/inference HTTP 500 - Internal Server Error\n")
            f.write(filler * 5000)
            f.write("2026-09-23T12:00:02Z [ERROR] 10.244.15.89 - POST /api/v1/chat HTTP 500 - Internal Server Error\n")
            f.write(filler * 5000)

    # 10. app_crash.log (TASK-LOG-10)
    app_crash = """2026-09-23T09:11:58.000Z [INFO] Initializing worker connection pool...
2026-09-23T09:12:00.001Z [ERROR] Worker pool socket leak: open fd count reached 1024 (ulimit ceiling)
2026-09-23T09:12:00.005Z [FATAL] Error: socket: EMFILE: too many open files
2026-09-23T09:12:00.010Z [ERROR] Failed to initiate connection: getaddrinfo EAI_AGAIN dns.internal
2026-09-23T09:12:00.015Z [PANIC] dial tcp: lookup dns.internal on 127.0.0.53:53: read udp: EMFILE: too many open files
"""
    _write(os.path.join(LOGS_DIR, "app_crash.log"), app_crash)

def generate_config_fixtures():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(BACKEND_CONFIG, exist_ok=True)

    # 1. platform_config.json (TASK-CFG-01, TASK-CFG-02)
    platform_config = {
        "num_users": 10,
        "traefik_port": 8080,
        "max_parallel_requests": 2,
        "rpm_limit": 60,
        "daily_token_budget": 2000000
    }
    _write(os.path.join(CONFIG_DIR, "platform_config.json"), json.dumps(platform_config, indent=2) + "\n")
    _write(os.path.join(BACKEND_CONFIG, "platform_config.json"), json.dumps(platform_config, indent=2) + "\n")

    # 2. litellm_config.yaml (TASK-CFG-03)
    litellm_yaml = """model_list:
  - model_name: fast-model
    litellm_params:
      model: openai/Qwen/Qwen2.5-Coder-14B-Instruct
      api_base: http://127.0.0.1:8000/v1
      api_key: mock-key
      rpm: 60
      tpm: 150000
      max_parallel_requests: 2
"""
    _write(os.path.join(CONFIG_DIR, "litellm_config.yaml"), litellm_yaml)

    # 3. docker-compose.aux.yml (TASK-CFG-04)
    compose_yaml = """version: '3.8'
services:
  victorialogs:
    image: victoriametrics/victoria-logs:v0.25.0
    container_name: victorialogs
    ports:
      - "9428:9428"
    volumes:
      - ./backend/data/victorialogs:/vl-data
    deploy:
      resources:
        limits:
          memory: 2G
  seaweedfs:
    image: chrislusf/seaweedfs:latest
    container_name: seaweedfs
    ports:
      - "8333:8333"
      - "9333:9333"
"""
    _write(os.path.join(CONFIG_DIR, "docker-compose.aux.yml"), compose_yaml)

    # 4. dsh-sysadmin.service (TASK-CFG-05, TASK-CFG-06)
    dsh_service = """[Unit]
Description=DeepSeek Harness Sysadmin Agent Service
After=network.target valkey.service

[Service]
Type=simple
User=sysadmin
WorkingDirectory=/opt/sysadmin-platform
ExecStart=/usr/bin/python3 -m backend.services.agent_tools.server
LimitNOFILE=65535
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
"""
    _write(os.path.join(CONFIG_DIR, "dsh-sysadmin.service"), dsh_service)
    _write(os.path.join(CONFIG_DIR, "dsh-agent.service"), dsh_service)

    # 5. nginx_vhost.conf (TASK-CFG-07)
    nginx_vhost = """server {
    listen 80;
    server_name ops.sysadmin.internal;

    location /api {
        proxy_pass http://127.0.0.1:3080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
"""
    _write(os.path.join(CONFIG_DIR, "nginx_vhost.conf"), nginx_vhost)

    # 6. backup.sh (TASK-CFG-10)
    backup_sh = """#!/bin/bash
# Production Backup Script
echo "Executing backup..."
tar -czf /tmp/backup.tar.gz ./data
echo "Backup complete."
"""
    workspace_dir = os.path.join(PROJECT_ROOT, "backend/data/workspaces/sysadmin-01")
    os.makedirs(workspace_dir, exist_ok=True)
    backup_target = os.path.join(workspace_dir, "backup.sh")
    _write(backup_target, backup_sh)
    os.chmod(backup_target, 0o700)
    os.chmod(workspace_dir, 0o700)

def generate_runbook_fixtures():
    os.makedirs(RUNBOOKS_DIR, exist_ok=True)
    os.makedirs(BACKEND_RUNBOOKS, exist_ok=True)

    # 1. nginx_recovery.md (TASK-RBK-01)
    nginx_rbk = """# Runbook: Procédure de Récupération Serveur Web Nginx

## 1. Description du Service
Ce runbook couvre le diagnostic et la reprise sur incident du serveur mandataire inverse Nginx de production.

## 2. Diagnostic Rapide
Vérifier l'état du service et inspecter les dernières erreurs :
```bash
systemctl status nginx
grep -i error /var/log/nginx/error.log
```

## 3. Validation de Configuration
Ne jamais recharger Nginx sans tester la syntaxe :
```bash
nginx -t
```

## 4. Procédure de Redémarrage (Action Modificatrice)
Après validation de la configuration et approbation :
```bash
systemctl restart nginx
```

## 5. Escalade Incident P1
Si l'erreur persiste au-delà de 10 minutes ou impacte le trafic client, déclencher l'astreinte avec la clé prioritaire P1.
"""
    _write(os.path.join(RUNBOOKS_DIR, "nginx_recovery.md"), nginx_rbk)
    _write(os.path.join(BACKEND_RUNBOOKS, "nginx_recovery.md"), nginx_rbk)

    # 2. postgresql_recovery.md (TASK-RBK-02)
    pg_rbk = """# PostgreSQL Operations & Disaster Recovery Runbook

## 1. Architecture Overview
PostgreSQL runs as primary on port 5432 with WAL streaming replication.

## 2. Point-in-Time Recovery
To perform Point-in-Time Recovery (PITR) to a specific target time:
1. Stop the PostgreSQL service: `systemctl stop postgresql`
2. Restore the physical base backup to data directory `/var/lib/postgresql/data`.
3. Configure PITR parameters in `postgresql.conf`:
   - `restore_command = 'cp /mnt/server/archivedir/%f %p'`
   - `recovery_target_time = '2026-09-23 12:00:00 UTC'`
4. Create the required trigger signal file in the data directory:
   `touch /var/lib/postgresql/data/recovery.signal`
5. Start PostgreSQL service to begin WAL replay: `systemctl start postgresql`

## 3. Replication Failover
Promote standby node using `pg_ctl promote`.
"""
    _write(os.path.join(RUNBOOKS_DIR, "postgresql_recovery.md"), pg_rbk)
    _write(os.path.join(BACKEND_RUNBOOKS, "postgresql_recovery.md"), pg_rbk)

    # 3. valkey_operations.md (TASK-RBK-03)
    valkey_rbk = """# Valkey Cache and Quota Store Runbook

## 1. Health Inspection
Check connectivity via `valkey-cli ping`.

## 2. Memory Saturation & Eviction
When Valkey reports `OOM command not allowed when used memory > 'maxmemory'`:
1. Inspect memory distribution:
   `valkey-cli INFO memory`
   `valkey-cli MEMORY USAGE <key>`
2. For token counters and ephemeral sessions, set eviction policy in `valkey.conf`:
   `maxmemory-policy volatile-lru`
3. Notice: Never execute `FLUSHALL` in production as it destroys active rate limiting and session keys.

## 3. Persistence Configuration
RDB snapshots are scheduled every 900 seconds.
"""
    _write(os.path.join(RUNBOOKS_DIR, "valkey_operations.md"), valkey_rbk)
    _write(os.path.join(BACKEND_RUNBOOKS, "valkey_operations.md"), valkey_rbk)

    # 4. disk_reclamation.md (TASK-RBK-04)
    disk_rbk = """# Linux Filesystem Disk Reclamation Runbook

## 1. Disk Space Monitoring
Check partition capacity with `df -h`.

## 2. Emergency Disk Reclamation
When root partition or `/var` exceeds 95% capacity:
1. Safe systemd journal vacuuming:
   `journalctl --vacuum-size=500M`
2. Clear package manager cache:
   `apt-get clean`
3. WARNING: Never run `rm /var/log/*.log` directly on active services. Open file descriptors prevent disk reclamation and may corrupt logging.
4. Mutation note: Executing disk cleanup actions modifies system state and requires Human-in-the-Loop approval.

## 3. Large File Identification
Locate unlinked large files with `lsof +L1`.
"""
    _write(os.path.join(RUNBOOKS_DIR, "disk_reclamation.md"), disk_rbk)
    _write(os.path.join(BACKEND_RUNBOOKS, "disk_reclamation.md"), disk_rbk)

    # 5. tls_certificate_management.md (TASK-RBK-05)
    tls_rbk = """# TLS Certificate Management & Rotation Runbook

## 1. Certificate Inventory
All certificates reside under `/etc/ssl/certs/sysadmin/`.

## 2. Zero-Downtime Certificate Rotation
To apply updated TLS certificates without dropping active TCP connections:
1. For Nginx reverse proxy:
   Issue a graceful reload signal: `systemctl reload nginx` (SIGHUP preserves active connections).
   WARNING: Avoid `systemctl restart nginx` which terminates in-flight connections.
2. For Traefik gateway:
   Traefik dynamic file provider automatically watches certificate paths and reloads certs with zero downtime.

## 3. Expiration Verification
Check certificate expiry: `openssl x509 -enddate -noout -in /etc/ssl/certs/cert.pem`.
"""
    _write(os.path.join(RUNBOOKS_DIR, "tls_certificate_management.md"), tls_rbk)
    _write(os.path.join(BACKEND_RUNBOOKS, "tls_certificate_management.md"), tls_rbk)

    # 6. incident_escalation.md (TASK-RBK-06)
    inc_rbk = """# Incident Escalation & On-Call Protocol

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
"""
    _write(os.path.join(RUNBOOKS_DIR, "incident_escalation.md"), inc_rbk)
    _write(os.path.join(BACKEND_RUNBOOKS, "incident_escalation.md"), inc_rbk)

    # 7. seaweedfs_maintenance.md (TASK-RBK-07)
    weed_rbk = """# SeaweedFS Distributed Storage Maintenance Runbook

## 1. Cluster Status
Check master state at `http://127.0.0.1:9333/cluster/status`.

## 2. Volume Vacuum and Compaction
When disk usage remains high after deleting objects:
1. Connect to master shell:
   `weed shell -master=127.0.0.1:9333`
2. Execute volume vacuuming with garbage threshold:
   `volume.vacuum -garbageThreshold=0.3`
3. Explanation: This command compacts volume files and physically releases freed disk blocks.

## 3. Filer Replication
Inspect replication lag in filer metadata.
"""
    _write(os.path.join(RUNBOOKS_DIR, "seaweedfs_maintenance.md"), weed_rbk)
    _write(os.path.join(BACKEND_RUNBOOKS, "seaweedfs_maintenance.md"), weed_rbk)

    # 8. ssh_hardening.md (TASK-RBK-08)
    ssh_rbk = """# Linux SSH Server Security Hardening Runbook

## 1. Compliance Requirements
All bastion and internal nodes must adhere to zero-trust SSH baselines.

## 2. Baseline Hardening Directives
The 5 mandatory directives in `/etc/ssh/sshd_config` are:
1. `PermitRootLogin no`
2. `PasswordAuthentication no`
3. `X11Forwarding no`
4. `MaxAuthTries 3`
5. `KbdInteractiveAuthentication no`

WARNING: Before terminating your active SSH session, verify configuration validity and test access in a secondary terminal session (`sshd -t`).

## 3. Port Knocking & Fail2ban
Configure fail2ban to ban IPs with > 5 failed attempts in 10 minutes.
"""
    _write(os.path.join(RUNBOOKS_DIR, "ssh_hardening.md"), ssh_rbk)
    _write(os.path.join(BACKEND_RUNBOOKS, "ssh_hardening.md"), ssh_rbk)

    # 9. victorialogs_audit_guide.md (TASK-RBK-09)
    vl_rbk = """# VictoriaLogs Forensic Audit Investigation Runbook

## 1. Ingestion Endpoint
Audit records are streamed via `POST /insert/jsonline?_stream_fields=service,user_id&_time_field=timestamp`.
Default retention period is configured to 90 days (`-retentionPeriod=90d`).

## 2. Forensic Audit Queries
Historical actions are queried using LogsQL on port 9428 (`/select/logsql/query`):
- To query all actions performed by user 'sysadmin-02' that required human approval and had a non-zero exit code:
  `service:dsh-agent AND user_id:sysadmin-02 AND human_approved:true AND exit_code:!0`
- Query syntax explanation:
  * `service:dsh-agent`: restricts to sysadmin agent service stream.
  * `user_id:sysadmin-02`: isolates exact operator account.
  * `human_approved:true`: filters actions that went through approval gate.
  * `exit_code:!0`: pinpoints failed commands.

## 3. Outbox Replay
If VictoriaLogs was temporarily unreachable, outbox replay worker flushes `outbox.jsonl`.
"""
    _write(os.path.join(RUNBOOKS_DIR, "victorialogs_audit_guide.md"), vl_rbk)
    _write(os.path.join(BACKEND_RUNBOOKS, "victorialogs_audit_guide.md"), vl_rbk)

    # 10. disaster_recovery_plan.md (TASK-RBK-10)
    dr_rbk = """# Platform Disaster Recovery & Cold Restore Playbook

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
"""
    _write(os.path.join(RUNBOOKS_DIR, "disaster_recovery_plan.md"), dr_rbk)
    _write(os.path.join(BACKEND_RUNBOOKS, "disaster_recovery_plan.md"), dr_rbk)

def _write(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def generate_all():
    generate_log_fixtures()
    generate_config_fixtures()
    generate_runbook_fixtures()
    print("All fixtures generated successfully.")

if __name__ == "__main__":
    generate_all()
