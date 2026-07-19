#!/usr/bin/env bash
# P1: merge duplicate identities by primary_email (Node B VPS).
# Uses HOST code under GMAIL_ROOT (bind-mount) so sync-p0-to-vps applies without image rebuild.
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
ENV_VPS="${GMAIL_ROOT}/.env.vps"
OUT_DIR="${OUT_DIR:-${GMAIL_ROOT}/runs/p1-identity-email-dedup-$(date -u +%Y%m%dT%H%M%SZ)}"
MODE="${MODE:-dry-run}" # dry-run | write
LIMIT="${LIMIT:-0}"

_fix_lf() {
  local f
  for f in "$@"; do
    [[ -f "$f" ]] && sed -i 's/\r$//' "$f" 2>/dev/null || true
  done
}

_fix_lf "${GMAIL_ROOT}/deploy/p1-reconcile-identity-emails.sh"

mkdir -p "${OUT_DIR}"

echo "OUT_DIR=${OUT_DIR}"
echo "MODE=${MODE}"
echo "LIMIT=${LIMIT}"
echo "GMAIL_ROOT=${GMAIL_ROOT}"

PSQL=(docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -t -A)
SCRIPT_HOST="${GMAIL_ROOT}/tools/gmail_audit/scripts/reconcile_identity_emails.py"

if [[ ! -f "${SCRIPT_HOST}" ]]; then
  echo "ERROR: missing ${SCRIPT_HOST} (run sync-p0-to-vps.ps1 first)" >&2
  exit 2
fi

if ! grep -q 'duplicate_groups_before' "${SCRIPT_HOST}" 2>/dev/null; then
  echo "WARN: reconcile script on host looks stale (no duplicate_groups_before); sync recommended" >&2
fi

echo "==> metrics_before"
"${PSQL[@]}" -c "
SELECT count(*) AS duplicate_email_identity_groups
FROM (
  SELECT lower(primary_email) AS email_norm, COUNT(DISTINCT identity_id) AS n
  FROM topinstal_identities
  WHERE primary_email <> ''
  GROUP BY lower(primary_email)
  HAVING COUNT(DISTINCT identity_id) > 1
) t;
" | tr -d '[:space:]' | tee "${OUT_DIR}/duplicate_groups_before.txt"

echo "==> reconcile (host-mounted gmail_audit)"
ARGS=()
if [[ "${MODE}" == "dry-run" ]]; then
  ARGS+=(--dry-run)
elif [[ "${MODE}" == "write" ]]; then
  ARGS+=(--apply)
else
  echo "ERROR: MODE must be dry-run or write" >&2
  exit 2
fi
if [[ "${LIMIT}" != "0" ]]; then
  ARGS+=(--limit "${LIMIT}")
fi
if [[ "${MODE}" == "write" && "${LIMIT}" == "0" ]]; then
  ARGS+=(--require-zero-after)
fi

cd "${GMAIL_ROOT}"
docker compose --env-file "${ENV_VPS}" -f docker-compose.vps.yml --profile worker run --rm --no-deps \
  -v "${GMAIL_ROOT}/tools/gmail_audit:/app/tools/gmail_audit:ro" \
  -v /etc/topinstal/gmail-agent.env:/etc/topinstal/gmail-agent.env:ro \
  -e GMAIL_AGENT_ENV_FILE=/etc/topinstal/gmail-agent.env \
  -e PYTHONPATH=/app/tools/gmail_audit \
  -w /app/tools/gmail_audit \
  gmail-agent-worker \
  python scripts/reconcile_identity_emails.py "${ARGS[@]}" | tee "${OUT_DIR}/reconcile.log"

echo "==> metrics_after"
"${PSQL[@]}" -c "
SELECT count(*) AS duplicate_email_identity_groups
FROM (
  SELECT lower(primary_email) AS email_norm, COUNT(DISTINCT identity_id) AS n
  FROM topinstal_identities
  WHERE primary_email <> ''
  GROUP BY lower(primary_email)
  HAVING COUNT(DISTINCT identity_id) > 1
) t;
" | tr -d '[:space:]' | tee "${OUT_DIR}/duplicate_groups_after.txt"

echo "OK_P1_RECONCILE"
