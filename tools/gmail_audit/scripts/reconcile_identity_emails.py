#!/usr/bin/env python3
"""P1: merge duplicate topinstal_identities rows sharing the same primary_email."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import load_settings
from correlation_registry.identity_email_dedup import ADVISORY_LOCK_KEY, run_email_identity_dedup
from correlation_registry.store import PostgresCorrelationRegistryStore


def _db_url() -> str:
    settings = load_settings(require_groq=False, require_google=False)
    return str(getattr(settings, "mailbox_memory_database_url", "") or os.environ.get("MAILBOX_MEMORY_DATABASE_URL") or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge email-identical duplicate identities.")
    parser.add_argument("--dry-run", action="store_true", help="Report planned merges only (default).")
    parser.add_argument("--apply", action="store_true", help="Execute merges.")
    parser.add_argument("--limit", type=int, default=0, help="Max duplicate groups to process (0 = all).")
    parser.add_argument("--require-zero-after", action="store_true", help="Exit 1 if duplicates remain after apply.")
    args = parser.parse_args()

    dry_run = not args.apply
    db_url = _db_url()
    if not db_url:
        print(json.dumps({"ok": False, "error": "MAILBOX_MEMORY_DATABASE_URL is not set."}), file=sys.stderr)
        return 1

    store = PostgresCorrelationRegistryStore(db_url)
    store.bootstrap()

    import psycopg

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            if not dry_run:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
                locked = bool(cur.fetchone()[0])
                if not locked:
                    print(json.dumps({"ok": False, "error": "advisory_lock_busy"}), file=sys.stderr)
                    return 2
            try:
                result = run_email_identity_dedup(
                    store,
                    dry_run=dry_run,
                    limit=max(0, int(args.limit or 0)),
                    operator_id="reconcile_identity_emails",
                )
            finally:
                if not dry_run:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            conn.commit()

    result["ok"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.require_zero_after and not dry_run and int(result.get("duplicate_groups_after") or 0) > 0:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
