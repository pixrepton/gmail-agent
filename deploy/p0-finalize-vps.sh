#!/usr/bin/env bash
set -euo pipefail

GMAIL_ROOT=/opt/gmail-agent/current
cd "$GMAIL_ROOT"
tar -xzf /tmp/gmail-agent-p0-final.tgz

sed -i 's/\r$//' deploy/*.sh 2>/dev/null || true
chmod +x deploy/*.sh

if [[ ! -f /etc/topinstal/gmail-agent.env ]]; then
  bash deploy/migrate-gmail-agent-env.sh
else
  echo "SKIP migrate: /etc/topinstal/gmail-agent.env exists"
fi

export DEBIAN_FRONTEND=noninteractive
if ! command -v caddy >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq caddy
fi
if [[ ! -f /etc/caddy/Caddyfile ]]; then
  printf '{\n\tadmin off\n}\n' > /etc/caddy/Caddyfile
fi
bash deploy/render-nodeb-caddy.sh "$GMAIL_ROOT/deploy/Caddyfile.nodeb-rag.template"

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q active; then
  ufw allow from 46.224.235.86 to any port 8443 proto tcp comment 'RAG NodeB API' || true
fi

docker compose --env-file .env.vps -f docker-compose.vps.yml build gmail-agent-worker
docker compose --env-file .env.vps -f docker-compose.vps.yml --profile worker --profile api up -d --force-recreate gmail-agent-worker gmail-agent-nodeb-api
docker restart gmail-agent-nodeb-api 2>/dev/null || true

sleep 4
docker exec -e PYTHONPATH=/app/tools/gmail_audit -w /app/tools/gmail_audit gmail-agent-nodeb-api \
  python scripts/backfill_correlation_registry.py --from-orchestrator-workflows

echo "==> verification"
docker exec -i gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -f - <<'SQL'
SELECT count(*) AS cieplo_workflow_links FROM correlation_links WHERE link_type = 'cieplo_workflow';
SQL

EID=$(docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -t -A -c \
  "SELECT engagement_id FROM topinstal_engagements LIMIT 1")
TOKEN=$(grep -m1 '^NODE_B_REGISTRY_TOKEN=' /etc/topinstal/gmail-agent.env | cut -d= -f2-)
curl -sf -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:8765/health"
echo
curl -sf -H "Authorization: Bearer ${TOKEN}" "https://127.0.0.1:8443/health" --insecure | head -c 200 || \
  curl -sf -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:8443/health" | head -c 200
echo
curl -sf -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:8765/engagements/${EID}/snapshot" -o /dev/null -w 'snapshot_local=%{http_code}\n'
curl -sf -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:8443/engagements/${EID}/snapshot" -o /dev/null -w 'snapshot_caddy=%{http_code}\n' || true

echo OK_P0_FINALIZE
