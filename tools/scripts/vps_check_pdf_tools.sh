#!/usr/bin/env bash
set -euo pipefail
cd /opt/gmail-agent/current
docker compose --env-file .env.vps -f docker-compose.vps.yml run --rm --no-deps gmail-agent-worker sh -c '
which pdftoppm pdfinfo tesseract 2>/dev/null || true
python -c "import importlib.util; print(\"fitz\", bool(importlib.util.find_spec(\"fitz\"))); print(\"pdf2image\", bool(importlib.util.find_spec(\"pdf2image\")))"
'
