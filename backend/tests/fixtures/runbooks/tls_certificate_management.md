# TLS Certificate Management & Rotation Runbook

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
