#!/usr/bin/env bash
# Gate B Row 4 only — requires same PROOF_DIR as row3-stop + real operator zla_sprawa click
set -euo pipefail

ROOT="${ROOT:-/opt/gmail-agent/current}"
if [[ "$ROOT" != "/opt/gmail-agent/current" ]]; then
  echo "ERROR: ROOT must be /opt/gmail-agent/current (got: $ROOT)" >&2
  exit 2
fi
cd "$ROOT"

if [[ -z "${PROOF_DIR:-}" ]]; then
  echo "ERROR: set PROOF_DIR to the Row 3 proof directory before running Row 4" >&2
  exit 2
fi
export PROOF_DIR
export GATE_B_SKIP_OPERATOR_PAUSE=1

python3 scripts/gate_b_runtime_proof.py render-script \
  --proof-dir "$PROOF_DIR" \
  --env-file .env.vps \
  --compose-file docker-compose.vps.yml \
  --service gmail-agent-worker \
  --phase row4-only \
  --output "$PROOF_DIR/gate-b-row4.sh"

chmod +x "$PROOF_DIR/gate-b-row4.sh"
bash "$PROOF_DIR/gate-b-row4.sh" 2>&1 | tee "$PROOF_DIR/logs/row4-only-runner.log"

python3 scripts/gate_b_runtime_proof.py classify --proof-dir "$PROOF_DIR" | tee "$PROOF_DIR/classify-row4.json"
echo "GATEB_PROOF_DIR=$PROOF_DIR"
