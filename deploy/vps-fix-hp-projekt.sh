#!/usr/bin/env bash
set -euo pipefail
REPO="${1:-/opt/gmail-agent/current}"
cd "$REPO"

WORKER_NAME="${WORKER_NAME:-gmail-agent-vps-gmail-agent-worker-1}"
docker cp "${REPO}/tools/gmail_audit/attachment_content_extraction.py" \
  "${WORKER_NAME}:/app/tools/gmail_audit/attachment_content_extraction.py"
docker cp "${REPO}/tools/gmail_audit/scripts/vps_fix_drive_documents.py" \
  "${WORKER_NAME}:/app/tools/gmail_audit/scripts/vps_fix_drive_documents.py"
docker cp "${REPO}/tools/gmail_audit/scripts/vps_materialize_drive_chunks.py" \
  "${WORKER_NAME}:/app/tools/gmail_audit/scripts/vps_materialize_drive_chunks.py"

WORKER=(
  docker exec
  -e GOOGLE_DRIVE_MAX_DOWNLOAD_BYTES=70000000
  -e SIGNAL_RUNTIME_MODE=legacy
  -e PYTHONPATH=/app/tools/gmail_audit
  -w /app/tools/gmail_audit
  "${WORKER_NAME}"
)

echo "=== pdftoppm in worker ==="
"${WORKER[@]}" which pdftoppm

echo "=== re-ingest HP + Projekt ==="
"${WORKER[@]}" python scripts/vps_fix_drive_documents.py | tee /tmp/drive_fix_hp_projekt.json

echo "=== embed/materialize chunks ==="
"${WORKER[@]}" python scripts/vps_materialize_drive_chunks.py | tee /tmp/drive_materialize_after_fix.json

echo "=== chunk verification ==="
docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -c "
SELECT d.file_name, d.extraction_status, length(d.text_content) AS text_len,
       COUNT(c.chunk_id) AS chunks,
       COUNT(*) FILTER (WHERE c.embedding_status='ready') AS ready
FROM company_drive_documents d
LEFT JOIN company_drive_document_chunks c ON c.document_id = d.document_id
WHERE d.file_name ILIKE '%HP.xlsm%' OR d.file_name ILIKE '%Projekt techniczny%'
GROUP BY d.file_name, d.extraction_status, length(d.text_content);"
