#!/usr/bin/env bash
# Daszek Gate B on Node B VPS — default row3-stop (use vps_gateb_row4_only.sh after operator click)
set -euo pipefail

ROOT="${ROOT:-/opt/gmail-agent/current}"
if [[ "$ROOT" != "/opt/gmail-agent/current" ]]; then
  echo "ERROR: ROOT must be /opt/gmail-agent/current (got: $ROOT)" >&2
  exit 2
fi
cd "$ROOT"

PROOF_DIR="${PROOF_DIR:-$ROOT/runs/gate-b-proof-$(date -u +%Y%m%dT%H%M%SZ)}"
export PROOF_DIR
export GATE_B_SKIP_OPERATOR_PAUSE="${GATE_B_SKIP_OPERATOR_PAUSE:-1}"
export GATE_B_DOCTOR_NONFATAL="${GATE_B_DOCTOR_NONFATAL:-1}"
GATE_B_PHASE="${GATE_B_PHASE:-row3-stop}"

python3 scripts/gate_b_runtime_proof.py render-script \
  --proof-dir "$PROOF_DIR" \
  --env-file .env.vps \
  --compose-file docker-compose.vps.yml \
  --service gmail-agent-worker \
  --phase "$GATE_B_PHASE" \
  --row3-exclude-message-id "gateb_badbad_20260525T010256Z" \
  --output "$PROOF_DIR/gate-b-runtime-run.sh"

chmod +x "$PROOF_DIR/gate-b-runtime-run.sh"
bash "$PROOF_DIR/gate-b-runtime-run.sh" 2>&1 | tee "$PROOF_DIR/gate-b-runner.log"

python3 scripts/gate_b_runtime_proof.py classify --proof-dir "$PROOF_DIR" | tee "$PROOF_DIR/classify.json"
echo "GATEB_PROOF_DIR=$PROOF_DIR"
