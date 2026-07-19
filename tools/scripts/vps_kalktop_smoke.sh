#!/usr/bin/env bash
set -euo pipefail

load_env_key() {
  local key="$1"
  python3 - "$key" <<'PY'
import sys
from pathlib import Path
want = sys.argv[1]
for raw in Path("/etc/topinstal/.env").read_text(encoding="utf-8", errors="replace").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    if k.strip() == want:
        print(v.strip().strip('"').strip("'"))
        break
PY
}

KALKTOP_BASE_URL="$(load_env_key KALKTOP_BASE_URL)"
KALKTOP_AGENT_KEY="$(load_env_key KALKTOP_AGENT_KEY)"

if [[ -z "${KALKTOP_BASE_URL}" || -z "${KALKTOP_AGENT_KEY}" ]]; then
  echo "ERROR=MISSING_KALKTOP_ENV"
  exit 2
fi

URL="${KALKTOP_BASE_URL%/}/wp-json/topinstal/v1/calculate-offer"
CODE="$(curl -sS -o /tmp/kalktop-smoke.out -w "%{http_code}" \
  -H "X-Top-Instal-Agent-Key: ${KALKTOP_AGENT_KEY}" \
  -H "Authorization: Bearer ${KALKTOP_AGENT_KEY}" \
  -H "Content-Type: application/json" \
  -X POST "${URL}" \
  -d '{}')"

echo "HTTP_CODE=${CODE}"
echo "TARGET_HOST=${KALKTOP_BASE_URL#*//}"
echo "TARGET_HOST=${TARGET_HOST%%/*}"

python3 - <<'PY'
import json, re
from pathlib import Path
raw = Path("/tmp/kalktop-smoke.out").read_text(encoding="utf-8", errors="replace")[:2000]
try:
    obj = json.loads(raw)
    print("BODY_JSON=" + json.dumps(obj, ensure_ascii=False)[:600])
except Exception:
    safe = re.sub(r"[A-Za-z0-9_\-]{24,}", "***", raw)
    print("BODY_PREVIEW=" + safe[:600])
PY
