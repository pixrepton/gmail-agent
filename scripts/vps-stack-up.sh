#!/usr/bin/env bash
# Start the repo-native VPS infrastructure stack.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ENV_FILE="$ROOT/.env.vps"
ENV_EXAMPLE="$ROOT/.env.vps.example"
APP_ENV="$ROOT/tools/gmail_audit/.env"
COMPOSE="$ROOT/docker-compose.vps.yml"

if [[ ! -f "$ENV_FILE" && -f "$ENV_EXAMPLE" ]]; then
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  echo "Created .env.vps from example - replace placeholder passwords before starting." >&2
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE and no example available." >&2
  exit 1
fi

if grep -E 'CHANGE_ME|LOCAL_ONLY' "$ENV_FILE" >/dev/null; then
  echo "Refusing to start: replace placeholder values in $ENV_FILE first." >&2
  exit 1
fi

if [[ ! -f "$APP_ENV" ]]; then
  echo "Missing $APP_ENV. Copy tools/gmail_audit/.env.example and configure app secrets/runtime URLs first." >&2
  exit 1
fi

docker compose --env-file "$ENV_FILE" -f "$COMPOSE" up -d mailbox-memory-db neo4j ollama
docker compose --env-file "$ENV_FILE" -f "$COMPOSE" --profile setup run --rm ollama-pull
docker compose --env-file "$ENV_FILE" -f "$COMPOSE" ps

echo ""
echo "Next proof:"
echo "  docker compose --env-file .env.vps -f docker-compose.vps.yml run --rm gmail-agent-worker"
echo ""
echo "Start 24h worker only after doctor/ingest proof:"
echo "  docker compose --env-file .env.vps -f docker-compose.vps.yml --profile worker up -d gmail-agent-worker"
