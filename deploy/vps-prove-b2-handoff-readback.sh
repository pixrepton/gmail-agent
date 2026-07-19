#!/usr/bin/env bash
# Bounded B2 readback audit: Gate B handoff case_062a7aa4ed7b + optional test card.
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
ENV_VPS="${ENV_VPS_FILE:-${GMAIL_ROOT}/.env.vps}"
COMPOSE=(docker compose --env-file "${ENV_VPS}" -f "${GMAIL_ROOT}/docker-compose.vps.yml")
FEED_JSON="${FEED_JSON:-}"
RUN_ID="b2-handoff-audit-$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${GMAIL_ROOT}/runs/${RUN_ID}"

if [[ -z "${FEED_JSON}" ]]; then
  LATEST="$(ls -td "${GMAIL_ROOT}"/runs/daszek-feed-*/operational_feed_snapshot.json 2>/dev/null | head -1 || true)"
  FEED_JSON="${LATEST:-}"
fi
if [[ -z "${FEED_JSON}" || ! -f "${FEED_JSON}" ]]; then
  echo "ERROR: set FEED_JSON or run push_daszek_operational_feed_prod.sh first" >&2
  exit 2
fi

mkdir -p "${OUT_DIR}"

echo "==> B2 handoff readback audit"
echo "feed=${FEED_JSON}"

"${COMPOSE[@]}" --profile worker run --rm -T \
  -e PYTHONPATH=/app/tools/gmail_audit \
  -v "${GMAIL_ROOT}/tools/gmail_audit/audit_b2_feed_readback.py:/app/tools/gmail_audit/audit_b2_feed_readback.py:ro" \
  -v "${GMAIL_ROOT}/deploy/fixtures/b2_handoff_audit_cases.json:/app/b2_handoff_audit_cases.json:ro" \
  -v "${FEED_JSON}:/tmp/feed.json:ro" \
  -v "${OUT_DIR}:/out" \
  gmail-agent-worker \
  python tools/gmail_audit/audit_b2_feed_readback.py \
    --feed-json /tmp/feed.json \
    --handoff-bounded \
    --require-gateb \
    --out /out/report.json \
  | tee "${OUT_DIR}/audit.log"

echo "B2_HANDOFF_READBACK_PROVE_OK ${OUT_DIR}"
