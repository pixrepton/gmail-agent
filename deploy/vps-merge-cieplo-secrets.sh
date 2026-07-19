#!/usr/bin/env bash
# Merge cieplo-worker secrets from operator-provided fragment into /etc/topinstal/.env
set -euo pipefail

FRAGMENT="${1:-/tmp/topinstal-cieplo-secrets.env}"
ORCH_ENV="${ORCH_ENV_FILE:-/etc/topinstal/.env}"

if [[ ! -f "${FRAGMENT}" ]]; then
  echo "ERROR: missing fragment ${FRAGMENT}" >&2
  exit 1
fi

install -d -m 755 /etc/topinstal
touch "${ORCH_ENV}"
cp "${ORCH_ENV}" "${ORCH_ENV}.bak.$(date -u +%Y%m%dT%H%M%SZ)"

python3 - <<'PY'
import re
from pathlib import Path

orch = Path("/etc/topinstal/.env")
frag = Path("/tmp/topinstal-cieplo-secrets.env")
keys = {}
for path in (frag,):
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        keys[k.strip()] = v.strip()

lines = orch.read_text(encoding="utf-8").splitlines() if orch.is_file() else []
seen = set()
out = []
for line in lines:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in line:
        out.append(line)
        continue
    k, _, _ = line.partition("=")
    k = k.strip()
    if k in keys:
        out.append(f"{k}={keys[k]}")
        seen.add(k)
    else:
        out.append(line)
for k, v in keys.items():
    if k not in seen:
        out.append(f"{k}={v}")
orch.write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"OK: merged {len(keys)} keys into {orch}")
PY

chmod 640 "${ORCH_ENV}"
chown root:topinstal "${ORCH_ENV}"
rm -f "${FRAGMENT}"
echo "OK: cieplo secrets merged"
