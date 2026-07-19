#!/usr/bin/env bash
set -euo pipefail
cd /opt/gmail-agent/current
docker compose --env-file .env.vps -f docker-compose.vps.yml run --rm --no-deps -w /app/tools/gmail_audit \
  -v /opt/gmail-agent/current/tools/gmail_audit/scripts:/app/tools/gmail_audit/scripts:ro \
  gmail-agent-worker env PYTHONPATH=/app/tools/gmail_audit python scripts/vps_extract_facts_smoke.py
