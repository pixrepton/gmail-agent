#!/usr/bin/env bash
# P1.3 proof: POST cieplo_workflow_persisted via Node B API, process via shadow timer/oneshot.
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
GMAIL_ENV="${GMAIL_ENV_FILE:-/etc/topinstal/gmail-agent.env}"
ENV_VPS="${GMAIL_ROOT}/.env.vps"
WORKFLOW_ID="${P13_WORKFLOW_ID:-p1-proof-workflow-$(date -u +%Y%m%dT%H%M%SZ)}"

TOKEN=""
for f in "${GMAIL_ENV}" "${GMAIL_ROOT}/tools/gmail_audit/.env"; do
  if [[ -f "${f}" ]]; then
    TOKEN="$(grep -m1 '^NODE_B_REGISTRY_TOKEN=' "${f}" 2>/dev/null | cut -d= -f2- | tr -d '\r' || true)"
    [[ -n "${TOKEN}" ]] && break
  fi
done
if [[ -z "${TOKEN}" ]]; then
  echo "ERROR: NODE_B_REGISTRY_TOKEN missing" >&2
  exit 1
fi

export WF="${WORKFLOW_ID}"
PAYLOAD="$(python3 - <<PY
import json, os
print(json.dumps({
  "event_type": "cieplo_workflow_persisted",
  "source_repo": "topinstal-cieplo-orchestrator",
  "payload": {
    "workflow_id": os.environ["WF"],
    "message_id": "p13-proof-message",
    "request_id": "p13-proof-request",
    "proof": "p1_3_vps_proof",
  },
  "correlation": {
    "workflow_id": os.environ["WF"],
    "trace_id": "p13-proof-trace",
  },
}))
PY
)"

echo "==> POST /internal/os-events workflow_id=${WORKFLOW_ID}"
HTTP_CODE="$(curl -sS -m 30 -o /tmp/p13_emit.json -w "%{http_code}" \
  -X POST "http://127.0.0.1:8765/internal/os-events" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "${PAYLOAD}")"
cat /tmp/p13_emit.json
echo ""
echo "http=${HTTP_CODE}"
[[ "${HTTP_CODE}" == "200" ]] || exit 1

EVENT_ID="$(python3 -c "import json; print(json.load(open('/tmp/p13_emit.json'))['event_id'])")"
echo "event_id=${EVENT_ID}"

echo "==> processor one shot"
docker compose --env-file "${ENV_VPS}" -f "${GMAIL_ROOT}/docker-compose.vps.yml" exec -T \
  -e EVENT_SPINE_PROCESSOR_ENABLED=1 -e EVENT_SPINE_PROCESSOR_MODE=shadow \
  gmail-agent-worker \
  python tools/gmail_audit/gmail_intake.py event-spine-processor --max-iterations 1 --verbose

docker compose --env-file "${ENV_VPS}" -f "${GMAIL_ROOT}/docker-compose.vps.yml" exec -T mailbox-memory-db \
  psql -U mailbox_memory -d mailbox_memory -c \
  "SELECT event_id, event_type, processing_status FROM unified_os_events WHERE event_id='${EVENT_ID}';"

echo "P1_ORCHESTRATOR_EMIT_PROVE_OK event_id=${EVENT_ID}"
