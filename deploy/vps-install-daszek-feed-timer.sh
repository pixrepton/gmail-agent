#!/usr/bin/env bash
# Install systemd timer for operational feed push (Luka #4).
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
UNIT_SRC="${GMAIL_ROOT}/deploy/systemd"
LOG_DIR="/var/log/topinstal"
LOG_FILE="${LOG_DIR}/daszek-feed-push.log"

if [[ ! -f "${UNIT_SRC}/topinstal-daszek-feed-push.service" ]]; then
  echo "Missing ${UNIT_SRC}/topinstal-daszek-feed-push.service" >&2
  exit 1
fi

echo "==> CRLF fix on deploy scripts"
find "${GMAIL_ROOT}/deploy" -name "*.sh" -exec sed -i 's/\r$//' {} + 2>/dev/null || true

echo "==> log directory"
install -d -m 755 "${LOG_DIR}"
touch "${LOG_FILE}"
chmod 644 "${LOG_FILE}" 2>/dev/null || true

echo "==> install systemd units"
install -m 644 "${UNIT_SRC}/topinstal-daszek-feed-push.service" /etc/systemd/system/
install -m 644 "${UNIT_SRC}/topinstal-daszek-feed-push.timer" /etc/systemd/system/

systemctl daemon-reload
systemctl enable topinstal-daszek-feed-push.timer
systemctl start topinstal-daszek-feed-push.timer

echo "==> timer status"
systemctl status topinstal-daszek-feed-push.timer --no-pager || true
systemctl list-timers topinstal-daszek-feed-push.timer --no-pager || true

echo "==> optional: trigger one push now (bounded)"
if [[ "${TRIGGER_PUSH_NOW:-1}" == "1" ]]; then
  systemctl start topinstal-daszek-feed-push.service || true
  sleep 3
  tail -n 25 "${LOG_FILE}" || true
fi

echo "DASZEK_FEED_TIMER_INSTALL_OK"
