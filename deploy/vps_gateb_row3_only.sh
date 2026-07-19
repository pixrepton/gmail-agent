#!/usr/bin/env bash
# Gate B Row 3 only on Node B VPS (row3-stop → classify → operator handoff file)
set -euo pipefail

ROOT="${ROOT:-/opt/gmail-agent/current}"
if [[ "$ROOT" != "/opt/gmail-agent/current" ]]; then
  echo "ERROR: ROOT must be /opt/gmail-agent/current (got: $ROOT)" >&2
  exit 2
fi
cd "$ROOT"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PROOF_DIR="${PROOF_DIR:-$ROOT/runs/gate-b-proof-$STAMP}"
export PROOF_DIR
export GATE_B_DOCTOR_NONFATAL="${GATE_B_DOCTOR_NONFATAL:-1}"
# Prefer groq primary + cerebras fallback; alternation rotates per stage (LLM_BACKEND=groq).
export LLM_BACKEND="${LLM_BACKEND:-groq}"
export LLM_PRIMARY_PROVIDER="${LLM_PRIMARY_PROVIDER:-groq}"
export LLM_FALLBACK_PROVIDERS="${LLM_FALLBACK_PROVIDERS:-cerebras}"
export LLM_STRUCTURED_PROVIDER_ALTERNATION="${LLM_STRUCTURED_PROVIDER_ALTERNATION:-1}"
export DASZEK_V2_READBACK_ENABLED="${DASZEK_V2_READBACK_ENABLED:-1}"

mkdir -p "$PROOF_DIR/logs"

echo "[gate-b] PROOF_DIR=$PROOF_DIR"

python3 scripts/gate_b_runtime_proof.py render-script \
  --proof-dir "$PROOF_DIR" \
  --env-file .env.vps \
  --compose-file docker-compose.vps.yml \
  --service gmail-agent-worker \
  --phase row3-stop \
  --row3-exclude-message-id "gateb_badbad_20260525T010256Z" \
  --row3-exclude-message-id "19e5bbd58b518553" \
  --output "$PROOF_DIR/gate-b-row3.sh"

chmod +x "$PROOF_DIR/gate-b-row3.sh"
bash "$PROOF_DIR/gate-b-row3.sh" 2>&1 | tee "$PROOF_DIR/logs/row3-runner.log"

python3 scripts/gate_b_runtime_proof.py classify --proof-dir "$PROOF_DIR" | tee "$PROOF_DIR/classify-row3.json"
python3 scripts/gate_b_runtime_proof.py write-handoff \
  --proof-dir "$PROOF_DIR" \
  --output "$PROOF_DIR/OPERATOR_ROW4_HANDOFF.txt" \
  --json-output "$PROOF_DIR/OPERATOR_ROW4_HANDOFF.json" || true

echo "GATEB_PROOF_DIR=$PROOF_DIR"
echo "HANDOFF=$PROOF_DIR/OPERATOR_ROW4_HANDOFF.txt"
