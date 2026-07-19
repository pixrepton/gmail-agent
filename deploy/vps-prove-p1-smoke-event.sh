#!/usr/bin/env bash
# Seed osevt_p1_smoke_v1, run processor once, assert claimed>=1 and processed status.
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
GMAIL_ENV="${GMAIL_ENV_FILE:-/etc/topinstal/gmail-agent.env}"
ENV_VPS="${GMAIL_ROOT}/.env.vps"
DB_USER="${MAILBOX_MEMORY_POSTGRES_USER:-mailbox_memory}"
DB_NAME="${MAILBOX_MEMORY_POSTGRES_DB:-mailbox_memory}"
EVENT_ID="${P1_SMOKE_EVENT_ID:-osevt_p1_smoke_v1}"

COMPOSE=(docker compose --env-file "${ENV_VPS}" -f "${GMAIL_ROOT}/docker-compose.vps.yml")

_set_env_kv() {
  local file="$1" key="$2" value="$3"
  touch "$file"
  chmod 600 "$file" 2>/dev/null || true
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    local tmp
    tmp="$(mktemp)"
    awk -v k="$key" -v v="$value" '
      BEGIN { done=0 }
      $0 ~ "^" k "=" { print k "=" v; done=1; next }
      { print }
      END { if (!done) print k "=" v }
    ' "$file" >"$tmp"
    mv "$tmp" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >>"$file"
  fi
}

echo "==> shadow-only env flags"
_set_env_kv "${GMAIL_ENV}" EVENT_SPINE_PROCESSOR_ENABLED 1
_set_env_kv "${GMAIL_ENV}" EVENT_SPINE_PROCESSOR_MODE shadow

echo "==> seed"
bash "${GMAIL_ROOT}/deploy/vps-seed-test-event.sh"

echo "==> processor (max-iterations=1)"
out="$("${COMPOSE[@]}" exec -T \
  -e EVENT_SPINE_PROCESSOR_ENABLED=1 \
  -e EVENT_SPINE_PROCESSOR_MODE=shadow \
  gmail-agent-worker \
  python tools/gmail_audit/gmail_intake.py event-spine-processor --max-iterations 1 --verbose 2>&1)" || {
  echo "${out}"
  exit 1
}
echo "${out}"

if ! echo "${out}" | grep -q "'claimed': 1"; then
  if ! echo "${out}" | grep -q '"claimed": 1'; then
    echo "PROVE_FAILED: expected claimed: 1 in processor output" >&2
    exit 1
  fi
fi
if echo "${out}" | grep -q "'errors': \['"; then
  echo "PROVE_FAILED: processor reported errors" >&2
  exit 1
fi
if echo "${out}" | grep -qE "'failed': [1-9]"; then
  echo "PROVE_FAILED: processor reported failed > 0" >&2
  exit 1
fi

db_status="$("${COMPOSE[@]}" exec -T mailbox-memory-db \
  psql -U "${DB_USER}" -d "${DB_NAME}" -tAc \
  "SELECT processing_status FROM unified_os_events WHERE event_id='${EVENT_ID}'")"
db_status="$(echo "${db_status}" | tr -d '[:space:]')"
echo "==> DB processing_status for ${EVENT_ID}: ${db_status}"

if [[ "${db_status}" != "processed" ]]; then
  echo "PROVE_FAILED: expected processed, got ${db_status}" >&2
  "${COMPOSE[@]}" exec -T mailbox-memory-db psql -U "${DB_USER}" -d "${DB_NAME}" -c \
    "SELECT event_id, processing_status, last_error, attempt_count FROM unified_os_events WHERE event_id='${EVENT_ID}'" >&2 || true
  exit 1
fi

echo "P1_SMOKE_EVENT_PROVE_OK event_id=${EVENT_ID} claimed=1 status=processed"
