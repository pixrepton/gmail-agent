#!/usr/bin/env bash
# 24h soak watch: baseline | sample | finalize (append JSONL samples).
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
ENV_VPS="${ENV_VPS_FILE:-${GMAIL_ROOT}/.env.vps}"
COMPOSE=(docker compose --env-file "${ENV_VPS}" -f "${GMAIL_ROOT}/docker-compose.vps.yml")
PHASE="${1:-sample}"
SOAK_DIR="${2:-}"

if [[ -z "${SOAK_DIR}" ]]; then
  echo "usage: $0 baseline|sample|finalize SOAK_DIR" >&2
  exit 2
fi

mkdir -p "${SOAK_DIR}/watch"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SAMPLES="${SOAK_DIR}/watch/samples.jsonl"

WORKER_PS="$("${COMPOSE[@]}" ps gmail-agent-worker 2>/dev/null | tail -n +2 || true)"
PENDING="$("${COMPOSE[@]}" --profile worker run --rm -T gmail-agent-worker \
  python tools/gmail_audit/gmail_intake.py daszek-bridge-drain \
  --remote --domain adjudication --dry-run --max-items 50 2>/dev/null \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('items') or []))" 2>/dev/null || echo "-1")"
FEED_TAIL=""
if [[ -f /var/log/topinstal/daszek-feed-push.log ]]; then
  FEED_TAIL="$(tail -n 3 /var/log/topinstal/daszek-feed-push.log 2>/dev/null | tr '\n' ' ' || true)"
fi

export TS PHASE WORKER_PS PENDING FEED_TAIL
LINE="$(python3 -c "
import json, os
print(json.dumps({
    'ts': os.environ['TS'],
    'phase': os.environ['PHASE'],
    'worker_ps': os.environ.get('WORKER_PS', '')[:2000],
    'pending_adjudication': os.environ.get('PENDING', ''),
    'feed_log_tail': os.environ.get('FEED_TAIL', '')[:500],
}, ensure_ascii=False))
")"

echo "${LINE}" >> "${SAMPLES}"

if [[ "${PHASE}" == "baseline" ]]; then
  mkdir -p "${SOAK_DIR}/baseline"
  echo "${LINE}" > "${SOAK_DIR}/baseline/snapshot.json"
  (cd "${GMAIL_ROOT}" && git rev-parse HEAD 2>/dev/null || true) > "${SOAK_DIR}/baseline/git-head.txt" || true
  grep -E '^(SIGNAL_RUNTIME_MODE|EVENT_SPINE_PROCESSOR_MODE)=' /etc/topinstal/gmail-agent.env 2>/dev/null \
    > "${SOAK_DIR}/baseline/env-flags.txt" || true
  echo "SOAK_BASELINE_OK ${SOAK_DIR}"
elif [[ "${PHASE}" == "finalize" ]]; then
  COUNT="$(wc -l < "${SAMPLES}" | tr -d ' ')"
  echo "{\"finished_at\":\"${TS}\",\"sample_count\":${COUNT}}" > "${SOAK_DIR}/finalize.json"
  echo "SOAK_FINALIZE_OK samples=${COUNT}"
else
  echo "SOAK_SAMPLE_OK pending=${PENDING}"
fi
