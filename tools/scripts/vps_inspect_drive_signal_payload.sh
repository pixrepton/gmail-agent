#!/usr/bin/env bash
set -euo pipefail
PSQL="docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory"
$PSQL -c "
SELECT signal_id, signal_kind, processing_state,
       left(signal_summary_pl, 60) AS summary,
       (payload_json ? 'document_row') AS has_document_row,
       (payload_json ? 'chunk_rows') AS has_chunk_rows,
       coalesce(jsonb_array_length(payload_json->'chunk_rows'), 0) AS chunk_rows_len,
       left(coalesce(payload_json->'document_row'->>'file_name',''), 50) AS file_name
FROM mailbox_memory_signals
WHERE source_kind = 'drive'
  AND observed_at::date = '2026-05-08'
ORDER BY observed_at
LIMIT 20;"
