#!/usr/bin/env bash
# Bounded smoke test for event-spine-processor (shadow mode).
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
GMAIL_ENV="${GMAIL_ENV_FILE:-/etc/topinstal/gmail-agent.env}"
ENV_VPS="${GMAIL_ROOT}/.env.vps"
MAX_ITER="${1:-5}"

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

echo "==> schema verify"
bash "${GMAIL_ROOT}/deploy/vps-verify-event-spine-schema.sh"

echo "==> enable shadow processor flags (gmail-agent.env)"
_set_env_kv "${GMAIL_ENV}" EVENT_SPINE_PROCESSOR_ENABLED 1
_set_env_kv "${GMAIL_ENV}" EVENT_SPINE_PROCESSOR_MODE shadow

echo "==> event-spine-processor smoke (max-iterations=${MAX_ITER})"
docker compose --env-file "${ENV_VPS}" -f "${GMAIL_ROOT}/docker-compose.vps.yml" \
  exec -T -e EVENT_SPINE_PROCESSOR_ENABLED=1 -e EVENT_SPINE_PROCESSOR_MODE=shadow \
  gmail-agent-worker \
  python tools/gmail_audit/gmail_intake.py event-spine-processor --max-iterations "${MAX_ITER}" --verbose

echo "EVENT_SPINE_PROCESSOR_SMOKE_OK"
