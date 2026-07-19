#!/usr/bin/env bash
# Deploy continuous signal-worker on Node B VPS (Docker Compose worker profile).
#
# Encodes runbook steps: stack up → doctor preflight → optional bounded signal-worker →
# build/recreate gmail-agent-worker with GMAIL_AGENT_WORKER_COMMAND pointing at signal-worker loop.
#
# Prerequisites (repo root on VPS, typically /opt/gmail-agent/current):
#   - .env.vps (passwords + GMAIL_AGENT_WORKER_COMMAND containing signal-worker)
#   - tools/gmail_audit/.env (MAILBOX_MEMORY_DATABASE_URL=...@mailbox-memory-db:5432/...,
#     SIGNAL_RUNTIME_MODE=shadow|active, SIGNAL_WORKER_ENABLED=1, neo4j://neo4j:7687 for pilot, etc.)
#     Optional: CASE_INTELLIGENCE_VNEXT_ENABLED, DECISION_PIPELINE_ENABLED, … (same file; see
#     docs/archive/runbooks/DECISION_PIPELINE_FLAGS.md)
#
# Optional env (shell):
#   VPS_SIGNAL_SOAK_BOUNDED_ITERATIONS   If set to a positive int, runs one-off bounded worker before detach.
#   VPS_SIGNAL_SOAK_SKIP_DOCTOR=1       Skip canonical doctor preflight (not recommended).
#   VPS_SIGNAL_SOAK_NO_DEPLOY=1         Stop after bounded/doctor; do not bring worker daemon up -d.
#
# Usage:
#   chmod +x scripts/vps-signal-worker-soak-deploy.sh
#   ./scripts/vps-signal-worker-soak-deploy.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ENV_FILE="${ROOT}/.env.vps"
COMPOSE="${ROOT}/docker-compose.vps.yml"
APP_ENV="${ROOT}/tools/gmail_audit/.env"

STACK_SCRIPT="${ROOT}/scripts/vps-stack-up.sh"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy from .env.vps.example and configure." >&2
  exit 1
fi

if ! grep -q '^GMAIL_AGENT_WORKER_COMMAND=' "$ENV_FILE" \
  || grep -Eq '^GMAIL_AGENT_WORKER_COMMAND=[[:space:]]*$' "$ENV_FILE"; then
  echo "Missing or empty GMAIL_AGENT_WORKER_COMMAND in .env.vps." >&2
  exit 1
fi

if ! grep -E '^GMAIL_AGENT_WORKER_COMMAND=.*signal-worker' "$ENV_FILE"; then
  echo "ERROR: .env.vps GMAIL_AGENT_WORKER_COMMAND must include signal-worker, e.g.:" >&2
  echo '  GMAIL_AGENT_WORKER_COMMAND=python tools/gmail_audit/gmail_intake.py signal-worker --loop --verbose' >&2
  exit 1
fi

if [[ ! -f "$APP_ENV" ]]; then
  echo "Missing $APP_ENV" >&2
  exit 1
fi

if ! grep -qE '^MAILBOX_MEMORY_DATABASE_URL=.*mailbox-memory-db' "$APP_ENV"; then
  echo "WARN: MAILBOX_MEMORY_DATABASE_URL should reference host mailbox-memory-db (not 127.0.0.1) for gmail-agent-worker on compose network." >&2
fi

if ! grep -q '^SIGNAL_WORKER_ENABLED=1' "$APP_ENV"; then
  echo "WARN: tools/gmail_audit/.env should set SIGNAL_WORKER_ENABLED=1 for signal-worker." >&2
fi

if ! grep -qE '^SIGNAL_RUNTIME_MODE=(shadow|active)' "$APP_ENV"; then
  echo "WARN: SIGNAL_RUNTIME_MODE should be shadow or active (not legacy) for unified signal runtime." >&2
fi

if grep -qE '^NEO4J_URI=neo4j://127\.0\.0\.1' "$APP_ENV"; then
  echo "WARN: NEO4J_URI uses 127.0.0.1 — inside worker container prefer neo4j://neo4j:7687 (compose service)." >&2
fi

compose_run_worker() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE" --profile worker run -T --rm gmail-agent-worker "$@"
}

echo "==> Base stack (Postgres + Neo4j + Ollama + ollama-pull)"
bash "$STACK_SCRIPT"

if [[ "${VPS_SIGNAL_SOAK_SKIP_DOCTOR:-0}" != "1" ]]; then
  echo "==> Preflight doctor (Gmail API + Drive + Daszek readback)"
  compose_run_worker \
    python tools/gmail_audit/gmail_intake.py doctor \
    --gmail-source google_api \
    --check-drive \
    --check-daszek \
    --check-daszek-v2-read \
    --verbose
fi

_bounded="${VPS_SIGNAL_SOAK_BOUNDED_ITERATIONS:-}"
if [[ -n "${_bounded}" ]]; then
  if ! [[ "${_bounded}" =~ ^[0-9]+$ ]] || [[ "${_bounded}" -lt 1 ]]; then
    echo "VPS_SIGNAL_SOAK_BOUNDED_ITERATIONS must be a positive integer, got ${_bounded}" >&2
    exit 1
  fi
  echo "==> Bounded signal-worker (max-iterations=${_bounded})"
  compose_run_worker \
    python tools/gmail_audit/gmail_intake.py signal-worker \
    --loop \
    --max-iterations "${_bounded}" \
    --verbose
fi

if [[ "${VPS_SIGNAL_SOAK_NO_DEPLOY:-0}" == "1" ]]; then
  echo "VPS_SIGNAL_SOAK_NO_DEPLOY=1 — skipping worker detach. Exiting OK."
  exit 0
fi

echo "==> Build gmail-agent-worker image"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE" build gmail-agent-worker

echo "==> Up worker (detached)"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE" --profile worker up -d gmail-agent-worker --force-recreate

_worker_cid=""
_worker_cid="$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE" --profile worker ps -q gmail-agent-worker 2>/dev/null | head -n1)" || true
if [[ -z "${_worker_cid}" ]]; then
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE" --profile worker ps -a || true
  echo "WARN: Could not resolve gmail-agent-worker container id; check docker compose ps manually." >&2
else
  echo "Worker container id: ${_worker_cid}"
  echo "==> Recent logs:"
  docker logs --tail 80 "${_worker_cid}" 2>/dev/null || true
fi

echo ""
echo "24h soak: monitor docker logs volume runs/ Postgres growth. Not Gate B proven until recorded in proof pack."
