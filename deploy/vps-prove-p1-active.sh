#!/usr/bin/env bash
# P1.4 proof: active processor writes idempotent event_spine_handler_effects rows.
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
ENV_VPS="${GMAIL_ROOT}/.env.vps"
DB_USER="${MAILBOX_MEMORY_POSTGRES_USER:-mailbox_memory}"
DB_NAME="${MAILBOX_MEMORY_POSTGRES_DB:-mailbox_memory}"
EVENT_ID="${P14_ACTIVE_EVENT_ID:-osevt_p14_active_smoke_v1}"
ENGAGEMENT_ID="${P14_ENGAGEMENT_ID:-6048589a-e64c-474a-8b41-56cf4224502c}"
MARKER="P14_ACTIVE_PROVE_$(date -u +%Y%m%dT%H%M%SZ)"

COMPOSE=(docker compose --env-file "${ENV_VPS}" -f "${GMAIL_ROOT}/docker-compose.vps.yml")

_psql() {
  "${COMPOSE[@]}" exec -T mailbox-memory-db psql -U "${DB_USER}" -d "${DB_NAME}" -v ON_ERROR_STOP=1 "$@"
}

echo "==> verify event spine schema"
bash "${GMAIL_ROOT}/deploy/vps-verify-event-spine-schema.sh"

echo "==> seed ${EVENT_ID} (pending) + clear prior handler effect"
_psql <<SQL
INSERT INTO unified_os_events (
    event_id, event_type, engagement_id, source_repo, occurred_at,
    payload, correlation, processing_status, attempt_count, failure_detail
) VALUES (
    '${EVENT_ID}',
    'correlation_links_registered',
    '${ENGAGEMENT_ID}',
    'gmail-agent',
    NOW(),
    '{"marker":"${MARKER}","links_count":1}'::jsonb,
    '{"marker":"${MARKER}"}'::jsonb,
    'pending',
    0,
    '{}'::jsonb
)
ON CONFLICT (event_id) DO UPDATE SET
    payload = EXCLUDED.payload,
    correlation = EXCLUDED.correlation,
    processing_status = 'pending',
    processed_at = NULL,
    processor_id = NULL,
    last_error = NULL,
    failure_detail = '{}'::jsonb,
    attempt_count = 0;
DELETE FROM event_spine_handler_effects WHERE event_id = '${EVENT_ID}';
SQL

echo "==> process once (active)"
"${COMPOSE[@]}" exec -T \
  -e EVENT_SPINE_PROCESSOR_ENABLED=1 \
  -e EVENT_SPINE_PROCESSOR_MODE=active \
  gmail-agent-worker \
  python tools/gmail_audit/gmail_intake.py event-spine-processor --max-iterations 1 --verbose

st="$(_psql -tAc "SELECT processing_status FROM unified_os_events WHERE event_id='${EVENT_ID}'")"
st="$(echo "${st}" | tr -d '[:space:]')"
fx="$(_psql -tAc "SELECT handler_key FROM event_spine_handler_effects WHERE event_id='${EVENT_ID}'")"
fx="$(echo "${fx}" | tr -d '[:space:]')"

echo "==> event status=${st} handler_effect=${fx}"
if [[ "${st}" != "processed" || "${fx}" != "correlation_links_registered" ]]; then
  echo "P14_ACTIVE_PROVE_FAILED marker=${MARKER}" >&2
  exit 1
fi

echo "P14_ACTIVE_PROVE_OK marker=${MARKER} event_id=${EVENT_ID}"
