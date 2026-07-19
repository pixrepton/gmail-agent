#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose --env-file .env.vps -f docker-compose.local-vps.yml --profile worker run --rm \
  -e LLM_BACKEND=groq -e LLM_PRIMARY_PROVIDER=groq -e LLM_FALLBACK_PROVIDERS=cerebras,nvidia \
  -e LLM_STRUCTURED_PROVIDER_ALTERNATION=1 -e CASE_OS_RUNTIME_PROFILE=minimal -e INTAKE_LLM_BEFORE_SIGNAL=1 \
  gmail-agent-worker sh -lc '
python tools/gmail_audit/gmail_intake.py signal-run --oneshot --message-id 19ee5e5feec89939 --projection-proof >/tmp/o.json 2>/tmp/e.log || true
RUN=$(ls -td /app/tools/gmail_audit/runs/*shadow-signal-run* 2>/dev/null | head -1)
echo RUN=$RUN
python - <<PY
import json
from pathlib import Path
import glob
runs=sorted(glob.glob("/app/tools/gmail_audit/runs/*shadow-signal-run*"))
run=Path(runs[-1])
for line in (run/"stage_records.jsonl").read_text(encoding="utf-8").splitlines():
    rec=json.loads(line)
    ir=rec.get("intake_reasoning_result") or {}
    em=ir.get("execution_metadata") or {}
    if em.get("error"):
        print("ERROR", em.get("error"))
    rm=ir.get("request_meta") or em.get("request_meta") or {}
    print("attempts", json.dumps(rm.get("llm_provider_attempts") or em.get("llm_provider_attempts"), indent=2)[:1500])
    print("parse", em.get("parse_status"))
PY
tail -8 /tmp/e.log
'
