#!/usr/bin/env bash
# Nightly on the server (cron: ops/langlab-backup.cron): the database and the learners' files,
# kept 14 days on the server and copied to Cloudflare R2 once its rclone remote "r2" is set up in
# /etc/langlab/rclone.conf (provider Cloudflare, list_version = 2; the R2 token allows only this bucket
# and the server's IPv4 and IPv6 — it reaches R2 over IPv6). Voice recordings aren't backed up: they live a week anyway (app/cleanup.py).
#   install: sudo install -o root -m 644 /opt/langlab/lang-lab-backend/ops/langlab-backup.cron /etc/cron.d/langlab-backup
set -euo pipefail
umask 077  # the dumps hold learners' addresses
cd /opt/langlab/lang-lab-backend/ops
D=/var/backups/langlab
T=$(date -u +%F)
mkdir -p -m 700 "$D"
docker compose exec -T db pg_dump -U langlab -Fc langlab > "$D/db-$T.dump"
docker run --rm -v langlab_data:/data:ro alpine:3.22 tar czf - -C /data learners > "$D/learners-$T.tar.gz"
find "$D" -type f -mtime +14 -delete
if rclone listremotes --config /etc/langlab/rclone.conf 2>/dev/null | grep -q '^r2:'; then
  rclone copy --config /etc/langlab/rclone.conf "$D" r2:langlab-backups --max-age 2d
fi
echo "$(date -u +%FT%TZ) backup $T ok"
