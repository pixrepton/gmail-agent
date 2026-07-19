#!/usr/bin/env bash
set -euo pipefail
cd /opt/gmail-agent/current
docker compose --env-file .env.vps -f docker-compose.vps.yml run --rm --no-deps \
  -v /opt/gmail-agent/current/tools/gmail_audit:/app/tools/gmail_audit:ro \
  -w /app/tools/gmail_audit \
  -e PYTHONPATH=/app/tools/gmail_audit \
  -e GOOGLE_DRIVE_MAX_DOWNLOAD_BYTES=70000000 \
  gmail-agent-worker python -c "
from config import load_settings
from drive_client import GoogleDriveClient
from attachment_content_extraction import extract_attachment_text

settings = load_settings(require_groq=False, require_google=False)
client = GoogleDriveClient(settings)
fid = '1HhF_S22efuifKwWezRiDJC_BBeYgqhpH'
meta = client.get_file_metadata(fid)
down = client.download_content(meta, max_bytes=70000000)
print('bytes', len(down.data), 'mime', down.mime_type)
for docling_on in (True, False):
    r = extract_attachment_text(
        down.data,
        mime_type=down.mime_type,
        file_name='Projekt techniczny skan.pdf',
        docling_enabled=docling_on,
        docling_options={'max_pages': 40, 'timeout_sec': 180},
    )
    print('docling', docling_on, 'status', r.get('extraction_status'), 'method', r.get('extraction_method'), 'len', len(r.get('extracted_text') or ''))
"
