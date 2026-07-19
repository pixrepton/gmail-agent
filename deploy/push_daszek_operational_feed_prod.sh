#!/usr/bin/env bash
# Build operational feed from mailbox memory on Node B and POST to production Daszek (Node A).
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
WP_HOST="${WP_HOST:-https://topinstal.com.pl}"
CASE_LIMIT="${CASE_LIMIT:-50}"
TASK_LIMIT="${TASK_LIMIT:-80}"
# B1 feedback bridge: path to Daszek v2 desk_notes.json on the VPS host (optional).
DESK_NOTES_JSON="${DESK_NOTES_JSON:-}"
RUN_ID="daszek-feed-$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${GMAIL_ROOT}/runs/${RUN_ID}"
FEED_JSON="${OUT_DIR}/operational_feed_snapshot.json"

mkdir -p "${OUT_DIR}"

TOKEN=""
for f in /etc/topinstal/gmail-agent.env "${GMAIL_ROOT}/tools/gmail_audit/.env"; do
  if [[ -f "${f}" ]]; then
    TOKEN="$(grep -m1 '^DASZEK_BRIDGE_TOKEN=' "${f}" 2>/dev/null | cut -d= -f2- | tr -d '\r' || true)"
    if [[ -z "${TOKEN}" ]]; then
      TOKEN="$(grep -m1 '^NODE_B_REGISTRY_TOKEN=' "${f}" 2>/dev/null | cut -d= -f2- | tr -d '\r' || true)"
    fi
  fi
  [[ -n "${TOKEN}" ]] && break
done

if [[ -z "${TOKEN}" ]]; then
  echo "ERROR: missing DASZEK_BRIDGE_TOKEN / NODE_B_REGISTRY_TOKEN" >&2
  exit 1
fi

cd "${GMAIL_ROOT}"

# Worker image embeds tools/gmail_audit at build time — rebuild after sync-p0-to-vps:
#   docker compose --env-file .env.vps -f docker-compose.vps.yml build gmail-agent-worker

DESK_MOUNT=()
DESK_ARGS=()
if [[ -n "${DESK_NOTES_JSON}" ]]; then
  if [[ ! -f "${DESK_NOTES_JSON}" ]]; then
    echo "ERROR: DESK_NOTES_JSON not found: ${DESK_NOTES_JSON}" >&2
    exit 1
  fi
  DESK_MOUNT=(-v "${DESK_NOTES_JSON}:/tmp/desk_notes.json:ro")
  DESK_ARGS=(--desk-notes-json /tmp/desk_notes.json)
  echo "==> B1: desk_notes enrich from ${DESK_NOTES_JSON}"
fi

echo "==> build operational feed (mailbox memory)"
docker compose --env-file .env.vps -f docker-compose.vps.yml --profile worker run --rm -T \
  -e PYTHONPATH=/app/tools/gmail_audit \
  -e GMAIL_AGENT_RUN_ID="${RUN_ID}" \
  -e DASZEK_SNAPSHOT_ENV=production \
  -v "${OUT_DIR}:/out" \
  "${DESK_MOUNT[@]}" \
  gmail-agent-worker \
  python /app/tools/gmail_audit/daszek_v3_operational_feed.py \
    --from-mailbox-memory \
    --case-limit "${CASE_LIMIT}" \
    --task-limit "${TASK_LIMIT}" \
    "${DESK_ARGS[@]}" \
    --environment production \
    --source-run-id "${RUN_ID}" \
    --out "/out/operational_feed_snapshot.json"

if [[ ! -s "${FEED_JSON}" ]]; then
  echo "ERROR: feed JSON missing at ${FEED_JSON}" >&2
  exit 1
fi

python3 - <<'PY' "${FEED_JSON}"
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
d = json.loads(p.read_text(encoding="utf-8"))
feed = d.get("feed") or {}
eligible = sum(
    1 for c in (feed.get("cases") or [])
    if isinstance(c, dict) and c.get("feedback_eligible") is True
)
print(
    "feed_counts",
    "desk=", len(feed.get("desk") or []),
    "cases=", len(feed.get("cases") or []),
    "feedback_eligible=", eligible,
    "snapshot_id=", d.get("snapshot_id"),
)
handoff = "case_062a7aa4ed7b"
for c in feed.get("cases") or []:
    if isinstance(c, dict) and c.get("case_id") == handoff:
        print(
            "b1_handoff",
            "v2_desk_note_id=", c.get("v2_desk_note_id"),
            "feedback_eligible=", c.get("feedback_eligible"),
        )
        break
PY

echo "==> POST to ${WP_HOST}"
HTTP_CODE="$(curl -sS -m 120 -o "${OUT_DIR}/post_response.json" -w "%{http_code}" \
  -X POST "${WP_HOST}/wp-json/daszek/v3/operational-feed-snapshots" \
  -H "Content-Type: application/json" \
  -H "X-Daszek-Bridge-Token: ${TOKEN}" \
  --data-binary @"${FEED_JSON}")"

echo "post_http=${HTTP_CODE}"
head -c 400 "${OUT_DIR}/post_response.json"
echo ""

if [[ "${HTTP_CODE}" != "200" && "${HTTP_CODE}" != "201" ]]; then
  echo "ERROR: POST failed" >&2
  exit 1
fi

python3 - <<'PY' "${OUT_DIR}/post_response.json"
import json, sys
from pathlib import Path
d = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
assert d.get("ok") is True, d
print("DASZEK_OPERATIONAL_FEED_PUSH_OK", d.get("snapshot_id", ""))
PY

echo "artifacts=${OUT_DIR}"
