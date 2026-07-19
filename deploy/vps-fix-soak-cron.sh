#!/usr/bin/env bash
# Repair 24h soak cron (Permission denied after sync stripped +x). Re-install with bash wrapper.
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
SOAK_DIR="${SOAK_DIR:-/opt/gmail-agent/current/runs/24h-soak-20260525}"
CRON_FILE="/etc/cron.d/topinstal-24h-soak"
LOG_FILE="/var/log/topinstal/24h-soak-watch.log"

find "${GMAIL_ROOT}/deploy" -name "*.sh" -exec sed -i 's/\r$//' {} + 2>/dev/null || true
chmod +x "${GMAIL_ROOT}/deploy/"*.sh 2>/dev/null || true

cat > "${CRON_FILE}" <<EOF
# TOP-INSTAL gmail-agent 24h soak watch (invoke via bash — survives sync without +x)
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
*/15 * * * * root bash ${GMAIL_ROOT}/deploy/vps-24h-soak-watch.sh sample ${SOAK_DIR} >> ${LOG_FILE} 2>&1
EOF
chmod 644 "${CRON_FILE}"

echo "==> manual sample (verify)"
bash "${GMAIL_ROOT}/deploy/vps-24h-soak-watch.sh" sample "${SOAK_DIR}"

COUNT="$(wc -l < "${SOAK_DIR}/watch/samples.jsonl" | tr -d ' ')"
echo "SOAK_CRON_FIXED samples=${COUNT} soak_dir=${SOAK_DIR}"
