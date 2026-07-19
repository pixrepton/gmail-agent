#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
ENV_FILE="$ROOT/.env.mailbox-memory"
COMPOSE="$ROOT/docker-compose.mailbox-memory.yml"

if [[ -f "$ENV_FILE" ]]; then
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE" down
else
  docker compose -f "$COMPOSE" down
fi
