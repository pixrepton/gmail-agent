#!/usr/bin/env bash
set -euo pipefail
docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -c "
SELECT observation_id, left(source_fingerprint, 24),
       (payload_json ? 'document_row') AS has_doc,
       left(coalesce(payload_json->'document_row'->>'file_name',''), 40) AS file_name,
       length(coalesce(payload_json->'document_row'->>'text_content','')) AS text_len
FROM mailbox_memory_raw_observations
WHERE source_kind = 'drive'
  AND created_at::date = '2026-05-08'
ORDER BY created_at DESC
LIMIT 10;"
