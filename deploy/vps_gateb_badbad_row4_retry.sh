#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/opt/gmail-agent/current}"
PROOF_DIR="${PROOF_DIR:-/opt/gmail-agent/current/runs/gate-b-proof-20260525T234800Z}"
cd "$ROOT"

export PROOF_DIR GATE_B_SKIP_OPERATOR_PAUSE=1
export ENV_FILE=.env.vps
export COMPOSE_FILE=docker-compose.vps.yml
export SERVICE=gmail-agent-worker

echo "[fix] journal backfill + re-queue zla_sprawa"
docker compose --env-file .env.vps -f docker-compose.vps.yml --profile worker run --rm \
  -v "$ROOT/deploy/vps_gateb_badbad_row4_fix.py:/app/deploy/vps_gateb_badbad_row4_fix.py:ro" \
  -e GMAIL_AGENT_ENV_FILE=/etc/topinstal/gmail-agent.env \
  gmail-agent-worker python /app/deploy/vps_gateb_badbad_row4_fix.py

python3 scripts/gate_b_runtime_proof.py render-script \
  --proof-dir "$PROOF_DIR" \
  --env-file .env.vps \
  --compose-file docker-compose.vps.yml \
  --service gmail-agent-worker \
  --phase row4-only \
  --output "$PROOF_DIR/gate-b-row4-retry.sh"

chmod +x "$PROOF_DIR/gate-b-row4-retry.sh"
bash "$PROOF_DIR/gate-b-row4-retry.sh" 2>&1 | tee "$PROOF_DIR/logs/row4-retry-runner.log"

python3 scripts/gate_b_runtime_proof.py classify --proof-dir "$PROOF_DIR" | tee "$PROOF_DIR/classify-row4-retry.json"
echo "GATEB_PROOF_DIR=$PROOF_DIR"
