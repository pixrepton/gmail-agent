#!/usr/bin/env bash
set -euo pipefail
PSQL="docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory"
$PSQL -c "
SELECT file_name, drive_item_id, extraction_status,
       length(coalesce(text_content,'')) AS text_len,
       left(coalesce(summary_text,''), 120) AS summary,
       metadata::text
FROM company_drive_documents
WHERE file_name ILIKE '%HP.xlsm%' OR file_name ILIKE '%Projekt techniczny%';"
