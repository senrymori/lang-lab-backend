#!/usr/bin/env bash
# Deploy from this Mac: both repos to /opt/langlab on the server (code only — no learner data, recordings,
# venvs or git history), then rebuild and restart. Secrets stay in /etc/langlab on the server.
#   ops/deploy.sh [user@host]
set -euo pipefail
HOST=${1:-ubuntu@54.39.111.101}
KEY=~/.ssh/langlab_ed25519
HERE=$(cd "$(dirname "$0")/.." && pwd)
LP=$(cd "$HERE/../learning-lang-platform" && pwd)

ADMIN="$HERE/../lang-lab-admin"
(cd "$ADMIN" && npm ci --no-audit --no-fund --silent && npm run build --silent)
ssh -i $KEY "$HOST" mkdir -p /opt/langlab/lang-lab-admin
rsync -az --delete -e "ssh -i $KEY" "$ADMIN/dist/" "$HOST:/opt/langlab/lang-lab-admin/dist/"

for d in "$HERE" "$LP"; do
  rsync -az --delete -e "ssh -i $KEY" \
    --exclude .git --exclude .venv --exclude .idea --exclude __pycache__ --exclude .pytest_cache \
    --exclude node_modules --exclude /var --exclude /data --exclude /recordings --exclude /desktop \
    "$d/" "$HOST:/opt/langlab/$(basename "$d")/"
done
# the tunnel starts once its token is in /etc/langlab/env
ssh -i $KEY "$HOST" 'cd /opt/langlab/lang-lab-backend/ops && P=$(sudo grep -q ^TUNNEL_TOKEN= /etc/langlab/env && echo "--profile tunnel" || true) \
  && docker compose $P up -d --build && docker image prune -f >/dev/null && docker compose $P ps'
