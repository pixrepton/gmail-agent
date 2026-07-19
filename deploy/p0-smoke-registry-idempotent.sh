#!/usr/bin/env bash
set -euo pipefail
NODE_B_REGISTRY_TOKEN=$(grep -m1 '^NODE_B_REGISTRY_TOKEN=' /etc/topinstal/gmail-agent.env | cut -d= -f2- | tr -d '\r')
export NODE_B_REGISTRY_TOKEN
systemctl restart cieplo-worker-api.service 2>/dev/null || systemctl restart cieplo-worker.service 2>/dev/null || true
sleep 2
WF=$(docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -tAc \
  "SELECT target_id FROM correlation_links WHERE link_type='cieplo_workflow' LIMIT 1" 2>/dev/null | tr -d ' \n' || true)
if [[ -z "$WF" ]]; then WF="test-wf-idempotent-$(date +%s)"; fi
BODY=$(python3 -c "import json; print(json.dumps({'identity_email':'idempotent-smoke@topinstal.local','message_id':'','links':[{'link_type':'cieplo_workflow','target_id':'${WF}','source_repo':'topinstal-cieplo-orchestrator','confidence':1.0}]}))")
for i in 1 2; do
  CODE=$(curl -s -o /tmp/reg.json -w '%{http_code}' \
    -H "Authorization: Bearer ${NODE_B_REGISTRY_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$BODY" \
    http://127.0.0.1:8765/internal/registry/links)
  echo "POST attempt ${i} http=${CODE}"
done
CNT=$(docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -tAc \
  "SELECT COUNT(*) FROM correlation_links WHERE link_type='cieplo_workflow' AND target_id='${WF}' AND source_repo='topinstal-cieplo-orchestrator'" | tr -d ' \n')
echo "rows_for_workflow=${CNT}"
grep -c '_register_correlation_from_envelope' /opt/topinstal-cieplo-worker/src/topinstal_cieplo_worker/ingress/processor.py
