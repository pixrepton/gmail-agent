#!/usr/bin/env python3
"""Backfill P2.0 identity_kind + property_anchor on existing registry rows."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent / "tools" / "gmail_audit"
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from correlation_registry.identity_metadata import (
    BINDING_LEVEL_TECHNICAL,
    merge_engagement_metadata,
    merge_identity_metadata,
)


def _db_url() -> str:
    return str(os.environ.get("MAILBOX_MEMORY_DATABASE_URL") or "").strip()


def _needs_identity_backfill(meta: object) -> bool:
    if not isinstance(meta, dict):
        return True
    return not str(meta.get("identity_kind") or "").strip()


def _needs_engagement_backfill(meta: object) -> bool:
    if not isinstance(meta, dict):
        return True
    return meta.get("binding_level_applied") is None


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill customer identity metadata (P2.0).")
    parser.add_argument("--apply", action="store_true", help="Write updates (default: dry-run counts).")
    args = parser.parse_args()

    db_url = _db_url()
    if not db_url:
        print(json.dumps({"ok": False, "error": "MAILBOX_MEMORY_DATABASE_URL is not set."}))
        return 1

    import psycopg
    from psycopg.rows import dict_row

    identity_candidates = 0
    engagement_candidates = 0
    identities_updated = 0
    engagements_updated = 0

    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT identity_id, primary_email, display_name, metadata FROM topinstal_identities"
            )
            identity_rows = list(cur.fetchall() or [])
            for row in identity_rows:
                meta = row.get("metadata")
                if isinstance(meta, str):
                    meta = json.loads(meta)
                if not _needs_identity_backfill(meta):
                    continue
                identity_candidates += 1
                if not args.apply:
                    continue
                merged = merge_identity_metadata(
                    meta if isinstance(meta, dict) else {},
                    email=str(row.get("primary_email") or ""),
                    display_name=str(row.get("display_name") or ""),
                )
                cur.execute(
                    """
                    UPDATE topinstal_identities
                    SET metadata = %s::jsonb, updated_at = NOW()
                    WHERE identity_id = %s
                    """,
                    (json.dumps(merged, ensure_ascii=False), row["identity_id"]),
                )
                identities_updated += int(cur.rowcount or 0)

            cur.execute(
                """
                SELECT e.engagement_id, e.metadata,
                       (
                         SELECT target_id FROM correlation_links cl
                         WHERE cl.engagement_id = e.engagement_id
                           AND cl.link_type = 'mailbox_case'
                         ORDER BY cl.updated_at DESC
                         LIMIT 1
                       ) AS case_id
                FROM topinstal_engagements e
                """
            )
            engagement_rows = list(cur.fetchall() or [])
            for row in engagement_rows:
                meta = row.get("metadata")
                if isinstance(meta, str):
                    meta = json.loads(meta)
                case_id = str(row.get("case_id") or "").strip()
                hints: dict[str, object] = {}
                if case_id:
                    anchor = meta.get("property_anchor") if isinstance(meta, dict) else {}
                    if not isinstance(anchor, dict) or not str(anchor.get("investment_key") or "").strip():
                        hints["property_anchor"] = {"investment_key": case_id}
                if not _needs_engagement_backfill(meta) and not hints:
                    continue
                engagement_candidates += 1
                if not args.apply:
                    continue
                merged = merge_engagement_metadata(
                    meta if isinstance(meta, dict) else {},
                    hints=hints or None,
                    binding_level=BINDING_LEVEL_TECHNICAL,
                )
                cur.execute(
                    """
                    UPDATE topinstal_engagements
                    SET metadata = %s::jsonb, updated_at = NOW()
                    WHERE engagement_id = %s
                    """,
                    (json.dumps(merged, ensure_ascii=False), row["engagement_id"]),
                )
                engagements_updated += int(cur.rowcount or 0)
        if args.apply:
            conn.commit()

    result = {
        "ok": True,
        "dry_run": not args.apply,
        "identity_candidates": identity_candidates,
        "engagement_candidates": engagement_candidates,
        "identities_updated": identities_updated,
        "engagements_updated": engagements_updated,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
