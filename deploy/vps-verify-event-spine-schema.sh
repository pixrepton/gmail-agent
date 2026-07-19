#!/usr/bin/env bash
# Verify P1.2b processing_* columns on unified_os_events (idempotent bootstrap).
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
ENV_VPS="${GMAIL_ROOT}/.env.vps"
COMPOSE=(docker compose --env-file "${ENV_VPS}" -f "${GMAIL_ROOT}/docker-compose.vps.yml")
DB_USER="${MAILBOX_MEMORY_POSTGRES_USER:-mailbox_memory}"
DB_NAME="${MAILBOX_MEMORY_POSTGRES_DB:-mailbox_memory}"

REQUIRED_COLS=(
  processing_status
  processed_at
  processor_id
  attempt_count
  last_error
  failure_detail
)

echo "==> unified_os_events columns (before bootstrap)"
"${COMPOSE[@]}" exec -T mailbox-memory-db \
  psql -U "${DB_USER}" -d "${DB_NAME}" -c "\d unified_os_events" || true

echo "==> correlation_registry bootstrap (CREATE + ALTER IF NOT EXISTS)"
docker exec -e PYTHONPATH=/app/tools/gmail_audit -w /app/tools/gmail_audit gmail-agent-nodeb-api \
  python scripts/vps_p0_bootstrap_once.py

echo "==> column presence check"
missing=0
for col in "${REQUIRED_COLS[@]}"; do
  if ! "${COMPOSE[@]}" exec -T mailbox-memory-db \
    psql -U "${DB_USER}" -d "${DB_NAME}" -tAc \
    "SELECT 1 FROM information_schema.columns WHERE table_name='unified_os_events' AND column_name='${col}'" \
    | grep -q 1; then
    echo "MISSING: ${col}"
    missing=1
  else
    echo "OK: ${col}"
  fi
done

if [[ "${missing}" -ne 0 ]]; then
  echo "SCHEMA_VERIFY_FAILED: processing_* columns missing after bootstrap" >&2
  exit 1
fi

echo "SCHEMA_VERIFY_OK"
