#!/usr/bin/env python3
"""Audit correlation_links mailbox_case targets without matching mailbox_memory_cases."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1] / "tools" / "gmail_audit"
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))


def _bad_links_postgres(db_url: str) -> list[dict[str, str]]:
    import psycopg

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cl.engagement_id, cl.target_id, cl.source_repo
                FROM correlation_links cl
                WHERE cl.link_type = 'mailbox_case'
                  AND NOT EXISTS (
                    SELECT 1 FROM mailbox_memory_cases c
                    WHERE c.case_id = cl.target_id
                  )
                ORDER BY cl.target_id
                """
            )
            rows = cur.fetchall() or []
    return [
        {
            "engagement_id": str(r[0] or ""),
            "case_id": str(r[1] or ""),
            "source_repo": str(r[2] or ""),
        }
        for r in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit orphan mailbox_case correlation links.")
    parser.add_argument("--fix", action="store_true", help="Delete orphan mailbox_case links (default: dry-run)")
    args = parser.parse_args()

    db_url = str(os.environ.get("MAILBOX_MEMORY_DATABASE_URL") or "").strip()
    if not db_url:
        print("MAILBOX_MEMORY_DATABASE_URL is not set.", file=sys.stderr)
        return 2

    bad = _bad_links_postgres(db_url)
    print(f"orphan_mailbox_case_links={len(bad)}")
    for row in bad[:50]:
        print(f"  engagement={row['engagement_id']} case_id={row['case_id']} repo={row['source_repo']}")

    if not bad:
        return 0
    if not args.fix:
        print("dry-run: pass --fix to delete orphan links")
        return 1

    import psycopg

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            for row in bad:
                cur.execute(
                    """
                    DELETE FROM correlation_links
                    WHERE link_type = 'mailbox_case'
                      AND target_id = %s
                      AND engagement_id = %s
                    """,
                    (row["case_id"], row["engagement_id"]),
                )
        conn.commit()
    print(f"deleted={len(bad)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
