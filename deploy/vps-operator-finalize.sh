#!/usr/bin/env bash
# One-shot operator finalize on Node B VPS (secrets on disk, not in git).
set -euo pipefail

GMAIL_ROOT="${GMAIL_ROOT:-/opt/gmail-agent/current}"
GMAIL_ENV="${GMAIL_ENV_FILE:-/etc/topinstal/gmail-agent.env}"
ENV_VPS="${GMAIL_ROOT}/.env.vps"
PWD_LINE="${1:-/tmp/daszek-pwd-line.env}"
CIEPLO_FRAG="${2:-/tmp/topinstal-cieplo-secrets.env}"

if [[ -f "${PWD_LINE}" ]]; then
  VAL="$(grep -m1 '^DASZEK_PASSWORD=' "${PWD_LINE}" | cut -d= -f2- | tr -d '\r')"
  VAL="${VAL#\'}"; VAL="${VAL%\'}"; VAL="${VAL#\"}"; VAL="${VAL%\"}"
  python3 - "${GMAIL_ENV}" "${VAL}" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
val = sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
out, seen = [], False
for line in lines:
    if line.strip().startswith("DASZEK_PASSWORD="):
        out.append(f"DASZEK_PASSWORD='{val}'")
        seen = True
    else:
        out.append(line)
if not seen:
    out.append(f"DASZEK_PASSWORD='{val}'")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
print("OK: gmail-agent.env DASZEK_PASSWORD set (single-quoted)")
PY
fi

# Optional: mirror escaped password into .env.vps for Compose interpolation safety
if [[ -f "${ENV_VPS}" && -n "${VAL:-}" ]]; then
  ESCAPED="${VAL//\$/\$\$}"
  if grep -q '^DASZEK_PASSWORD=' "${ENV_VPS}"; then
    sed -i "s|^DASZEK_PASSWORD=.*|DASZEK_PASSWORD=${ESCAPED}|" "${ENV_VPS}"
  else
    printf '\n# Compose-safe mirror (app reads /etc/topinstal/gmail-agent.env)\nDASZEK_PASSWORD=%s\n' "${ESCAPED}" >>"${ENV_VPS}"
  fi
  echo "OK: .env.vps DASZEK_PASSWORD uses \$\$ escape"
fi

if [[ -f "${CIEPLO_FRAG}" ]]; then
  bash "${GMAIL_ROOT}/deploy/vps-merge-cieplo-secrets.sh" "${CIEPLO_FRAG}" 2>/dev/null \
    || bash /tmp/vps-merge-cieplo-secrets.sh "${CIEPLO_FRAG}"
fi

if [[ -f "${GMAIL_ROOT}/deploy/vps-compose-escape-env-vps.sh" ]]; then
  bash "${GMAIL_ROOT}/deploy/vps-compose-escape-env-vps.sh" "${ENV_VPS}"
elif [[ -f /tmp/vps-compose-escape-env-vps.sh ]]; then
  bash /tmp/vps-compose-escape-env-vps.sh "${ENV_VPS}"
fi

chmod 640 /etc/topinstal/.env
chown root:topinstal /etc/topinstal/.env
chmod 600 "${GMAIL_ENV}"

rm -f "${PWD_LINE}" "${CIEPLO_FRAG}" /tmp/daszek-pwd-line.env 2>/dev/null || true
echo "OK: operator finalize complete"
