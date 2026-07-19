#!/usr/bin/env bash
# Session 3 Drive: replay May-8 signals (active), SQL snapshot, optional ingest, doctor, change detection.
set -euo pipefail

REPO="${1:-/opt/gmail-agent/current}"
ENV_FILE="${2:-/etc/topinstal/gmail-agent.env}"
PHASE="${3:-replay}" # replay | sql | doctor | enable-change-detection | inventory

cd "$REPO"
COMPOSE=(docker compose --env-file .env.vps -f docker-compose.vps.yml)
WORKER=(
  "${COMPOSE[@]}" run --rm --no-deps
  -v "${REPO}/tools/gmail_audit:/app/tools/gmail_audit:ro"
  -w /app/tools/gmail_audit
  gmail-agent-worker
)
PSQL=(docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory)

_run_worker() {
  env SIGNAL_RUNTIME_MODE="${SIGNAL_RUNTIME_MODE:-active}" "${WORKER[@]}" "$@"
}

_sql_counts() {
  echo "=== company_drive_documents ==="
  "${PSQL[@]}" -t -A -c "SELECT COUNT(*)::text FROM company_drive_documents;"
  echo "=== company_drive_document_chunks by embedding_status ==="
  "${PSQL[@]}" -c "SELECT embedding_status, COUNT(*)::bigint FROM company_drive_document_chunks GROUP BY 1 ORDER BY 2 DESC;"
  echo "=== target files chunk check ==="
  "${PSQL[@]}" -c "
    SELECT d.file_name, COUNT(c.chunk_id) AS chunks,
           COUNT(*) FILTER (WHERE c.embedding_status = 'ready') AS ready_chunks
    FROM company_drive_documents d
    LEFT JOIN company_drive_document_chunks c ON c.document_id = d.document_id
    WHERE d.file_name ILIKE '%Zestawienie HP%'
       OR d.file_name ILIKE '%Projekt techniczny skan%'
    GROUP BY d.file_name
    ORDER BY d.file_name;"
}

case "$PHASE" in
  replay)
    cp -a "$ENV_FILE" "${ENV_FILE}.bak.session3.$(date +%Y%m%d%H%M%S)"
    if grep -q '^SIGNAL_RUNTIME_MODE=' "$ENV_FILE"; then
      sed -i 's/^SIGNAL_RUNTIME_MODE=.*/SIGNAL_RUNTIME_MODE=active/' "$ENV_FILE"
    else
      echo 'SIGNAL_RUNTIME_MODE=active' >> "$ENV_FILE"
    fi
    grep '^SIGNAL_RUNTIME_MODE=' "$ENV_FILE"
  _run_worker env PYTHONPATH=/app/tools/gmail_audit DRIVE_REPLAY_DATE=2026-05-08 \
      python scripts/vps_reingest_drive_from_jsonl.py | tee /tmp/drive_reingest_jsonl.json
    _run_worker env PYTHONPATH=/app/tools/gmail_audit \
      python scripts/vps_materialize_drive_chunks.py | tee /tmp/drive_materialize_chunks.json
    _sql_counts
    ;;
  sql)
    _sql_counts
    ;;
  doctor)
    _run_worker python gmail_intake.py doctor --skip-gmail --verbose | tee /tmp/doctor.json
    ;;
  enable-change-detection)
    cp -a "$ENV_FILE" "${ENV_FILE}.bak.drive-change.$(date +%Y%m%d%H%M%S)"
    if grep -q '^DRIVE_CHANGE_DETECTION_ENABLED=' "$ENV_FILE"; then
      sed -i 's/^DRIVE_CHANGE_DETECTION_ENABLED=.*/DRIVE_CHANGE_DETECTION_ENABLED=1/' "$ENV_FILE"
    else
      echo 'DRIVE_CHANGE_DETECTION_ENABLED=1' >> "$ENV_FILE"
    fi
    grep '^DRIVE_CHANGE_DETECTION_ENABLED=' "$ENV_FILE"
    "${COMPOSE[@]}" --profile worker up -d --force-recreate gmail-agent-worker
    docker ps --filter name=gmail-agent-worker --format 'table {{.Names}}\t{{.Status}}'
    ;;
  inventory)
    _run_worker env PYTHONPATH=/app/tools/gmail_audit \
      python scripts/drive_ingest_inventory.py --limit 25 | tee /tmp/drive_ingest_inventory.json
    ;;
  ingest)
    _run_worker python gmail_intake.py drive-ingest --limit 25 --verbose | tee /tmp/drive_ingest_active.json
    _sql_counts
    ;;
  *)
    echo "Unknown phase: $PHASE" >&2
    exit 1
    ;;
esac
