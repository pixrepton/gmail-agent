#!/usr/bin/env bash
# Idempotent: point Neo4j pilot at docker-compose service hostname + recreate worker/api.
set -euo pipefail

ENV="${1:-/etc/topinstal/gmail-agent.env}"
KEY="NEO4J_URI"
VAL="neo4j://neo4j:7687"
REPO="${2:-/opt/gmail-agent/current}"

if [[ ! -f "$ENV" ]]; then
  echo "Missing env file: $ENV" >&2
  exit 1
fi

cp -a "$ENV" "${ENV}.bak.$(date +%Y%m%d%H%M%S)"
if grep -q "^${KEY}=" "$ENV"; then
  sed -i "s|^${KEY}=.*|${KEY}=${VAL}|" "$ENV"
else
  printf '\n%s=%s\n' "$KEY" "$VAL" >> "$ENV"
fi

echo "=== Neo4j env (no secrets) ==="
grep -E '^(NEO4J_PILOT_ENABLED|NEO4J_URI|NEO4J_DATABASE)=' "$ENV" || true

cd "$REPO"
docker compose --env-file .env.vps -f docker-compose.vps.yml --profile worker up -d --force-recreate gmail-agent-worker
docker compose --env-file .env.vps -f docker-compose.vps.yml --profile api up -d --force-recreate gmail-agent-nodeb-api 2>/dev/null || true
echo "=== containers ==="
docker ps --filter name=gmail-agent --format 'table {{.Names}}\t{{.Status}}' | head -8
