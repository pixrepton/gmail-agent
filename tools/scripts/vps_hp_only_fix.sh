#!/usr/bin/env bash
set -euo pipefail
REPO="${1:-/opt/gmail-agent/current}"
WORKER="${WORKER_NAME:-gmail-agent-vps-gmail-agent-worker-1}"
docker cp "${REPO}/tools/gmail_audit/drive_ingest_runtime.py" "${WORKER}:/app/tools/gmail_audit/drive_ingest_runtime.py"
docker cp "${REPO}/tools/gmail_audit/attachment_content_extraction.py" "${WORKER}:/app/tools/gmail_audit/attachment_content_extraction.py"
docker cp "${REPO}/tools/gmail_audit/scripts/vps_fix_drive_documents.py" "${WORKER}:/app/tools/gmail_audit/scripts/vps_fix_drive_documents.py"
docker exec -e PYTHONPATH=/app/tools/gmail_audit -e GOOGLE_DRIVE_MAX_DOWNLOAD_BYTES=10000000 \
  -w /app/tools/gmail_audit "${WORKER}" \
  python scripts/vps_fix_drive_documents.py | tee /tmp/drive_fix_hp_only.json
docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -c "
SELECT d.file_name, d.extraction_status,
       length(d.text_content) AS text_len,
       COUNT(c.chunk_id) AS chunks,
       COUNT(*) FILTER (WHERE c.embedding_status='ready') AS ready
FROM company_drive_documents d
LEFT JOIN company_drive_document_chunks c ON c.document_id = d.document_id
WHERE d.file_name ILIKE '%HP.xlsm%'
GROUP BY d.file_name, d.extraction_status, length(d.text_content);"
