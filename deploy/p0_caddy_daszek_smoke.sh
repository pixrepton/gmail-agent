#!/usr/bin/env bash
set -euo pipefail
TOKEN=$(grep -m1 '^NODE_B_REGISTRY_TOKEN=' /etc/topinstal/gmail-agent.env | cut -d= -f2- | tr -d '\r')
CASE="${1:-case_c02cfc10b5b9}"
CODE=$(curl -sS -m 15 -o /tmp/caddy_eng.json -w '%{http_code}' \
  -H "Authorization: Bearer ${TOKEN}" \
  "http://127.0.0.1:8443/cases/${CASE}/engagement")
echo "caddy_case_engagement_http=${CODE}"
head -c 200 /tmp/caddy_eng.json
echo
