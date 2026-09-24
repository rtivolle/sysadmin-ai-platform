# Linux Filesystem Disk Reclamation Runbook

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
