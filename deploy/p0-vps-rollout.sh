#!/usr/bin/env bash
# P0 correlation registry + shadow signal worker — idempotent VPS rollout (Node B).
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
ORCH_ROOT="${ORCH_ROOT:-/opt/topinstal-cieplo-worker}"
GMAIL_ENV="${GMAIL_ENV_FILE:-/etc/topinstal/gmail-agent.env}"
ORCH_ENV="${ORCH_ENV_FILE:-/etc/topinstal/.env}"
ENV_VPS="${GMAIL_ROOT}/.env.vps"
WORKER_CMD='python tools/gmail_audit/gmail_intake.py signal-worker --loop --verbose'

_fix_lf() {
  local f
  for f in "$@"; do
    [[ -f "$f" ]] && sed -i 's/\r$//' "$f" 2>/dev/null || true
  done
}

_get_env_kv() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 0
  grep -m1 "^${key}=" "$file" 2>/dev/null | cut -d= -f2- | tr -d '\r' || true
}

_set_env_kv() {
  local file="$1" key="$2" value="$3"
  install -d -m 755 "$(dirname "$file")"
  touch "$file"
  if [[ "$file" == /etc/topinstal/gmail-agent.env ]]; then
    chmod 600 "$file" 2>/dev/null || true
    chown root:root "$file" 2>/dev/null || true
  else
    chmod 640 "$file" 2>/dev/null || true
    chown root:topinstal "$file" 2>/dev/null || true
  fi
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    local tmp
    tmp="$(mktemp)"
    awk -v k="$key" -v v="$value" '
      BEGIN { done=0 }
      $0 ~ "^" k "=" { print k "=" v; done=1; next }
      { print }
      END { if (!done) print k "=" v }
    ' "$file" >"$tmp"
    mv "$tmp" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >>"$file"
  fi
}

_resolve_bridge_token() {
  local token="${NODE_B_REGISTRY_TOKEN:-}"
  if [[ -n "${token}" ]]; then
    printf '%s' "$token"
    return 0
  fi
  for key in DASZEK_BRIDGE_TOKEN NODE_B_REGISTRY_TOKEN; do
    token="$(_get_env_kv "${GMAIL_ENV}" "$key")"
    if [[ -n "${token}" ]]; then
      printf '%s' "$token"
      return 0
    fi
  done
  local legacy="${GMAIL_ROOT}/tools/gmail_audit/.env"
  for key in DASZEK_BRIDGE_TOKEN NODE_B_REGISTRY_TOKEN; do
    token="$(_get_env_kv "${legacy}" "$key")"
    if [[ -n "${token}" ]]; then
      printf '%s' "$token"
      return 0
    fi
  done
  return 1
}

_fix_lf "${GMAIL_ROOT}/deploy/"*.sh "${GMAIL_ROOT}/scripts/"*.sh 2>/dev/null || true

if [[ ! -f "${GMAIL_ENV}" ]]; then
  legacy="${GMAIL_ROOT}/tools/gmail_audit/.env"
  if [[ -f "${legacy}" ]]; then
    echo "==> migrate app env to ${GMAIL_ENV}"
    bash "${GMAIL_ROOT}/deploy/migrate-gmail-agent-env.sh" "${legacy}" "${GMAIL_ENV}"
  else
    echo "ERROR: missing ${GMAIL_ENV} (and no legacy tools/gmail_audit/.env)" >&2
    exit 1
  fi
fi

TOKEN="$(_resolve_bridge_token)" || {
  echo "NODE_B_REGISTRY_TOKEN or DASZEK_BRIDGE_TOKEN required in ${GMAIL_ENV}" >&2
  exit 1
}

echo "==> shadow signal flags in ${GMAIL_ENV}"
_set_env_kv "${GMAIL_ENV}" SIGNAL_RUNTIME_MODE shadow
_set_env_kv "${GMAIL_ENV}" SIGNAL_WORKER_ENABLED 1
_set_env_kv "${GMAIL_ENV}" DASZEK_BRIDGE_TOKEN "${TOKEN}"
_set_env_kv "${GMAIL_ENV}" NODE_B_REGISTRY_TOKEN "${TOKEN}"
# Shadow/dry-run: do not attach Daszek session at worker boot (avoids crash loop on bad login).
_set_env_kv "${GMAIL_ENV}" DASZEK_V2_PUSH 0
_set_env_kv "${GMAIL_ENV}" DASZEK_V2_READBACK_ENABLED 0

if [[ -f "${ENV_VPS}" ]]; then
  echo "==> worker command in .env.vps"
  _set_env_kv "${ENV_VPS}" GMAIL_AGENT_WORKER_COMMAND "${WORKER_CMD}"
fi

if [[ -f "${GMAIL_ROOT}/deploy/merge-topinstal-cieplo-env.sh" ]]; then
  echo "==> orchestrator /etc/topinstal/.env placeholders"
  GMAIL_ENV_FILE="${GMAIL_ENV}" ORCH_ENV_FILE="${ORCH_ENV}" NODE_B_REGISTRY_TOKEN="${TOKEN}" \
    bash "${GMAIL_ROOT}/deploy/merge-topinstal-cieplo-env.sh"
fi
if [[ -f "${GMAIL_ROOT}/deploy/vps-audit-cieplo-env.sh" ]]; then
  bash "${GMAIL_ROOT}/deploy/vps-audit-cieplo-env.sh" || echo "WARN: cieplo env audit failed (set KALKTOP/GENERATOR/SMTP before live poll)"
fi

export DASZEK_BRIDGE_TOKEN="${TOKEN}"
export NODE_B_REGISTRY_TOKEN="${TOKEN}"

cd "${GMAIL_ROOT}"

if [[ -f "${GMAIL_ROOT}/deploy/vps-fix-daszek-password-env.py" ]]; then
  echo "==> quote DASZEK_PASSWORD in gmail-agent.env (literal \$ in password)"
  python3 "${GMAIL_ROOT}/deploy/vps-fix-daszek-password-env.py" "${GMAIL_ENV}" || true
fi
if [[ -f "${GMAIL_ROOT}/deploy/vps-compose-escape-env-vps.sh" ]]; then
  echo "==> escape \$ -> \$\$ in .env.vps for Compose"
  bash "${GMAIL_ROOT}/deploy/vps-compose-escape-env-vps.sh" "${ENV_VPS}"
fi

echo "==> docker build gmail-agent-runtime"
docker compose --env-file .env.vps -f docker-compose.vps.yml build gmail-agent-worker

echo "==> start worker + Node B API (8765)"
docker compose --env-file .env.vps -f docker-compose.vps.yml --profile worker --profile api up -d \
  --force-recreate gmail-agent-worker gmail-agent-nodeb-api
docker restart gmail-agent-nodeb-api 2>/dev/null || true

echo "==> P1 event spine schema bootstrap"
docker exec -e PYTHONPATH=/app/tools/gmail_audit -w /app/tools/gmail_audit gmail-agent-nodeb-api \
  python scripts/vps_p0_bootstrap_once.py
if [[ -f "${GMAIL_ROOT}/deploy/vps-verify-event-spine-schema.sh" ]]; then
  echo "==> P1 event spine schema verify (processing_* columns)"
  bash "${GMAIL_ROOT}/deploy/vps-verify-event-spine-schema.sh"
fi

echo "==> orchestrator env (Node B registry + workflow API)"
_set_env_kv "${ORCH_ENV}" NODE_B_REGISTRY_BASE_URL "http://127.0.0.1:8765"
_set_env_kv "${ORCH_ENV}" NODE_B_REGISTRY_TOKEN "${TOKEN}"
chmod 640 "${ORCH_ENV}" 2>/dev/null || true
chown root:topinstal "${ORCH_ENV}" 2>/dev/null || true

echo "==> start cieplo-worker-api (8000)"
systemctl enable cieplo-worker-api.service 2>/dev/null || true
systemctl restart cieplo-worker-api.service

sleep 8
echo "==> smoke"
curl -sf "http://127.0.0.1:8765/health" | head -c 200 || { echo "Node B API health FAILED"; exit 1; }
echo ""
for _try in 1 2 3 4 5; do
  if curl -sf "http://127.0.0.1:8000/healthz" | head -c 200; then
    echo ""
    break
  fi
  if [[ "${_try}" -eq 5 ]]; then
    echo "Orchestrator API health FAILED" >&2
    systemctl status cieplo-worker-api --no-pager -l | tail -15 >&2 || true
    exit 1
  fi
  sleep 3
done
echo ""

echo "==> worker container status"
docker ps --filter name=gmail-agent-worker --format '{{.Names}} {{.Status}}'

echo "OK: P0 VPS rollout complete (shadow signal worker)"
