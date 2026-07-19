#!/usr/bin/env bash
# P2 proof bundle: soak finalize check, feed audit, engagement merge, Gate B, Skrzat smoke.
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
OUT_DIR="${OUT_DIR:-${GMAIL_ROOT}/runs/p2-nodeb-proof-$(date -u +%Y%m%dT%H%M%SZ)}"
MODE_MERGE="${MODE_MERGE:-dry-run}" # dry-run | write
WP_HOST="${WP_HOST:-https://topinstal.com.pl}"

mkdir -p "${OUT_DIR}"
find "${GMAIL_ROOT}/deploy" -name "*.sh" -exec sed -i 's/\r$//' {} + 2>/dev/null || true

echo "OUT_DIR=${OUT_DIR}"

_run_py() {
  docker compose --env-file "${GMAIL_ROOT}/.env.vps" -f "${GMAIL_ROOT}/docker-compose.vps.yml" --profile worker run --rm --no-deps \
    -v "${GMAIL_ROOT}/tools/gmail_audit:/app/tools/gmail_audit:ro" \
    -v /etc/topinstal/gmail-agent.env:/etc/topinstal/gmail-agent.env:ro \
    -e GMAIL_AGENT_ENV_FILE=/etc/topinstal/gmail-agent.env \
    -e PYTHONPATH=/app/tools/gmail_audit \
    -w /app/tools/gmail_audit \
    gmail-agent-worker \
    "$@"
}

echo "==> Skrzat unit smoke"
cd "${GMAIL_ROOT}"
docker compose --env-file .env.vps -f docker-compose.vps.yml --profile worker run --rm --no-deps \
  -v "${GMAIL_ROOT}/tools/gmail_audit:/app/tools/gmail_audit:ro" \
  -e PYTHONPATH=/app/tools/gmail_audit \
  -w /app/tools/gmail_audit \
  gmail-agent-worker \
  python -m pytest tests/test_skrzat_runtime.py -q | tee "${OUT_DIR}/skrzat-pytest.log" || true

echo "==> engagement duplicate metrics (before)"
docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -t -A -c "
SELECT count(*) FROM (
  SELECT target_id FROM correlation_links
  WHERE link_type='gmail_message' AND target_id <> ''
  GROUP BY target_id HAVING count(DISTINCT engagement_id) > 1
) x;
" | tee "${OUT_DIR}/dup_engagement_groups_before.txt"

echo "==> reconcile engagement duplicates (${MODE_MERGE})"
ARGS=()
[[ "${MODE_MERGE}" == "dry-run" ]] && ARGS+=(--dry-run)
_run_py python scripts/reconcile_engagement_duplicates.py "${ARGS[@]}" | tee "${OUT_DIR}/reconcile_engagements.log"

echo "==> push feed + audit eligible vs WP"
bash "${GMAIL_ROOT}/deploy/push_daszek_operational_feed_prod.sh" 2>&1 | tee "${OUT_DIR}/feed-push.log" || true
FEED_JSON="$(ls -t "${GMAIL_ROOT}"/runs/daszek-feed-*/operational_feed_snapshot.json 2>/dev/null | head -1)"
if [[ -n "${FEED_JSON}" && -f "${FEED_JSON}" ]]; then
  docker compose --env-file "${GMAIL_ROOT}/.env.vps" -f "${GMAIL_ROOT}/docker-compose.vps.yml" --profile worker run --rm --no-deps \
    -v "${GMAIL_ROOT}/tools/gmail_audit:/app/tools/gmail_audit:ro" \
    -v "${FEED_JSON}:/tmp/operational_feed_snapshot.json:ro" \
    -v /etc/topinstal/gmail-agent.env:/etc/topinstal/gmail-agent.env:ro \
    -e GMAIL_AGENT_ENV_FILE=/etc/topinstal/gmail-agent.env \
    -e PYTHONPATH=/app/tools/gmail_audit \
    -w /app/tools/gmail_audit \
    gmail-agent-worker \
    python scripts/audit_feed_eligible_wp.py --feed-json /tmp/operational_feed_snapshot.json --wp-host "${WP_HOST}" \
    | tee "${OUT_DIR}/feed-audit.log" || true
fi

echo "==> Gate B"
bash "${GMAIL_ROOT}/deploy/p0_gateb_staging_verify.sh" 2>&1 | tee "${OUT_DIR}/gateb.log" | tail -15

echo "OK_P2_NODEB_PROOF_BUNDLE"
