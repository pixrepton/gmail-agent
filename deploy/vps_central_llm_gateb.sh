#!/usr/bin/env bash
# Bounded Central LLM Gate B: smokes + shadow-run 1→3→10
set -euo pipefail

ROOT="${ROOT:-/opt/gmail-agent/current}"
cd "$ROOT"

PROOF_DIR="${PROOF_DIR:-$ROOT/runs/central-llm-gateb-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$PROOF_DIR"
echo "PROOF_DIR=$PROOF_DIR" | tee "$PROOF_DIR/proof.env"

WORKER="${GMAIL_WORKER_CONTAINER:-gmail-agent-vps-gmail-agent-worker-1}"

docker exec -w /app/tools/gmail_audit -e PYTHONPATH=/app/tools/gmail_audit \
  "$WORKER" python /app/deploy/vps_smoke_context.py | tee "$PROOF_DIR/vps_smoke_context.log"

docker exec -w /app/tools/gmail_audit -e PYTHONPATH=/app/tools/gmail_audit \
  "$WORKER" python /app/deploy/vps_smoke_llm.py | tee "$PROOF_DIR/vps_smoke_llm.log"

run_shadow() {
  local lim=$1
  local out=$2
  echo "=== shadow-run limit=$lim ===" | tee -a "$PROOF_DIR/run.log"
  docker compose --env-file .env.vps -f docker-compose.vps.yml exec -T \
    -w /app/tools/gmail_audit -e PYTHONPATH=/app/tools/gmail_audit \
    gmail-agent-worker \
    python gmail_intake.py shadow-run --limit "$lim" --days 14 --verbose \
    2>&1 | tee "$out"
}

run_shadow 1 "$PROOF_DIR/shadow-1.log"
run_shadow 3 "$PROOF_DIR/shadow-3.log"
run_shadow 10 "$PROOF_DIR/shadow-10.log"

echo "CENTRAL_LLM_SHADOW_DONE" | tee "$PROOF_DIR/DONE.flag"
echo "$PROOF_DIR"
