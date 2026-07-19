#!/usr/bin/env bash
# Start Mailbox Memory Postgres (repo root). POSIX / Git Bash.
# Creates .env.mailbox-memory from example if missing, then fails closed until the placeholder password is replaced.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
ENV_FILE="$ROOT/.env.mailbox-memory"
EXAMPLE="$ROOT/.env.mailbox-memory.example"
COMPOSE="$ROOT/docker-compose.mailbox-memory.yml"

if [[ ! -f "$ENV_FILE" && -f "$EXAMPLE" ]]; then
  cp "$EXAMPLE" "$ENV_FILE"
  echo "Created .env.mailbox-memory from example - replace MAILBOX_MEMORY_POSTGRES_PASSWORD before first docker compose up." >&2
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE and no example available." >&2
  exit 1
fi

PASSWORD_LINE=$(grep -E '^MAILBOX_MEMORY_POSTGRES_PASSWORD=' "$ENV_FILE" || true)
PASSWORD_VALUE=${PASSWORD_LINE#MAILBOX_MEMORY_POSTGRES_PASSWORD=}
if [[ -z "$PASSWORD_VALUE" || "$PASSWORD_VALUE" = "CHANGE_ME_LOCAL_ONLY" ]]; then
  echo "MAILBOX_MEMORY_POSTGRES_PASSWORD must be set to a non-placeholder value in $ENV_FILE before starting Docker." >&2
  exit 1
fi

docker compose --env-file "$ENV_FILE" -f "$COMPOSE" up -d
docker compose --env-file "$ENV_FILE" -f "$COMPOSE" ps

echo ""
echo "Next: set MAILBOX_MEMORY_DATABASE_URL in tools/gmail_audit/.env (same user/password/port as .env.mailbox-memory)."
