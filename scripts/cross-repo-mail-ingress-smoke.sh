#!/usr/bin/env bash
# Optional live mail-ingress template (D1). Canonical contract proof: pytest tools/gmail_audit/tests/test_mail_ingress_contract.py
set -euo pipefail

: "${MAIL_INGRESS_URL:?Set MAIL_INGRESS_URL (e.g. https://example/wp-json/topinstal/v1/mail-ingress/cieplo-app)}"
: "${MAIL_INGRESS_AGENT_KEY:?Set MAIL_INGRESS_AGENT_KEY (redact in logs)}"

BODY='{"smoke":"d1","ts":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"}'

echo "=== D1 smoke template (run manually, capture redacted transcript) ==="
echo
echo "# Happy path (adjust JSON body to your contract):"
echo curl -sS -D - -o /dev/null -X POST \"\$MAIL_INGRESS_URL\" \\"
echo "  -H \"Content-Type: application/json\" \\"
echo "  -H \"X-Top-Instal-Agent-Key: \$MAIL_INGRESS_AGENT_KEY\" \\"
echo "  -d '$BODY'"
echo
echo "See docs/archive/runbooks/CROSS_REPO_LIVE_SMOKE_D1.md for scenario matrix."
