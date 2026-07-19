#!/usr/bin/env bash
# Move production secrets out of repo tree (one-time VPS / staging).
set -euo pipefail

SRC="${1:-/opt/gmail-agent/current/tools/gmail_audit/.env}"
DEST="${2:-/etc/topinstal/gmail-agent.env}"

if [[ ! -f "$SRC" ]]; then
  echo "Source env missing: $SRC" >&2
  exit 1
fi

install -d -m 750 /etc/topinstal
if [[ -f "$DEST" ]]; then
  cp "$DEST" "${DEST}.bak.$(date +%Y%m%d%H%M%S)"
fi
cp "$SRC" "$DEST"
chmod 600 "$DEST"
chown root:root "$DEST"

# Stub in repo tree — never deploy real secrets via tar again.
cat > "$(dirname "$SRC")/.env" <<'STUB'
# Production secrets live in /etc/topinstal/gmail-agent.env (not synced by deploy tarballs).
# Docker sets GMAIL_AGENT_ENV_FILE=/etc/topinstal/gmail-agent.env
STUB

echo "OK: migrated $SRC -> $DEST"
