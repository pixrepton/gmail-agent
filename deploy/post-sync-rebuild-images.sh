#!/usr/bin/env bash
# After sync-p0-to-vps: rebuild runtime images so nodeb-api/worker match host tree.
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
ENV_VPS="${GMAIL_ROOT}/.env.vps"

find "${GMAIL_ROOT}/deploy" -name "*.sh" -exec sed -i 's/\r$//' {} + 2>/dev/null || true

cd "${GMAIL_ROOT}"
echo "==> docker compose build (gmail-agent-worker image)"
docker compose --env-file "${ENV_VPS}" -f docker-compose.vps.yml build gmail-agent-worker

echo "==> recreate worker + nodeb-api"
docker compose --env-file "${ENV_VPS}" -f docker-compose.vps.yml --profile worker --profile api up -d \
  --force-recreate gmail-agent-worker gmail-agent-nodeb-api

sleep 3
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'gmail-agent-(worker|nodeb-api)' || true
echo "OK_POST_SYNC_REBUILD"
