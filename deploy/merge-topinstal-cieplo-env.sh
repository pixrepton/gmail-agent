#!/usr/bin/env bash
# Idempotent merge of /etc/topinstal/.env keys for cieplo-worker poll readiness.
# Does not overwrite non-empty existing values. Copies NODE_B_REGISTRY_TOKEN from gmail-agent.env.
set -euo pipefail

GMAIL_ENV="${GMAIL_ENV_FILE:-/etc/topinstal/gmail-agent.env}"
ORCH_ENV="${ORCH_ENV_FILE:-/etc/topinstal/.env}"
TOKEN="${NODE_B_REGISTRY_TOKEN:-}"

_get_env_kv() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 0
  grep -m1 "^${key}=" "$file" 2>/dev/null | cut -d= -f2- | tr -d '\r' || true
}

_ensure_kv_if_empty() {
  local file="$1" key="$2" default="$3"
  local cur
  cur="$(_get_env_kv "$file" "$key")"
  if [[ -n "${cur}" ]]; then
    return 0
  fi
  install -d -m 755 "$(dirname "$file")"
  touch "$file"
  chmod 640 "$file" 2>/dev/null || true
  chown root:topinstal "$file" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$default" >>"$file"
  echo "  appended ${key} (placeholder — set real secret before live poll)"
}

if [[ -z "${TOKEN}" ]]; then
  TOKEN="$(_get_env_kv "${GMAIL_ENV}" NODE_B_REGISTRY_TOKEN)"
fi
if [[ -z "${TOKEN}" ]]; then
  TOKEN="$(_get_env_kv "${GMAIL_ENV}" DASZEK_BRIDGE_TOKEN)"
fi

install -d -m 755 /etc/topinstal
touch "${ORCH_ENV}"
chmod 640 "${ORCH_ENV}" 2>/dev/null || true
chown root:topinstal "${ORCH_ENV}" 2>/dev/null || true

if [[ -n "${TOKEN}" ]]; then
  if grep -q '^NODE_B_REGISTRY_TOKEN=' "${ORCH_ENV}" 2>/dev/null; then
    sed -i "s|^NODE_B_REGISTRY_TOKEN=.*|NODE_B_REGISTRY_TOKEN=${TOKEN}|" "${ORCH_ENV}"
  else
    echo "NODE_B_REGISTRY_TOKEN=${TOKEN}" >>"${ORCH_ENV}"
  fi
fi

_ensure_kv_if_empty "${ORCH_ENV}" NODE_B_REGISTRY_BASE_URL "http://127.0.0.1:8765"
_ensure_kv_if_empty "${ORCH_ENV}" ORCHESTRATOR_PROCESSING_ENABLED "true"
_ensure_kv_if_empty "${ORCH_ENV}" KALKTOP_BASE_URL "https://topinstal.com.pl"
_ensure_kv_if_empty "${ORCH_ENV}" KALKTOP_AGENT_KEY "CHANGE_ME_KALKTOP_AGENT_KEY"
_ensure_kv_if_empty "${ORCH_ENV}" KALKTOP_TIMEOUT_S "120"
_ensure_kv_if_empty "${ORCH_ENV}" GENERATOR_BASE_URL "https://topinstal.com.pl"
_ensure_kv_if_empty "${ORCH_ENV}" GENERATOR_AGENT_KEY "CHANGE_ME_GENERATOR_AGENT_KEY"
_ensure_kv_if_empty "${ORCH_ENV}" GENERATOR_TIMEOUT_S "180"
_ensure_kv_if_empty "${ORCH_ENV}" SMTP_HOST "localhost"
_ensure_kv_if_empty "${ORCH_ENV}" SMTP_PORT "25"
_ensure_kv_if_empty "${ORCH_ENV}" SMTP_FROM "noreply@topinstal.com.pl"
_ensure_kv_if_empty "${ORCH_ENV}" SMTP_USE_SSL "false"
_ensure_kv_if_empty "${ORCH_ENV}" SMTP_USE_TLS "false"
_ensure_kv_if_empty "${ORCH_ENV}" INTERNAL_REVIEW_TO "office@topinstal.com.pl"
_ensure_kv_if_empty "${ORCH_ENV}" CIEPLO_FETCH_TIMEOUT_S "35"

echo "OK: merged cieplo-worker env keys into ${ORCH_ENV}"
