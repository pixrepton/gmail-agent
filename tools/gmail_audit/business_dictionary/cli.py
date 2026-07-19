"""CLI entry points for Business Dictionary operations."""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from business_dictionary.extractor import extract_terms_from_text
from business_dictionary.store import (
    ensure_dictionary_table,
    upsert_term,
    search_terms,
    get_stats,
    delete_term,
)
from business_dictionary.graph_store import upsert_term_node, get_graph_stats


def run_extract_cli(args: Any) -> int:
    """Extract business terms from a text file or stdin."""
    text = ""
    if args.text:
        text = args.text
    elif args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            text = f.read()
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        print("Provide --text, --file, or pipe text to stdin.", file=sys.stderr)
        return 1

    from config import load_settings

    settings = load_settings(require_groq=False, require_google=False)

    def _llm_call(messages, **kwargs):
        from agent_runtime.openai_agent_client import call_llm
        return call_llm(settings, messages=messages, **kwargs)

    terms = extract_terms_from_text(
        text,
        source_document=args.source or "",
        source_kind=args.source_kind or "cli",
        llm_call=_llm_call if not args.dry_run else None,
    )

    print(json.dumps([t.__dict__ for t in terms], ensure_ascii=False, indent=2))

    if not args.dry_run and terms:
        db_url = str(
            getattr(settings, "mailbox_memory_database_url", "")
            or os.environ.get("MAILBOX_MEMORY_DATABASE_URL", "")
        )
        if db_url:
            try:
                import psycopg
                conn = psycopg.connect(db_url)
                ensure_dictionary_table(conn)
                for term in terms:
                    upsert_term(conn, term)
                    if args.neo4j:
                        upsert_term_node(settings, term)
                conn.close()
                print(f"[OK] {len(terms)} terms stored in DB (neo4j={args.neo4j})", file=sys.stderr)
            except Exception as exc:
                print(f"[WARN] Storage failed: {exc}", file=sys.stderr)
        else:
            print("[WARN] No DB_URL — terms extracted but not persisted.", file=sys.stderr)

    return 0


def run_search_cli(args: Any) -> int:
    """Search business terms."""
    from config import load_settings
    settings = load_settings(require_groq=False, require_google=False)
    db_url = str(
        getattr(settings, "mailbox_memory_database_url", "")
        or os.environ.get("MAILBOX_MEMORY_DATABASE_URL", "")
    )
    if not db_url:
        print("Database not configured.", file=sys.stderr)
        return 1

    try:
        import psycopg
        conn = psycopg.connect(db_url)
        ensure_dictionary_table(conn)

        if args.stats:
            stats = get_stats(conn)
            print(json.dumps(stats.__dict__, ensure_ascii=False, indent=2))
        elif args.delete:
            ok = delete_term(conn, args.delete)
            print(f"[{'OK' if ok else 'FAIL'}] Deleted term_id={args.delete}", file=sys.stderr)
        else:
            items = search_terms(conn, query=args.query or "", category=args.category or "", limit=int(args.limit or 50))
            print(json.dumps(items, ensure_ascii=False, indent=2))

        conn.close()
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    return 0


def run_sync_cli(args: Any) -> int:
    """Sync existing Drive documents and engagements into business dictionary."""
    from config import load_settings
    settings = load_settings(require_groq=False, require_google=False)
    db_url = str(
        getattr(settings, "mailbox_memory_database_url", "")
        or os.environ.get("MAILBOX_MEMORY_DATABASE_URL", "")
    )
    if not db_url:
        print("Database not configured.", file=sys.stderr)
        return 1

    try:
        import psycopg
        conn = psycopg.connect(db_url)
        ensure_dictionary_table(conn)

        # Get all Drive documents with extracted text
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.drive_file_id, d.file_name, d.mime_type,
                       dc.chunk_text, d.case_id
                FROM drive_documents d
                JOIN drive_document_chunks dc ON dc.drive_file_id = d.drive_file_id
                WHERE dc.chunk_text IS NOT NULL AND dc.chunk_text != ''
                LIMIT %s
                """,
                (max(1, int(args.limit or 100)),),
            )
            docs = cur.fetchall() or []

        if not docs:
            print("No Drive documents with text found.", file=sys.stderr)
            conn.close()
            return 0

        def _llm_call(messages, **kwargs):
            from agent_runtime.openai_agent_client import call_llm
            return call_llm(settings, messages=messages, **kwargs)

        total = 0
        for doc in docs:
            file_id = doc[0] if not isinstance(doc, dict) else doc.get("drive_file_id")
            file_name = doc[1] if not isinstance(doc, dict) else doc.get("file_name", "")
            chunk_text = doc[3] if not isinstance(doc, dict) else doc.get("chunk_text", "")

            terms = extract_terms_from_text(
                chunk_text,
                source_document=f"drive:{file_name}/{file_id}",
                source_kind="drive",
                llm_call=_llm_call if not args.dry_run else None,
            )
            for term in terms:
                upsert_term(conn, term)
                if args.neo4j:
                    upsert_term_node(settings, term)
                total += 1

            print(f"  [{file_name}] {len(terms)} terms", file=sys.stderr)

        if not args.dry_run:
            conn.commit()
        conn.close()

        print(f"\n[OK] {total} terms synced from {len(docs)} Drive documents" +
              f" (neo4j={'yes' if args.neo4j else 'no'})", file=sys.stderr)

    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    return 0


def run_outbox_process_cli(args: Any) -> int:
    """Process pending sync_outbox entries — replicate PG terms to Neo4j."""
    from config import load_settings
    settings = load_settings(require_groq=False, require_google=False)
    db_url = str(
        getattr(settings, "mailbox_memory_database_url", "")
        or os.environ.get("MAILBOX_MEMORY_DATABASE_URL", "")
    )
    if not db_url:
        print("Database not configured.", file=sys.stderr)
        return 1

    try:
        import psycopg
        from business_dictionary.graph_store import process_outbox
        from business_dictionary.store import ensure_sync_outbox_table

        conn = psycopg.connect(db_url)
        ensure_sync_outbox_table(conn)
        stats = process_outbox(conn, settings, limit=int(args.limit or 50), dry_run=bool(args.dry_run))
        conn.close()
        print(f"[OK] Outbox processed: {stats}", file=sys.stderr)
        if args.dry_run:
            print(f"[DRY RUN] Would process {stats['skipped']} entries. Run without --dry-run to execute.", file=sys.stderr)
    except Exception as exc:
        print(f"[ERROR] Outbox processing failed: {exc}", file=sys.stderr)
        return 1

    return 0
