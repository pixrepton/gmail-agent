#!/usr/bin/env bash
# Idempotent: wire Ollama OpenAI-compat embeddings into production env + recreate worker.
set -euo pipefail

ENV="${1:-/etc/topinstal/gmail-agent.env}"
KEY="OPENAI_COMPAT_EMBEDDING_BASE_URL"
VAL="http://ollama:11434/v1"
REPO="${2:-/opt/gmail-agent/current}"

if [[ ! -f "$ENV" ]]; then
  echo "Missing env file: $ENV" >&2
  exit 1
fi

cp -a "$ENV" "${ENV}.bak.$(date +%Y%m%d%H%M%S)"
if grep -q "^${KEY}=" "$ENV"; then
  sed -i "s|^${KEY}=.*|${KEY}=${VAL}|" "$ENV"
else
  printf '\n# Embeddings (Ollama service on docker-compose.vps network)\n%s=%s\n' "$KEY" "$VAL" >> "$ENV"
fi

echo "=== ${KEY} in ${ENV} ==="
grep -E '^(OPENAI_COMPAT_EMBEDDING_BASE_URL|MAILBOX_MEMORY_VECTOR_ENABLED|OPENAI_COMPAT_EMBEDDING_MODEL|OPENAI_COMPAT_EMBEDDING_DIMENSIONS)=' "$ENV" || true

cd "$REPO"
docker compose --env-file .env.vps -f docker-compose.vps.yml --profile worker up -d --force-recreate gmail-agent-worker
echo "=== worker status ==="
docker ps --filter name=gmail-agent-worker --format 'table {{.Names}}\t{{.Status}}'
