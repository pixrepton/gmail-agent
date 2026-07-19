#!/usr/bin/env python3
"""Migrate legacy internal_task rows to operations + manual metadata (Phase 10)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any


def _db_url() -> str:
    return str(os.environ.get("MAILBOX_MEMORY_DATABASE_URL") or "").strip()


def _fetch_rows(conn: Any) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT case_id, metadata
        FROM mailbox_memory_cases
        WHERE case_family = %s
        ORDER BY created_at ASC
        """,
        ("internal_task",),
    )
    return list(cur.fetchall() or [])


def _migrate_row(meta_raw: Any) -> dict[str, Any]:
    meta = json.loads(meta_raw) if isinstance(meta_raw, str) else dict(meta_raw or {})
    meta.setdefault("requires_action", True)
    meta.setdefault("source_kind", "manual")
    meta["migrated_from_internal_task"] = True
    if not str(meta.get("export_case_type") or "").strip():
        meta["export_case_type"] = "operations"
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate internal_task rows to operations/manual.")
    parser.add_argument("--apply", action="store_true", help="Apply migration (default: dry-run report only).")
    args = parser.parse_args()

    db_url = _db_url()
    if not db_url:
        if not args.apply:
            print(json.dumps({"internal_task_count": 0, "dry_run": True, "note": "no_database"}, ensure_ascii=False))
            return 0
        print("MAILBOX_MEMORY_DATABASE_URL is not set.", file=sys.stderr)
        return 1

    import psycopg

    conn = psycopg.connect(db_url)
    try:
        rows = _fetch_rows(conn)
        print(json.dumps({"internal_task_count": len(rows), "dry_run": not args.apply}, ensure_ascii=False))
        if not rows:
            return 0
        if not args.apply:
            for case_id, _ in rows[:20]:
                print(f"would_migrate: {case_id}")
            if len(rows) > 20:
                print(f"... and {len(rows) - 20} more")
            return 0

        with conn:
            for case_id, meta_raw in rows:
                meta = _migrate_row(meta_raw)
                conn.execute(
                    """
                    UPDATE mailbox_memory_cases
                    SET case_family = %s,
                        metadata = %s::jsonb,
                        updated_at = NOW()
                    WHERE case_id = %s AND case_family = %s
                    """,
                    ("operations", json.dumps(meta, ensure_ascii=False), case_id, "internal_task"),
                )
        print(json.dumps({"migrated": len(rows)}, ensure_ascii=False))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
