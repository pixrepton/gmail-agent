#!/usr/bin/env bash
# After sync-p0-to-vps: Neo4j URI, rebuild worker (extract_facts), doctor + fact smoke.
set -euo pipefail

REPO="${1:-/opt/gmail-agent/current}"
ENV="${2:-/etc/topinstal/gmail-agent.env}"

cd "$REPO"

echo "==> Neo4j URI -> docker service hostname"
cp -a "$ENV" "${ENV}.bak.$(date +%Y%m%d%H%M%S)"
if grep -q '^NEO4J_URI=' "$ENV"; then
  sed -i 's|^NEO4J_URI=.*|NEO4J_URI=neo4j://neo4j:7687|' "$ENV"
else
  echo 'NEO4J_URI=neo4j://neo4j:7687' >>"$ENV"
fi
grep -E '^(NEO4J_PILOT_ENABLED|NEO4J_URI)=' "$ENV" || true

echo "==> docker build gmail-agent-worker (includes extract_facts changes)"
docker compose --env-file .env.vps -f docker-compose.vps.yml build gmail-agent-worker

echo "==> recreate worker + nodeb-api"
docker compose --env-file .env.vps -f docker-compose.vps.yml --profile worker --profile api up -d \
  --force-recreate gmail-agent-worker gmail-agent-nodeb-api

echo "==> doctor"
docker compose --env-file .env.vps -f docker-compose.vps.yml run --rm --no-deps gmail-agent-worker \
  python tools/gmail_audit/gmail_intake.py doctor --skip-gmail 2>&1 | tail -5

echo "==> extract_facts smoke (building_type + power_kw)"
docker compose --env-file .env.vps -f docker-compose.vps.yml run --rm --no-deps gmail-agent-worker \
  python -c "
from mailbox_memory_runtime import extract_facts_from_text
text = 'Mam dom jednorodzinny 150m2. Prosze o pompe 12 kW.'
rows = extract_facts_from_text(
    case_id='c', message_id='m', document_id='', text=text,
    source_type='message', source_ref='m',
    observed_at='2026-05-23T12:00:00+00:00', entity_scope='customer', metadata={},
)
for r in rows:
    print(r['fact_key'], r['normalized_value'], r['confidence'])
"
