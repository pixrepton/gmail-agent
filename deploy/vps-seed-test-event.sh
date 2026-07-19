#!/usr/bin/env bash
# Idempotent P1.2b smoke event for unified_os_events (shadow handler proof).
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
ENV_VPS="${GMAIL_ROOT}/.env.vps"
DB_USER="${MAILBOX_MEMORY_POSTGRES_USER:-mailbox_memory}"
DB_NAME="${MAILBOX_MEMORY_POSTGRES_DB:-mailbox_memory}"

EVENT_ID="${P1_SMOKE_EVENT_ID:-osevt_p1_smoke_v1}"
ENGAGEMENT_ID="${P1_SMOKE_ENGAGEMENT_ID:-6048589a-e64c-474a-8b41-56cf4224502c}"
EVENT_TYPE="${P1_SMOKE_EVENT_TYPE:-correlation_links_registered}"

COMPOSE=(docker compose --env-file "${ENV_VPS}" -f "${GMAIL_ROOT}/docker-compose.vps.yml")

_psql() {
  "${COMPOSE[@]}" exec -T mailbox-memory-db psql -U "${DB_USER}" -d "${DB_NAME}" -v ON_ERROR_STOP=1 "$@"
}

echo "==> seed smoke event event_id=${EVENT_ID} engagement_id=${ENGAGEMENT_ID}"

_psql <<SQL
INSERT INTO unified_os_events (
    event_id,
    event_type,
    engagement_id,
    source_repo,
    occurred_at,
    payload,
    correlation,
    processing_status,
    attempt_count,
    failure_detail
) VALUES (
    '${EVENT_ID}',
    '${EVENT_TYPE}',
    '${ENGAGEMENT_ID}',
    'gmail-agent',
    NOW(),
    '{"proof":"p1_smoke_v1","links_count":0,"case_id":"case_c02cfc10b5b9","note":"idempotent shadow handler proof"}'::jsonb,
    '{"proof":"p1_smoke_v1","case_id":"case_c02cfc10b5b9"}'::jsonb,
    'pending',
    0,
    '{}'::jsonb
)
ON CONFLICT (event_id) DO UPDATE SET
    event_type = EXCLUDED.event_type,
    engagement_id = EXCLUDED.engagement_id,
    payload = EXCLUDED.payload,
    correlation = EXCLUDED.correlation,
    processing_status = 'pending',
    processed_at = NULL,
    processor_id = NULL,
    last_error = NULL,
    failure_detail = '{}'::jsonb,
    attempt_count = 0;
SQL

status="$(_psql -tAc "SELECT processing_status FROM unified_os_events WHERE event_id='${EVENT_ID}'")"
status="$(echo "${status}" | tr -d '[:space:]')"
echo "==> row status after seed: ${status}"
if [[ "${status}" != "pending" ]]; then
  echo "SEED_FAILED: expected pending, got ${status}" >&2
  exit 1
fi

echo "P1_SMOKE_EVENT_SEED_OK event_id=${EVENT_ID}"
