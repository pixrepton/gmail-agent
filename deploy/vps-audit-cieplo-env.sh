#!/usr/bin/env bash
# Fail when orchestrator env still has merge-script placeholders (KALKTOP/GENERATOR/SMTP).
set -euo pipefail

ORCH_ENV="${ORCH_ENV_FILE:-/etc/topinstal/.env}"
FAIL=0

if [[ ! -f "${ORCH_ENV}" ]]; then
  echo "FAIL: missing ${ORCH_ENV}" >&2
  exit 1
fi

while IFS= read -r line; do
  key="${line%%=*}"
  val="${line#*=}"
  case "${key}" in
    KALKTOP_AGENT_KEY|GENERATOR_AGENT_KEY|SMTP_PASSWORD|SMTP_USER)
      if [[ "${val}" == CHANGE_ME_* ]] || [[ -z "${val}" ]]; then
        echo "FAIL: ${key} is placeholder or empty in ${ORCH_ENV}" >&2
        FAIL=1
      else
        echo "OK: ${key} set"
      fi
      ;;
  esac
done < <(grep -E '^(KALKTOP_AGENT_KEY|GENERATOR_AGENT_KEY|SMTP_PASSWORD|SMTP_USER)=' "${ORCH_ENV}" || true)

if [[ "${FAIL}" -ne 0 ]]; then
  echo "Hint: set real secrets on VPS (do not commit). merge-topinstal-cieplo-env.sh only fills empty keys." >&2
  exit 1
fi

echo "OK: cieplo-worker env audit passed for ${ORCH_ENV}"
