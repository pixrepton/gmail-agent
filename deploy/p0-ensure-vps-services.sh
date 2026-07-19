#!/usr/bin/env bash
# Idempotent Node B VPS: orchestrator bind, Caddy render, compose recreate, Gate B verify.
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
ENV_FILE="${GMAIL_ENV_FILE:-/etc/topinstal/gmail-agent.env}"

echo "==> cieplo-worker-api docker bridge (0.0.0.0:8000)"
mkdir -p /etc/systemd/system/cieplo-worker-api.service.d
cat > /etc/systemd/system/cieplo-worker-api.service.d/p0-docker-bridge.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/opt/topinstal-cieplo-worker/.venv/bin/uvicorn topinstal_cieplo_worker.main:app --host 0.0.0.0 --port 8000
EOF
systemctl daemon-reload
systemctl restart cieplo-worker-api
sleep 2

echo "==> Caddy render"
if [[ -f "${GMAIL_ROOT}/deploy/render-nodeb-caddy.sh" ]]; then
  sed -i 's/\r$//' "${GMAIL_ROOT}"/deploy/*.sh 2>/dev/null || true
  bash "${GMAIL_ROOT}/deploy/render-nodeb-caddy.sh" "${GMAIL_ROOT}/deploy/Caddyfile.nodeb-rag.template"
fi

echo "==> docker compose recreate (worker + nodeb-api)"
cd "${GMAIL_ROOT}"
if [[ -f docker-compose.vps.yml ]]; then
  docker compose --env-file .env.vps -f docker-compose.vps.yml build gmail-agent-worker 2>/dev/null || true
  docker compose --env-file .env.vps -f docker-compose.vps.yml --profile worker --profile api up -d \
    --force-recreate gmail-agent-worker gmail-agent-nodeb-api
fi
sleep 4

echo "==> ports"
ss -lntp | grep -E '8000|8443|8765' || true

echo "==> backfill orchestrator workflows"
docker exec -e PYTHONPATH=/app/tools/gmail_audit -w /app/tools/gmail_audit gmail-agent-nodeb-api \
  python scripts/backfill_correlation_registry.py --from-orchestrator-workflows || true

echo "==> Gate B verify"
VERIFY="${GMAIL_ROOT}/deploy/p0_gateb_staging_verify.sh"
if [[ -f "${VERIFY}" ]]; then
  sed -i 's/\r$//' "${VERIFY}"
  bash "${VERIFY}"
fi

echo OK_P0_ENSURE_VPS
