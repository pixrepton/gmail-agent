#!/usr/bin/env bash
set -euo pipefail
ENV_FILE=/etc/topinstal/gmail-agent.env
WF=3d930273-f681-4cea-aad0-c09a4f86e9c2
TOKEN=$(grep -m1 '^NODE_B_REGISTRY_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r')

echo "=== from host :8000 ==="
curl -sS -m 5 -o /dev/null -w "host_8000=%{http_code}\n" \
  -H "Authorization: Bearer ${TOKEN}" \
  "http://127.0.0.1:8000/internal/workflows/${WF}/context-pack"

echo "=== from nodeb-api container ==="
docker exec gmail-agent-nodeb-api sh -c "
  wget -q -O- --timeout=3 http://host.docker.internal:8000/ 2>/dev/null | head -c 80 || echo host_docker_internal_fail
  wget -q -O- --timeout=3 http://172.17.0.1:8000/ 2>/dev/null | head -c 80 || echo gateway_fail
" || true

# Prefer host gateway for Linux Docker
if ! grep -q '^CIEPLO_WORKFLOW_CONTEXT_BASE_URL=http://172.17.0.1:8000' "$ENV_FILE"; then
  if grep -q '^CIEPLO_WORKFLOW_CONTEXT_BASE_URL=' "$ENV_FILE"; then
    sed -i 's|^CIEPLO_WORKFLOW_CONTEXT_BASE_URL=.*|CIEPLO_WORKFLOW_CONTEXT_BASE_URL=http://172.17.0.1:8000|' "$ENV_FILE"
  else
    echo 'CIEPLO_WORKFLOW_CONTEXT_BASE_URL=http://172.17.0.1:8000' >> "$ENV_FILE"
  fi
  echo "UPDATED CIEPLO_WORKFLOW_CONTEXT_BASE_URL=http://172.17.0.1:8000"
fi

grep CIEPLO_WORKFLOW_CONTEXT_BASE_URL "$ENV_FILE"

echo "=== recreate nodeb-api if compose present ==="
if [ -d /opt/gmail-agent/current ]; then
  cd /opt/gmail-agent/current
  if docker compose ps nodeb-api 2>/dev/null | grep -q nodeb; then
    docker compose up -d nodeb-api --force-recreate 2>/dev/null || docker compose up -d gmail-agent-nodeb-api --force-recreate 2>/dev/null || true
  fi
fi
docker restart gmail-agent-nodeb-api 2>/dev/null || true
sleep 3

ENG=6048589a-e64c-474a-8b41-56cf4224502c
CODE=$(curl -sS -m 15 -o /tmp/snap2.json -w "%{http_code}" \
  -H "Authorization: Bearer ${TOKEN}" \
  "http://127.0.0.1:8765/engagements/${ENG}/snapshot")
echo "snapshot_after_fix_http=${CODE}"
python3 -c "
import json
d=json.load(open('/tmp/snap2.json'))
print('has_workflow_pack', d.get('workflow_context_pack') is not None)
print('missing', d.get('missing_components'))
"
