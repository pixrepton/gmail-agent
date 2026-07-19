#!/usr/bin/env bash
# Bounded Daszek push readiness: BOM-safe login via doctor; optional DASZEK_V2_PUSH=1.
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
GMAIL_ENV="${GMAIL_ENV_FILE:-/etc/topinstal/gmail-agent.env}"
ENV_VPS="${ENV_VPS_FILE:-${GMAIL_ROOT}/.env.vps}"
COMPOSE=(docker compose --env-file "${ENV_VPS}" -f "${GMAIL_ROOT}/docker-compose.vps.yml")
ENABLE_PUSH="${ENABLE_DASZEK_PUSH:-1}"

_set_env_kv() {
  local file="$1" key="$2" val="$3"
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$file"
  else
    echo "${key}=${val}" >>"$file"
  fi
}

echo "==> doctor Daszek (login + v2 surface)"
"${COMPOSE[@]}" exec -T gmail-agent-worker \
  python tools/gmail_audit/gmail_intake.py doctor --skip-gmail --check-daszek --verbose \
  > /tmp/daszek-doctor-bounded.json

python3 -c "
import json, sys
d=json.load(open('/tmp/daszek-doctor-bounded.json'))
checks=d.get('checks') or {}
for k in ('daszek','daszek_v2_operator_surface'):
    st=(checks.get(k) or {}).get('status')
    print(f'{k}: {st}')
    if st != 'ok':
        sys.exit(f'FAIL: {k} status={st!r}')
print('OK: daszek doctor bounded')
"

if [[ "${ENABLE_PUSH}" == "1" ]]; then
  echo "==> enable DASZEK_V2_PUSH=1 (READBACK stays 0)"
  _set_env_kv "${GMAIL_ENV}" DASZEK_V2_PUSH 1
  _set_env_kv "${GMAIL_ENV}" DASZEK_V2_READBACK_ENABLED 0
  echo "==> recreate worker with push enabled"
  "${COMPOSE[@]}" up -d --force-recreate gmail-agent-worker
  sleep 8
  if ! "${COMPOSE[@]}" ps gmail-agent-worker | grep -q Up; then
    echo "FAIL: worker not Up after push enable" >&2
    exit 1
  fi
  echo "OK: DASZEK_V2_PUSH=1 worker Up (bounded; no cohort replay)"
fi
