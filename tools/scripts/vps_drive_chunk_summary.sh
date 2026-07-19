#!/usr/bin/env bash
set -euo pipefail
PSQL="docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory"
$PSQL -c "
SELECT d.file_name, d.extraction_status, d.lane,
       COUNT(c.chunk_id) AS chunks,
       COUNT(*) FILTER (WHERE c.embedding_status='ready') AS ready
FROM company_drive_documents d
LEFT JOIN company_drive_document_chunks c ON c.document_id = d.document_id
WHERE d.updated_at >= '2026-05-23'
GROUP BY d.file_name, d.extraction_status, d.lane
ORDER BY ready DESC, d.file_name;"
