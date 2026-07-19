#!/usr/bin/env bash
# Install shadow event-spine systemd timer on VPS (after P1 smoke prove).
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
GMAIL_ENV="${GMAIL_ENV_FILE:-/etc/topinstal/gmail-agent.env}"
UNIT_SRC="${GMAIL_ROOT}/deploy/systemd"
LOG_DIR="/var/log/topinstal"
LOG_FILE="${LOG_DIR}/event-processor.log"

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

if [[ ! -f "${UNIT_SRC}/topinstal-event-processor.service" ]]; then
  echo "Missing ${UNIT_SRC}/topinstal-event-processor.service" >&2
  exit 1
fi

echo "==> shadow-only processor flags in ${GMAIL_ENV}"
_set_env_kv "${GMAIL_ENV}" EVENT_SPINE_PROCESSOR_ENABLED 1
_set_env_kv "${GMAIL_ENV}" EVENT_SPINE_PROCESSOR_MODE shadow

echo "==> log directory"
install -d -m 755 "${LOG_DIR}"
touch "${LOG_FILE}"
chmod 644 "${LOG_FILE}" 2>/dev/null || true
chown root:root "${LOG_DIR}" "${LOG_FILE}" 2>/dev/null || true

echo "==> install systemd units"
install -m 644 "${UNIT_SRC}/topinstal-event-processor.service" /etc/systemd/system/
install -m 644 "${UNIT_SRC}/topinstal-event-processor.timer" /etc/systemd/system/

systemctl daemon-reload
systemctl enable topinstal-event-processor.timer
systemctl start topinstal-event-processor.timer

echo "==> timer status"
systemctl status topinstal-event-processor.timer --no-pager || true
systemctl list-timers topinstal-event-processor.timer --no-pager || true

echo "==> trigger one batch now (optional proof)"
systemctl start topinstal-event-processor.service || true
sleep 2
if [[ -f "${LOG_FILE}" ]]; then
  echo "==> tail ${LOG_FILE}"
  tail -n 30 "${LOG_FILE}" || true
fi

echo "EVENT_SPINE_TIMER_INSTALL_OK"
