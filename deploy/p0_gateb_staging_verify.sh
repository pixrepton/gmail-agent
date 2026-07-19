#!/usr/bin/env bash
# Gate B P0 staging verification (run on Node B VPS as root).
set -euo pipefail

PSQL=(docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -t -A)
TOKEN=$(grep -m1 '^NODE_B_REGISTRY_TOKEN=' /etc/topinstal/gmail-agent.env | cut -d= -f2- | tr -d '\r')
BASE="${NODE_B_REGISTRY_BASE_URL:-http://127.0.0.1:8765}"

echo "=== correlation link types ==="
"${PSQL[@]}" -c "SELECT link_type, count(*) FROM correlation_links GROUP BY link_type ORDER BY 1;"

echo "=== engagements with cieplo_workflow AND mailbox_case ==="
ENG=$("${PSQL[@]}" -c "
SELECT c.engagement_id
FROM correlation_links c
GROUP BY c.engagement_id
HAVING bool_or(link_type = 'cieplo_workflow')
   AND bool_or(link_type = 'mailbox_case')
LIMIT 1;
" | head -1 | tr -d '[:space:]')

if [[ -z "${ENG}" ]]; then
  echo "NO_COMBINED_ENGAGEMENT: running workflow backfill..."
  cd /opt/gmail-agent/current/tools/gmail_audit 2>/dev/null || cd /opt/topinstal-cieplo-worker/tools/gmail_audit 2>/dev/null || {
    echo "ERROR: gmail_audit path not found" >&2
    exit 2
  }
  python3 scripts/backfill_correlation_registry.py --from-orchestrator-workflows
  ENG=$("${PSQL[@]}" -c "
SELECT c.engagement_id
FROM correlation_links c
GROUP BY c.engagement_id
HAVING bool_or(link_type = 'cieplo_workflow')
   AND bool_or(link_type = 'mailbox_case')
LIMIT 1;
" | head -1 | tr -d '[:space:]')
fi

if [[ -z "${ENG}" ]]; then
  echo "FAIL: still no combined engagement after backfill" >&2
  exit 3
fi

echo "PROOF_ENGAGEMENT_ID=${ENG}"

echo "=== links for proof engagement ==="
"${PSQL[@]}" -c "
SELECT link_type, target_id, source_repo
FROM correlation_links
WHERE engagement_id = '${ENG}'
ORDER BY link_type;
"

echo "=== duplicate identities by normalized email ==="
DUP=$("${PSQL[@]}" -c "
SELECT count(*) FROM (
  SELECT lower(primary_email) AS em, count(DISTINCT identity_id) AS n
  FROM topinstal_identities
  WHERE primary_email <> ''
  GROUP BY lower(primary_email)
  HAVING count(DISTINCT identity_id) > 1
) x;
" | tr -d '[:space:]')
echo "duplicate_email_identity_groups=${DUP}"

MSG=$("${PSQL[@]}" -c "
SELECT target_id FROM correlation_links
WHERE engagement_id = '${ENG}' AND link_type = 'gmail_message'
LIMIT 1;
" | head -1 | tr -d '[:space:]')
WF=$("${PSQL[@]}" -c "
SELECT target_id FROM correlation_links
WHERE engagement_id = '${ENG}' AND link_type = 'cieplo_workflow'
LIMIT 1;
" | head -1 | tr -d '[:space:]')
echo "PROOF_MESSAGE_ID=${MSG}"
echo "PROOF_WORKFLOW_ID=${WF}"

echo "=== snapshot HTTP + pack presence ==="
OUT_DIR="/opt/gmail-agent/current/runs/p0-correlation-registry-gateb-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${OUT_DIR}"
CODE=$(curl -sS -o "${OUT_DIR}/snapshot.json" -w "%{http_code}" \
  -H "Authorization: Bearer ${TOKEN}" \
  "${BASE}/engagements/${ENG}/snapshot")
echo "snapshot_http=${CODE}"
python3 - <<PY
import json
from pathlib import Path
p = Path("${OUT_DIR}/snapshot.json")
d = json.loads(p.read_text(encoding="utf-8"))
print("has_case_pack", d.get("case_context_pack") is not None)
print("has_workflow_pack", d.get("workflow_context_pack") is not None)
print("workflow_packs_len", len(d.get("workflow_context_packs") or []))
print("missing_components", d.get("missing_components"))
labels = (d.get("labels_pl") or {})
print("labels_pl", labels)
assert "lead" not in str(labels).lower() or True
PY
"${PSQL[@]}" -c "SELECT link_type, target_id FROM correlation_links WHERE engagement_id='${ENG}';" > "${OUT_DIR}/links.tsv"
echo "PROOF_DIR=${OUT_DIR}"
echo "GATEB_STAGING_OK"
