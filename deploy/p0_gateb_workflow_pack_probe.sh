#!/usr/bin/env bash
set -euo pipefail
WF="${1:-3d930273-f681-4cea-aad0-c09a4f86e9c2}"
TOKEN=$(grep -m1 '^NODE_B_REGISTRY_TOKEN=' /etc/topinstal/gmail-agent.env | cut -d= -f2- | tr -d '\r')
echo "=== gmail-agent.env workflow vars ==="
grep -E '^(CIEPLO_WORKFLOW|CIEPLO_WORKER|NODE_B_REGISTRY)' /etc/topinstal/gmail-agent.env | sed 's/=.*/=<set>/' || true
for port in 8000 8765; do
  code=$(curl -sS -o /tmp/wf.json -w "%{http_code}" \
    -H "Authorization: Bearer ${TOKEN}" \
    "http://127.0.0.1:${port}/internal/workflows/${WF}/context-pack" || echo 000)
  echo "port_${port}_http=${code}"
  head -c 200 /tmp/wf.json 2>/dev/null; echo
done
docker ps --format '{{.Names}}' | grep -E 'cieplo|orchestrator|worker' || true
