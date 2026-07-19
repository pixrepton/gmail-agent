#!/usr/bin/env bash
# Luka #3: bounded operator loop (feed -> Zła sprawa -> bridge drain -> reconcile -> feed regresja).
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
ENV_VPS="${ENV_VPS_FILE:-${GMAIL_ROOT}/.env.vps}"
COMPOSE=(docker compose --env-file "${ENV_VPS}" -f "${GMAIL_ROOT}/docker-compose.vps.yml")
RUN_ID="operator-loop-luka3-$(date -u +%Y%m%dT%H%M%SZ)"
PROOF_DIR="${PROOF_DIR:-${GMAIL_ROOT}/runs/${RUN_ID}}"
# auto = POST zla_sprawa z fixtures; manual = operator klika w UI, potem Enter na VPS
LUKA3_MODE="${LUKA3_MODE:-auto}"
HANDOFF_FIXTURE="${HANDOFF_FIXTURE:-${GMAIL_ROOT}/deploy/fixtures/luka3_operator_loop_handoff.json}"
SKIP_FEED_PUSH="${SKIP_FEED_PUSH:-0}"

mkdir -p "${PROOF_DIR}/logs"

run_worker() {
  "${COMPOSE[@]}" --profile worker run --rm -T \
    -e GMAIL_AGENT_ENV_FILE=/etc/topinstal/gmail-agent.env \
    -e PYTHONPATH=/app/tools/gmail_audit \
    "$@"
}

echo "==> Luka #3 operator loop proof"
echo "PROOF_DIR=${PROOF_DIR}"
echo "LUKA3_MODE=${LUKA3_MODE}"

echo "==> preflight doctor (Daszek)"
run_worker gmail-agent-worker \
  python tools/gmail_audit/gmail_intake.py doctor --skip-gmail --check-daszek --verbose \
  > "${PROOF_DIR}/doctor.json" 2> "${PROOF_DIR}/logs/doctor.stderr.log" || true

cp "${HANDOFF_FIXTURE}" "${PROOF_DIR}/OPERATOR_ROW4_HANDOFF.json"

echo "==> pending before (dry-run)"
run_worker gmail-agent-worker \
  python tools/gmail_audit/gmail_intake.py daszek-bridge-drain \
  --remote --domain adjudication --dry-run --max-items 5 \
  > "${PROOF_DIR}/pending-before-drain.json" 2> "${PROOF_DIR}/logs/pending-before.stderr.log"

if [[ "${LUKA3_MODE}" == "auto" ]]; then
  echo "==> auto: POST zla_sprawa (fixture handoff)"
  run_worker -v "${PROOF_DIR}:/app/proof:rw" \
    -v "${GMAIL_ROOT}/deploy/vps_gateb_operator_zla_sprawa.py:/app/deploy/vps_gateb_operator_zla_sprawa.py:ro" \
    gmail-agent-worker \
    python /app/deploy/vps_gateb_operator_zla_sprawa.py /app/proof \
    > "${PROOF_DIR}/operator-zla-sprawa.json" 2> "${PROOF_DIR}/logs/operator-zla-sprawa.stderr.log"
else
  echo "==> manual: operator — kliknij «Zła sprawa» na karcie TEST w Daszek, potem Enter"
  read -r
fi

echo "==> pending after click (dry-run)"
run_worker gmail-agent-worker \
  python tools/gmail_audit/gmail_intake.py daszek-bridge-drain \
  --remote --domain adjudication --dry-run --max-items 5 \
  > "${PROOF_DIR}/pending-after-click.json" 2> "${PROOF_DIR}/logs/pending-after-click.stderr.log"

echo "==> real bridge drain (max 1)"
run_worker gmail-agent-worker \
  python tools/gmail_audit/gmail_intake.py daszek-bridge-drain \
  --remote --domain adjudication --max-items 1 \
  --run-id "${RUN_ID}" \
  > "${PROOF_DIR}/drain-output.json" 2> "${PROOF_DIR}/logs/drain-output.stderr.log"

echo "==> pending after drain"
run_worker gmail-agent-worker \
  python tools/gmail_audit/gmail_intake.py daszek-bridge-drain \
  --remote --domain adjudication --dry-run --max-items 5 \
  > "${PROOF_DIR}/pending-after-drain.json" 2> "${PROOF_DIR}/logs/pending-after-drain.stderr.log"

python3 "${GMAIL_ROOT}/deploy/vps-prove-operator-loop-validate.py" "${PROOF_DIR}"

if [[ "${SKIP_FEED_PUSH}" != "1" ]]; then
  echo "==> feed regresja po reconcile (bez B1)"
  bash "${GMAIL_ROOT}/deploy/push_daszek_operational_feed_prod.sh" \
    > "${PROOF_DIR}/logs/feed-push.log" 2>&1
  tail -n 20 "${PROOF_DIR}/logs/feed-push.log" || true
fi

echo "LUKA3_OPERATOR_LOOP_PROVE_OK ${PROOF_DIR}"
