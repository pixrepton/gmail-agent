#!/usr/bin/env bash
set -euo pipefail
cd "${1:-/opt/gmail-agent/current}"
docker compose --env-file .env.vps -f docker-compose.vps.yml exec -u 0 gmail-agent-worker \
  sh -c 'apt-get update -qq && apt-get install -y -qq poppler-utils && which pdftoppm'
