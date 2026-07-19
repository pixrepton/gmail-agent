#!/usr/bin/env bash
# Session 3 — read-only Drive history: env, DB runs, documents, signals, cursors, worker logs.
set -euo pipefail
cd /opt/gmail-agent/current

PSQL="docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -t -A"

echo "=== DRIVE ENV (non-secret) ==="
grep -E '^(GOOGLE_DRIVE_|SIGNAL_RUNTIME_MODE|DRIVE_CHANGE|DOCLING_|MAILBOX_MEMORY_STAGE)' /etc/topinstal/gmail-agent.env 2>/dev/null \
  | grep -v CREDENTIALS | grep -v PASSWORD || true

echo ""
echo "=== DOCTOR drive slice (from /tmp/doctor.json if present) ==="
if [ -f /tmp/doctor.json ]; then
  python3 -c "
import json
d=json.load(open('/tmp/doctor.json'))
checks=d.get('checks') or {}
drive=checks.get('drive') or d.get('drive') or {}
print(json.dumps(drive, ensure_ascii=False, indent=2)[:2500])
" 2>/dev/null || echo "(parse failed)"
else
  echo "(no /tmp/doctor.json — run doctor first)"
fi

echo ""
echo "=== drive_ingest_runs (last 15) ==="
$PSQL -c "
SELECT run_id || E'\t' || status || E'\t' || left(root_folder_id,12) || E'\t' ||
       coalesce(stats->>'processed_count','?') || '/' ||
       coalesce(stats->>'stored_document_count','?') || E'\t' ||
       updated_at::text
FROM drive_ingest_runs
ORDER BY updated_at DESC NULLS LAST
LIMIT 15;" 2>/dev/null || echo "(table missing)"

echo ""
echo "=== company_drive_documents ==="
$PSQL -c "SELECT COUNT(*)::text || ' total rows' FROM company_drive_documents;" 2>/dev/null || echo "(table missing)"
$PSQL -c "
SELECT coalesce(lane,'') || E'\t' || coalesce(scope,'') || E'\t' || COUNT(*)::text
FROM company_drive_documents
GROUP BY 1,2 ORDER BY 3 DESC LIMIT 12;" 2>/dev/null || true
$PSQL -c "
SELECT left(file_name,48) || E'\t' || left(coalesce(case_id,''),20) || E'\t' || updated_at::text
FROM company_drive_documents
ORDER BY updated_at DESC NULLS LAST LIMIT 8;" 2>/dev/null || true

echo ""
echo "=== company_drive_document_chunks (embedding_status) ==="
$PSQL -c "
SELECT embedding_status, COUNT(*)::text FROM company_drive_document_chunks
GROUP BY 1 ORDER BY 2 DESC;" 2>/dev/null || echo "(empty/missing)"

echo ""
echo "=== company_drive_facts (top keys) ==="
$PSQL -c "
SELECT fact_key, COUNT(*)::text FROM company_drive_facts
GROUP BY 1 ORDER BY 2 DESC LIMIT 12;" 2>/dev/null || echo "(empty/missing)"

echo ""
echo "=== mailbox_memory_source_cursors (drive) ==="
$PSQL -c "
SELECT source_kind, cursor_scope, left(coalesce(cursor_value,''),24), updated_at::text
FROM mailbox_memory_source_cursors
WHERE source_kind ILIKE '%drive%'
ORDER BY updated_at DESC;" 2>/dev/null || true

echo ""
echo "=== mailbox_memory_signals (drive, last 10) ==="
$PSQL -c "
SELECT signal_id, signal_kind, processing_state, left(signal_summary_pl,40), observed_at::text
FROM mailbox_memory_signals
WHERE source_kind = 'drive'
ORDER BY observed_at DESC NULLS LAST
LIMIT 10;" 2>/dev/null || echo "(none)"

echo ""
echo "=== mailbox_memory_raw_observations (drive, last 5) ==="
$PSQL -c "
SELECT observation_kind, source_kind, left(source_fingerprint,32), created_at::text
FROM mailbox_memory_raw_observations
WHERE source_kind ILIKE '%drive%'
ORDER BY created_at DESC LIMIT 5;" 2>/dev/null || true

echo ""
echo "=== signal journal mirror (if on volume) ==="
for p in \
  /var/lib/docker/volumes/gmail-agent-signal-runtime/_data/drive_signals.jsonl \
  /app/tools/gmail_audit/data/mailbox_memory/signal_runtime/drive_signals.jsonl; do
  if [ -f "$p" ]; then
    echo "file: $p lines=$(wc -l < "$p")"
    tail -n 2 "$p" | head -c 400
    echo ""
  fi
done
docker compose --env-file .env.vps -f docker-compose.vps.yml run --rm --no-deps gmail-agent-worker \
  sh -c 'test -f /app/tools/gmail_audit/data/mailbox_memory/signal_runtime/drive_signals.jsonl && wc -l /app/tools/gmail_audit/data/mailbox_memory/signal_runtime/drive_signals.jsonl || echo "(no drive_signals.jsonl in worker volume)"' 2>/dev/null || true

echo ""
echo "=== worker logs: drive-ingest / drive_poll (last 30 hits) ==="
docker logs gmail-agent-vps-gmail-agent-worker-1 2>&1 | grep -iE 'drive[-_ ]?(ingest|poll|detect|signal)|drive_ingest' | tail -30 || echo "(no matches)"

echo ""
echo "=== drive-detect-changes (bounded poll, cursor only — no full ingest) ==="
docker compose --env-file .env.vps -f docker-compose.vps.yml run --rm --no-deps gmail-agent-worker \
  python tools/gmail_audit/gmail_intake.py drive-detect-changes --max-results 5 --verbose 2>&1 | tail -c 2000
