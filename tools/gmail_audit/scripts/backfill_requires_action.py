#!/usr/bin/env python3
"""Backfill requires_action metadata for legacy mailbox_memory_cases rows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_routing import classify_mailbox_row, desk_eligible


def _infer_requires_action(row: dict) -> bool:
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    if "requires_action" in meta:
        return bool(meta.get("requires_action"))
    export_type = str(meta.get("export_case_type") or "").strip()
    source_kind = str(meta.get("source_kind") or "gmail_inbound").strip()
    routing = classify_mailbox_row(
        str(row.get("case_family") or ""),
        source_kind,
        export_type or "other",
        str(meta.get("orchestrator_status") or "") or None,
    )
    return routing.requires_action


def backfill_requires_action(
    *,
    mailbox_store,
    dry_run: bool = False,
    limit: int = 5000,
) -> dict:
    fetch_cases = getattr(mailbox_store, "fetch_cases", None)
    if not callable(fetch_cases):
        return {"ok": False, "error": "mailbox_store.fetch_cases required"}

    updated = 0
    scanned = 0
    for row in fetch_cases(limit=max(1, int(limit))) or []:
        if not isinstance(row, dict):
            continue
        scanned += 1
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        if "requires_action" in meta:
            continue
        inferred = _infer_requires_action(row)
        meta = dict(meta)
        meta["requires_action"] = inferred
        if not dry_run:
            upsert = getattr(mailbox_store, "upsert_case", None)
            if callable(upsert):
                upsert({**row, "metadata": meta})
        updated += 1

    return {"ok": True, "scanned": scanned, "updated": updated, "dry_run": dry_run}


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill requires_action in mailbox case metadata.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()

    from config import load_settings
    from mailbox_memory_store import PostgresMailboxMemoryStore

    settings = load_settings(require_groq=False, require_google=False)
    db_url = str(getattr(settings, "mailbox_memory_database_url", "") or "").strip()
    if not db_url:
        print(json.dumps({"ok": False, "error": "mailbox_memory_database_url not configured"}))
        return 1
    store = PostgresMailboxMemoryStore(db_url)
    result = backfill_requires_action(mailbox_store=store, dry_run=args.dry_run, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
