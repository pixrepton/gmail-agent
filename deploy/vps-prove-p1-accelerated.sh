#!/usr/bin/env bash
# Accelerated P1.2b proof: timer path (smoke) + live emit + timer (production emitter).
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
ENV_VPS="${GMAIL_ROOT}/.env.vps"

echo "==> rebuild + recreate worker (pick up new gmail_audit scripts)"
docker compose --env-file "${ENV_VPS}" -f "${GMAIL_ROOT}/docker-compose.vps.yml" build gmail-agent-worker
docker compose --env-file "${ENV_VPS}" -f "${GMAIL_ROOT}/docker-compose.vps.yml" \
  --profile worker up -d --force-recreate gmail-agent-worker

echo "==> step 1: timer path (systemd oneshot)"
bash "${GMAIL_ROOT}/deploy/vps-prove-p1-timer-path.sh"

echo "==> step 2: live publish_os_event + timer"
bash "${GMAIL_ROOT}/deploy/vps-prove-p1-live-emit-timer.sh"

echo "P1_ACCELERATED_PROVE_OK"
