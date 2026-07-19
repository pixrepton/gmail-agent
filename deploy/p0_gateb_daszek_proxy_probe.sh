#!/usr/bin/env bash
set -euo pipefail
CASE=case_c02cfc10b5b9
TOKEN=$(grep -m1 '^NODE_B_REGISTRY_TOKEN=' /etc/topinstal/gmail-agent.env | cut -d= -f2- | tr -d '\r')
OUT=/opt/gmail-agent/current/runs/p0-correlation-registry-gateb-20260522T202101Z
mkdir -p "$OUT"
CODE=$(curl -sS -m 15 -o "$OUT/case_engagement.json" -w "%{http_code}" \
  -H "Authorization: Bearer ${TOKEN}" \
  "http://127.0.0.1:8765/cases/${CASE}/engagement")
echo "case_engagement_http=${CODE}"
python3 - <<PY
import json
from pathlib import Path
p = Path("$OUT/case_engagement.json")
d = json.loads(p.read_text(encoding="utf-8"))
labels = d.get("labels_pl") or {}
eng = d.get("engagement") or d
links = (eng.get("correlation_links") if isinstance(eng, dict) else None) or d.get("correlation_links") or []
has_wf = any((x or {}).get("link_type") == "cieplo_workflow" for x in links)
text = json.dumps(labels, ensure_ascii=False).lower()
assert "lead" not in text, labels
print("labels_pl_ok", labels)
print("has_cieplo_workflow_link", has_wf)
print("DASZEK_PROXY_PROBE_OK")
PY
