#!/usr/bin/env bash
set -euo pipefail
PSQL="docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory"
$PSQL -c "SELECT run_id, status, stats, updated_at FROM drive_ingest_runs ORDER BY updated_at DESC LIMIT 3;"
$PSQL -t -A -c "SELECT COUNT(*) FROM mailbox_memory_signals WHERE source_kind='drive';"
$PSQL -t -A -c "SELECT COUNT(*) FROM mailbox_memory_raw_observations WHERE source_kind='drive';"
