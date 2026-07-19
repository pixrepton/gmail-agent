#!/usr/bin/env bash
# Escape literal $ as $$ in .env.vps for Docker Compose variable substitution.
set -euo pipefail

ENV_VPS="${1:-/opt/gmail-agent/current/.env.vps}"

if [[ ! -f "${ENV_VPS}" ]]; then
  echo "WARN: ${ENV_VPS} missing — skip compose escape" >&2
  exit 0
fi

cp "${ENV_VPS}" "${ENV_VPS}.bak.$(date -u +%Y%m%dT%H%M%SZ)"

python3 - <<'PY'
import re
from pathlib import Path

path = Path("/opt/gmail-agent/current/.env.vps")
if not path.is_file():
    raise SystemExit(0)
lines = path.read_text(encoding="utf-8").splitlines()
out = []
changed = 0
for line in lines:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in line:
        out.append(line)
        continue
    key, _, val = line.partition("=")
    key = key.strip()
    # Only escape values that look like passwords/keys with a single leading $
    if key in {
        "DASZEK_PASSWORD",
        "MAILBOX_MEMORY_POSTGRES_PASSWORD",
        "NEO4J_PASSWORD",
    } and val.startswith("$") and not val.startswith("$$"):
        val = val.replace("$", "$$")
        changed += 1
    out.append(f"{key}={val}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"OK: compose escape applied to {path} ({changed} keys)")
PY
