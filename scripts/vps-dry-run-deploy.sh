#!/usr/bin/env bash
# One-shot VPS dry-run: sync code, rebuild runtime image, run bounded proofs.
# Does NOT start the 24h worker profile (no `docker compose --profile worker up -d`).
#
# Prerequisites on the VPS: `.env.vps`, `tools/gmail_audit/.env` (see repo docker-compose.vps.yml).
#
# Usage (from repo root on the VPS, after git pull or rsync):
#   chmod +x scripts/vps-dry-run-deploy.sh
#   ./scripts/vps-dry-run-deploy.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
ENV_FILE="${ROOT}/.env.vps"
COMPOSE="${ROOT}/docker-compose.vps.yml"
PROOF_TIMEOUT_SECONDS="${PROOF_TIMEOUT_SECONDS:-180}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy from .env.vps.example and set passwords." >&2
  exit 1
fi
if [[ ! -f "${ROOT}/tools/gmail_audit/.env" ]]; then
  echo "Missing tools/gmail_audit/.env" >&2
  exit 1
fi
if ! command -v timeout >/dev/null 2>&1; then
  echo "Missing required command: timeout" >&2
  exit 1
fi

run_worker_proof() {
  timeout "${PROOF_TIMEOUT_SECONDS}s" docker compose --env-file "$ENV_FILE" -f "$COMPOSE" --profile worker run --rm gmail-agent-worker "$@"
}

echo "==> docker compose: build gmail-agent-worker image"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE" build gmail-agent-worker

echo "==> Proof 1: calendar linker ambiguous (inline assertion in runtime container)"
run_worker_proof \
  env PYTHONPATH=tools/gmail_audit python - <<'PY'
from calendar_case_linker import link_calendar_event_to_case

link = link_calendar_event_to_case(
    {"summary": "Serwis pompa", "attendees": []},
    [
        {"case_id": "case_a", "subject": "Serwis pompa"},
        {"case_id": "case_b", "subject": "Serwis pompa"},
    ],
)
assert link["case_id"] == "", link
assert link["link_status"] == "ambiguous", link
assert len(link["candidates"]) == 2, link
print("calendar_ambiguous_inline=ok")
PY

echo "==> Proof 2: document-intelligence text fixture (no Gmail; optional --persist uses mounted .env DB)"
run_worker_proof \
  python tools/gmail_audit/gmail_intake.py document-intelligence \
  --source-id "proof_fixture_v1" \
  --case-id "case_proof_fixture" \
  --filename "FV-proof.txt" \
  --mime-type "text/plain" \
  --text-file tools/gmail_audit/tests/fixtures/document_intel_proof_invoice.txt

echo "==> Proof 3: eval-summary (mailbox memory from tools/gmail_audit/.env)"
run_worker_proof \
  python tools/gmail_audit/gmail_intake.py eval-summary --window all_time

echo "==> Optional: doctor preflight (skip Gmail)"
run_worker_proof \
  python tools/gmail_audit/gmail_intake.py doctor --skip-gmail --verbose

echo "Dry-run complete. Do not start 24h worker unless explicitly approved:"
echo "  docker compose --env-file .env.vps -f docker-compose.vps.yml --profile worker up -d gmail-agent-worker"
