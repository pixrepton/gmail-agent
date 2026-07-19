#!/usr/bin/env python3
"""Apply worker_heartbeat DDL (P1-ID-4) — idempotent schema for signal_worker checkpoint."""

from __future__ import annotations

import argparse
import json
import os
import sys

WORKER_HEARTBEAT_DDL = """
CREATE TABLE IF NOT EXISTS worker_heartbeat (
    worker_id TEXT PRIMARY KEY,
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    iteration_count INT NOT NULL DEFAULT 0,
    last_error TEXT,
    loop_mode TEXT,
    last_message_id TEXT,
    last_replayed_signal_id TEXT
);

ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS iteration_count INT NOT NULL DEFAULT 0;
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS loop_mode TEXT;
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS last_message_id TEXT;
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS last_replayed_signal_id TEXT;
"""


def _db_url() -> str:
    return str(os.environ.get("MAILBOX_MEMORY_DATABASE_URL") or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply worker_heartbeat table DDL.")
    parser.add_argument("--apply", action="store_true", help="Execute DDL (default: dry-run).")
    args = parser.parse_args()

    db_url = _db_url()
    if not db_url:
        print(json.dumps({"ok": False, "dry_run": not args.apply, "note": "no_database"}, ensure_ascii=False))
        return 0 if not args.apply else 1

    import psycopg

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'worker_heartbeat'
                )
                """
            )
            existed_before = bool(cur.fetchone()[0])
            if args.apply:
                cur.execute(WORKER_HEARTBEAT_DDL)
            conn.commit()

    result = {
        "ok": True,
        "dry_run": not args.apply,
        "table_existed_before": existed_before,
        "applied": bool(args.apply),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
