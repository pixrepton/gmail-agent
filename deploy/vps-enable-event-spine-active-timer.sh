#!/usr/bin/env bash
# After vps-prove-p1-active.sh: run event processor timer in active mode (bounded handlers only).
set -euo pipefail

GMAIL_ENV="${GMAIL_ENV_FILE:-/etc/topinstal/gmail-agent.env}"
UNIT=/etc/systemd/system/topinstal-event-processor.service

_set_env_kv() {
  local file="$1" key="$2" val="$3"
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$file"
  else
    echo "${key}=${val}" >>"$file"
  fi
}

_set_env_kv "${GMAIL_ENV}" EVENT_SPINE_PROCESSOR_ENABLED 1
_set_env_kv "${GMAIL_ENV}" EVENT_SPINE_PROCESSOR_MODE active

if [[ -f "${UNIT}" ]]; then
  sed -i 's/EVENT_SPINE_PROCESSOR_MODE=shadow/EVENT_SPINE_PROCESSOR_MODE=active/g' "${UNIT}"
  sed -i 's/-e EVENT_SPINE_PROCESSOR_MODE=shadow/-e EVENT_SPINE_PROCESSOR_MODE=active/g' "${UNIT}" || true
  systemctl daemon-reload
fi

echo "OK: event spine timer set to active (handlers: audit table only; no Gmail/CRM outbound)"
