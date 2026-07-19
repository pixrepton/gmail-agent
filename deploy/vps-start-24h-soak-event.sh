#!/usr/bin/env bash
# Start 24h soak event: baseline + event.json + cron watch (does not change SIGNAL_RUNTIME_MODE).
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
SOAK_DATE="${SOAK_DATE:-$(date -u +%Y%m%d)}"
SOAK_DIR="${SOAK_DIR:-${GMAIL_ROOT}/runs/24h-soak-${SOAK_DATE}}"
CRON_FILE="/etc/cron.d/topinstal-24h-soak"
LOG_FILE="/var/log/topinstal/24h-soak-watch.log"

find "${GMAIL_ROOT}/deploy" -name "*.sh" -exec sed -i 's/\r$//' {} + 2>/dev/null || true

mkdir -p "${SOAK_DIR}/baseline" "${SOAK_DIR}/watch"
touch "${LOG_FILE}"
chmod 644 "${LOG_FILE}" 2>/dev/null || true

echo "==> baseline"
bash "${GMAIL_ROOT}/deploy/vps-24h-soak-watch.sh" baseline "${SOAK_DIR}"

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
FINISH_AT="$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
GIT_HEAD="$(cd "${GMAIL_ROOT}" && git rev-parse HEAD 2>/dev/null || echo unknown)"
ENV_SNAP="${SOAK_DIR}/baseline/env-flags.txt"

python3 - "${SOAK_DIR}/event.json" "${STARTED_AT}" "${FINISH_AT}" "${SOAK_DIR}" "${GIT_HEAD}" "${ENV_SNAP}" <<'PY'
import json
import sys
from pathlib import Path

out, started, finish, soak_dir, git_head, env_snap = sys.argv[1:7]
env_lines = []
p = Path(env_snap)
if p.is_file():
    env_lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]

event = {
    "event": "gmail-agent-24h-soak",
    "status": "running",
    "started_at": started,
    "finalize_after": finish,
    "soak_dir": soak_dir,
    "git_head": git_head,
    "mode_observed": "unchanged — monitor only",
    "env_flags": env_lines,
    "finalize_command": f"bash /opt/gmail-agent/current/deploy/vps-24h-soak-watch.sh finalize {soak_dir}",
}
Path(out).write_text(json.dumps(event, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("event.json written", out)
PY

echo "==> install cron watch (every 15 min)"
cat > "${CRON_FILE}" <<EOF
# TOP-INSTAL gmail-agent 24h soak watch
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
*/15 * * * * root bash ${GMAIL_ROOT}/deploy/vps-24h-soak-watch.sh sample ${SOAK_DIR} >> ${LOG_FILE} 2>&1
EOF
chmod 644 "${CRON_FILE}"

echo "==> first sample"
bash "${GMAIL_ROOT}/deploy/vps-24h-soak-watch.sh" sample "${SOAK_DIR}"

echo "GMAIL_AGENT_24H_SOAK_STARTED ${SOAK_DIR}"
echo "finalize_after=${FINISH_AT}"
echo "log=${LOG_FILE}"
