#!/usr/bin/env bash
# Step 1: seed pending smoke event, process ONLY via systemd oneshot (not manual gmail_intake).
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
ENV_VPS="${GMAIL_ROOT}/.env.vps"
DB_USER="${MAILBOX_MEMORY_POSTGRES_USER:-mailbox_memory}"
DB_NAME="${MAILBOX_MEMORY_POSTGRES_DB:-mailbox_memory}"
EVENT_ID="${P1_SMOKE_EVENT_ID:-osevt_p1_smoke_v1}"
LOG_FILE="/var/log/topinstal/event-processor.log"
MARKER="TIMER_PATH_PROVE_$(date -u +%Y%m%dT%H%M%SZ)"

COMPOSE=(docker compose --env-file "${ENV_VPS}" -f "${GMAIL_ROOT}/docker-compose.vps.yml")

echo "==> seed ${EVENT_ID} -> pending"
bash "${GMAIL_ROOT}/deploy/vps-seed-test-event.sh"

echo "==> log marker: ${MARKER}"
echo "=== ${MARKER} ===" >>"${LOG_FILE}"

echo "==> systemd oneshot (timer unit, not manual intake)"
systemctl start topinstal-event-processor.service

echo "==> wait for service"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if ! systemctl is-active --quiet topinstal-event-processor.service; then
    break
  fi
  sleep 1
done

echo "==> log slice after marker"
log_slice="$(awk "/${MARKER}/{flag=1;next}flag" "${LOG_FILE}" | tail -n 20)"
echo "${log_slice}"

if ! echo "${log_slice}" | grep -qE "'claimed': 1|\"claimed\": 1"; then
  echo "TIMER_PATH_PROVE_FAILED: no claimed: 1 after marker" >&2
  exit 1
fi
if echo "${log_slice}" | grep -qE "'errors': \['|\"errors\": \[\""; then
  echo "TIMER_PATH_PROVE_FAILED: errors in log slice" >&2
  exit 1
fi

db_status="$("${COMPOSE[@]}" exec -T mailbox-memory-db \
  psql -U "${DB_USER}" -d "${DB_NAME}" -tAc \
  "SELECT processing_status FROM unified_os_events WHERE event_id='${EVENT_ID}'")"
db_status="$(echo "${db_status}" | tr -d '[:space:]')"
echo "==> DB ${EVENT_ID}: ${db_status}"

if [[ "${db_status}" != "processed" ]]; then
  echo "TIMER_PATH_PROVE_FAILED: expected processed" >&2
  exit 1
fi

echo "P1_TIMER_PATH_PROVE_OK marker=${MARKER}"
