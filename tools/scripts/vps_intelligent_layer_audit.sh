#!/usr/bin/env bash
# Read-only VPS audit: mailbox memory env, doctor, embedding counts, case-context probe.
set -euo pipefail
cd /opt/gmail-agent/current

echo "=== ENV (safe keys) ==="
grep -E '^(MAILBOX_MEMORY_STAGE_MODE|MAILBOX_MEMORY_VECTOR_ENABLED|OPENAI_COMPAT_EMBEDDING_MODEL|OPENAI_COMPAT_EMBEDDING_DIMENSIONS|OPENAI_COMPAT_EMBEDDING_BASE_URL|NEO4J_PILOT_ENABLED|SIGNAL_RUNTIME_MODE)=' /etc/topinstal/gmail-agent.env 2>/dev/null || true
if grep -q '^MAILBOX_MEMORY_DATABASE_URL=.' /etc/topinstal/gmail-agent.env 2>/dev/null; then
  echo "MAILBOX_MEMORY_DATABASE_URL=set"
else
  echo "MAILBOX_MEMORY_DATABASE_URL=missing"
fi

echo ""
echo "=== PGVECTOR EXTENSION ==="
docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -t -A -c "SELECT extname FROM pg_extension WHERE extname = 'vector';"

echo ""
echo "=== EMBEDDING STATUS (mailbox_memory_document_chunks) ==="
docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -t -A -c \
  "SELECT embedding_status, COUNT(*)::text FROM mailbox_memory_document_chunks GROUP BY 1 ORDER BY 2 DESC;"

echo ""
echo "=== EMBEDDING STATUS (company_drive_document_chunks) ==="
docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -t -A -c \
  "SELECT embedding_status, COUNT(*)::text FROM company_drive_document_chunks GROUP BY 1 ORDER BY 2 DESC;" 2>/dev/null || echo "(table missing or empty)"

echo ""
echo "=== CHUNKS WITH NON-NULL embedding (both tables) ==="
docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -t -A -c \
  "SELECT 'mailbox' AS src, COUNT(*)::text FROM mailbox_memory_document_chunks WHERE embedding IS NOT NULL
   UNION ALL SELECT 'drive', COUNT(*)::text FROM company_drive_document_chunks WHERE embedding IS NOT NULL;"

echo ""
echo "=== RECENT CASES ==="
docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -t -A -c \
  "SELECT case_id, updated_at::text FROM mailbox_memory_cases ORDER BY updated_at DESC NULLS LAST LIMIT 3;"

CASE_ID="$(docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -t -A -c \
  "SELECT c.case_id FROM mailbox_memory_cases c
   JOIN mailbox_memory_document_chunks ch ON ch.case_id = c.case_id
   WHERE ch.embedding_status = 'ready'
   GROUP BY c.case_id ORDER BY COUNT(*) DESC LIMIT 1;" | tr -d '[:space:]')"

echo ""
echo "=== TOP fact_key (mailbox_memory_facts) ==="
docker exec gmail-agent-mailbox-memory psql -U mailbox_memory -d mailbox_memory -t -A -c \
  "SELECT fact_key, COUNT(*)::text FROM mailbox_memory_facts GROUP BY 1 ORDER BY 2 DESC LIMIT 10;"

echo ""
echo "=== CASE-CONTEXT PROBE case_id=${CASE_ID} ==="
if [ -n "${CASE_ID}" ]; then
  docker compose --env-file .env.vps -f docker-compose.vps.yml run --rm --no-deps gmail-agent-worker \
    python tools/gmail_audit/gmail_intake.py case-context \
    --case-id "${CASE_ID}" \
    --query-text "wycena pompa ciepla instalacja" 2>/dev/null | python3 -c "
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception as e:
    print('JSON parse failed:', e)
    print(raw[:2000])
    raise SystemExit(0)
vr = d.get('vector_retrieval') or {}
print('vector_retrieval:', json.dumps(vr, ensure_ascii=False)[:500])
chunks = d.get('relevant_chunks') or []
print('relevant_chunks_count:', len(chunks))
for i, ch in enumerate(chunks[:3]):
    sig = ch.get('retrieval_signals') or {}
    print(f'  chunk[{i}] used_vector={sig.get(\"used_vector\")} mode={sig.get(\"retrieval_mode\")} vector_score={sig.get(\"vector_score\")} fallback={sig.get(\"fallback_reason\",\"\")}')
"
else
  echo "No case_id found"
fi
