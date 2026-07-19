#!/usr/bin/env bash
# Step 2: emit via publish_os_event, process via systemd oneshot, assert non-smoke event_id.
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
ENV_VPS="${GMAIL_ROOT}/.env.vps"
DB_USER="${MAILBOX_MEMORY_POSTGRES_USER:-mailbox_memory}"
DB_NAME="${MAILBOX_MEMORY_POSTGRES_DB:-mailbox_memory}"
SMOKE_ID="${P1_SMOKE_EVENT_ID:-osevt_p1_smoke_v1}"
LOG_FILE="/var/log/topinstal/event-processor.log"
MARKER="LIVE_EMIT_TIMER_PROVE_$(date -u +%Y%m%dT%H%M%SZ)"

COMPOSE=(docker compose --env-file "${ENV_VPS}" -f "${GMAIL_ROOT}/docker-compose.vps.yml")

echo "==> publish_os_event (live emitter)"
live_id="$("${COMPOSE[@]}" exec -T \
  -e PYTHONPATH=/app/tools/gmail_audit \
  -w /app/tools/gmail_audit \
  gmail-agent-worker \
  python scripts/vps_emit_live_os_event.py 2>&1)" || {
  echo "${live_id}" >&2
  exit 1
}
live_id="$(echo "${live_id}" | tail -n 1 | tr -d '[:space:]')"

if [[ -z "${live_id}" ]] || [[ "${live_id}" == osevt_p1_smoke* ]]; then
  echo "LIVE_EMIT_PROVE_FAILED: bad event_id=${live_id}" >&2
  exit 1
fi
echo "==> live event_id=${live_id}"

db_pending="$("${COMPOSE[@]}" exec -T mailbox-memory-db \
  psql -U "${DB_USER}" -d "${DB_NAME}" -tAc \
  "SELECT processing_status FROM unified_os_events WHERE event_id='${live_id}'")"
db_pending="$(echo "${db_pending}" | tr -d '[:space:]')"
echo "==> DB before timer: ${db_pending}"
if [[ "${db_pending}" != "pending" ]]; then
  echo "LIVE_EMIT_PROVE_FAILED: expected pending before timer, got ${db_pending}" >&2
  exit 1
fi

echo "=== ${MARKER} event_id=${live_id} ===" >>"${LOG_FILE}"
echo "==> systemd oneshot"
systemctl start topinstal-event-processor.service

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if ! systemctl is-active --quiet topinstal-event-processor.service; then
    break
  fi
  sleep 1
done

log_slice="$(awk "/${MARKER}/{flag=1;next}flag" "${LOG_FILE}" | tail -n 20)"
echo "${log_slice}"

if ! echo "${log_slice}" | grep -qE "'claimed': [1-9]|\"claimed\": [1-9]"; then
  echo "LIVE_EMIT_PROVE_FAILED: no claimed>=1 in log" >&2
  exit 1
fi

db_done="$("${COMPOSE[@]}" exec -T mailbox-memory-db \
  psql -U "${DB_USER}" -d "${DB_NAME}" -tAc \
  "SELECT processing_status FROM unified_os_events WHERE event_id='${live_id}'")"
db_done="$(echo "${db_done}" | tr -d '[:space:]')"
echo "==> DB after timer: ${db_done}"

if [[ "${db_done}" != "processed" ]]; then
  echo "LIVE_EMIT_PROVE_FAILED: expected processed" >&2
  "${COMPOSE[@]}" exec -T mailbox-memory-db psql -U "${DB_USER}" -d "${DB_NAME}" -c \
    "SELECT event_id, processing_status, last_error FROM unified_os_events WHERE event_id='${live_id}'" >&2
  exit 1
fi

echo "P1_LIVE_EMIT_TIMER_PROVE_OK event_id=${live_id} marker=${MARKER}"
