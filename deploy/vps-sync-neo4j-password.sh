#!/usr/bin/env bash
# Sync NEO4J_PASSWORD from .env.vps (compose auth source) into gmail-agent.env — no stdout secrets.
set -euo pipefail
REPO="${1:-/opt/gmail-agent/current}"
ENV="${2:-/etc/topinstal/gmail-agent.env}"

python3 - "$REPO" "$ENV" <<'PY'
import re
import sys
from pathlib import Path

repo = Path(sys.argv[1])
env_path = Path(sys.argv[2])
vps_env = repo / ".env.vps"


def read_pw(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^NEO4J_PASSWORD=(.*)$", text, re.MULTILINE)
    if not match:
        raise SystemExit(f"NEO4J_PASSWORD missing in {path}")
    return match.group(1).strip()


password = read_pw(vps_env)
text = env_path.read_text(encoding="utf-8")
if re.search(r"^NEO4J_PASSWORD=", text, re.MULTILINE):
    text = re.sub(r"^NEO4J_PASSWORD=.*$", f"NEO4J_PASSWORD={password}", text, count=1, flags=re.MULTILINE)
else:
    text = text.rstrip() + f"\nNEO4J_PASSWORD={password}\n"
env_path.write_text(text, encoding="utf-8")
print("NEO4J_PASSWORD synced from .env.vps (value not printed)")
PY
