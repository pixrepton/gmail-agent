#!/usr/bin/env bash
# Render /etc/caddy/nodeb-rag.caddy from template + /etc/topinstal/gmail-agent.env
set -euo pipefail

TEMPLATE="${1:-/opt/gmail-agent/current/deploy/Caddyfile.nodeb-rag.template}"
OUT="/etc/caddy/Caddyfile"
ENV_FILE="${GMAIL_ENV_FILE:-/etc/topinstal/gmail-agent.env}"
RAG_IP="${RAG_ALLOW_IP:-46.224.235.86}"
WP_IP="${WP_ALLOW_IP:-185.110.51.208}"

TOKEN="$(grep -m1 '^NODE_B_REGISTRY_TOKEN=' "$ENV_FILE" | cut -d= -f2- || true)"
if [[ -z "$TOKEN" ]]; then
  TOKEN="$(grep -m1 '^DASZEK_BRIDGE_TOKEN=' "$ENV_FILE" | cut -d= -f2- || true)"
fi
[[ -n "$TOKEN" ]] || { echo "NODE_B_REGISTRY_TOKEN missing in $ENV_FILE" >&2; exit 1; }

mkdir -p /etc/caddy
if [[ -f "$OUT" ]]; then
  cp "$OUT" "${OUT}.bak.$(date +%Y%m%d%H%M%S)"
fi
sed -e "s|{RAG_ALLOW_IP}|${RAG_IP}|g" \
    -e "s|{WP_ALLOW_IP}|${WP_IP}|g" \
    -e "s|{NODE_B_REGISTRY_TOKEN}|${TOKEN}|g" \
    "$TEMPLATE" > "$OUT"
chown root:caddy "$OUT"
chmod 640 "$OUT"

systemctl enable caddy
systemctl reload caddy || systemctl restart caddy
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q active; then
  ufw allow from "${RAG_IP}" to any port 8443 proto tcp comment 'RAG NodeB API' 2>/dev/null || true
  ufw allow from "${WP_IP}" to any port 8443 proto tcp comment 'WP Daszek NodeB API' 2>/dev/null || true
fi
echo "OK: $OUT (RAG ${RAG_IP}, WP ${WP_IP}, port 8443)"
