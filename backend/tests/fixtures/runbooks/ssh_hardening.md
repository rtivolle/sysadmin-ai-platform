# Linux SSH Server Security Hardening Runbook

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
