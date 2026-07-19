#!/usr/bin/env bash
docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -c "
SELECT chunk_id, embedding_status, left(coalesce(embedding_error,''), 120) AS err
FROM company_drive_document_chunks
WHERE document_id = 'gdoc_e5e1191f0362';"
