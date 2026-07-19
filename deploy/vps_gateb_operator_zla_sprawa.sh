#!/usr/bin/env bash
# Post zla_sprawa from OPERATOR_ROW4_HANDOFF.json via worker container (Node B VPS).
set -euo pipefail

ROOT="${ROOT:-/opt/gmail-agent/current}"
if [[ -z "${PROOF_DIR:-}" ]]; then
  echo "ERROR: set PROOF_DIR" >&2
  exit 2
fi
cd "$ROOT"

docker compose --env-file .env.vps -f docker-compose.vps.yml --profile worker run --rm \
  -v "$PROOF_DIR:/app/gate-b-proof:ro" \
  -v "$ROOT/deploy/vps_gateb_operator_zla_sprawa.py:/app/deploy/vps_gateb_operator_zla_sprawa.py:ro" \
  -e GMAIL_AGENT_ENV_FILE=/etc/topinstal/gmail-agent.env \
  gmail-agent-worker python /app/deploy/vps_gateb_operator_zla_sprawa.py /app/gate-b-proof
