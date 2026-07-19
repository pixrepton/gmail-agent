#!/usr/bin/env python3
"""Backfill / reconcile correlation_links (full scan or delta cron)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import load_settings
from correlation_registry.preview import (
    accumulate_plan,
    empty_dry_run_stats,
    plan_mailbox_case_sync,
    plan_workflow_sync,
    print_dry_run_summary,
)
from correlation_registry.orchestrator_backfill import fetch_workflows_from_db
from correlation_registry.service import build_correlation_registry_service


def _load_workflow_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    if path.suffix.lower() == ".jsonl":
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        return rows
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict) and isinstance(data.get("workflows"), list):
        return [row for row in data["workflows"] if isinstance(row, dict)]
    return []


def _sync_cases(
    service: Any,
    *,
    cases: list[dict],
    dry_run: bool,
    stats: dict[str, int] | None = None,
) -> int:
    count = 0
    for row in cases:
        count += 1
        if dry_run and stats is not None:
            plan = plan_mailbox_case_sync(
                service.store,
                case_id=str(row.get("case_id") or ""),
                customer_email=str(row.get("customer_email") or ""),
                thread_id=str(row.get("thread_id") or ""),
                message_id=str(row.get("message_id") or ""),
            )
            accumulate_plan(stats, plan)
            continue
        if dry_run:
            continue
        service.sync_mailbox_case(
            case_id=str(row["case_id"]),
            customer_email=str(row["customer_email"]),
            thread_id=str(row.get("thread_id") or ""),
            message_id=str(row.get("message_id") or ""),
        )
    return count


def _sync_workflows(
    service: Any,
    *,
    workflows: list[dict],
    dry_run: bool,
    stats: dict[str, int] | None = None,
) -> int:
    count = 0
    for wf in workflows:
        count += 1
        if dry_run and stats is not None:
            plan = plan_workflow_sync(
                service.store,
                workflow_id=str(wf.get("id") or wf.get("workflow_id") or ""),
                client_email=str(wf.get("client_email") or ""),
                message_id=str(wf.get("message_id") or ""),
            )
            accumulate_plan(stats, plan)
            continue
        if dry_run:
            continue
        service.sync_cieplo_workflow(
            workflow_id=str(wf.get("id") or wf.get("workflow_id") or ""),
            client_email=str(wf.get("client_email") or ""),
            message_id=str(wf.get("message_id") or ""),
            trace_id=str(wf.get("trace_id") or ""),
            external_key=str(wf.get("external_key") or ""),
        )
    return count


def _fetch_delta_cases(conn: Any, delta_hours: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.case_id, c.customer_email, c.thread_id,
                   (SELECT m.message_id FROM mailbox_memory_messages m
                    WHERE m.case_id = c.case_id
                    ORDER BY m.updated_at DESC NULLS LAST LIMIT 1) AS message_id
            FROM mailbox_memory_cases c
            WHERE c.customer_email <> ''
              AND (
                    c.updated_at >= NOW() - (%s || ' hours')::interval
                 OR EXISTS (
                        SELECT 1 FROM mailbox_memory_messages m
                        WHERE m.case_id = c.case_id
                          AND m.updated_at >= NOW() - (%s || ' hours')::interval
                    )
              )
            """,
            (str(max(1, delta_hours)), str(max(1, delta_hours))),
        )
        return list(cur.fetchall())


def _fetch_delta_links(conn: Any, delta_hours: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT link_type, target_id, source_repo, engagement_id
            FROM correlation_links
            WHERE updated_at >= NOW() - (%s || ' hours')::interval
            """,
            (str(max(1, delta_hours)),),
        )
        return list(cur.fetchall())


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill / reconcile TOP-INSTAL correlation registry (P0).")
    parser.add_argument("--dry-run", action="store_true", help="Plan only; print stats, do not write.")
    parser.add_argument(
        "--cron",
        action="store_true",
        help="Delta reconciliation mode (uses --delta-hours, default 24).",
    )
    parser.add_argument(
        "--delta-hours",
        type=int,
        default=24,
        help="With --cron: only records touched in the last N hours (default 24).",
    )
    parser.add_argument(
        "--workflows-json",
        type=Path,
        default=None,
        help="Optional JSON/JSONL export with id, client_email, message_id, trace_id.",
    )
    parser.add_argument(
        "--from-orchestrator-workflows",
        action="store_true",
        help="Backfill cieplo_workflow links from workflows table (same Postgres as mailbox_memory).",
    )
    args = parser.parse_args()

    if args.cron and args.delta_hours < 1:
        print("--delta-hours must be >= 1 when using --cron", file=sys.stderr)
        return 2

    settings = load_settings(require_groq=False, require_google=False)
    db_url = str(getattr(settings, "mailbox_memory_database_url", "") or "").strip()
    if not db_url:
        print("MAILBOX_MEMORY_DATABASE_URL not configured.", file=sys.stderr)
        return 2

    service = build_correlation_registry_service(db_url)
    if service is None:
        print("Failed to build correlation registry.", file=sys.stderr)
        return 2
    if not args.dry_run:
        service.bootstrap()

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        print("psycopg required.", file=sys.stderr)
        return 2

    workflows_only = (
        args.from_orchestrator_workflows
        and not args.cron
        and not args.workflows_json
    )
    if args.cron:
        mode = "cron_delta"
    elif workflows_only:
        mode = "orchestrator_workflows"
    else:
        mode = "full"
    stats = empty_dry_run_stats() if args.dry_run else None
    link_rows_seen = 0
    case_count = 0
    workflow_count = 0

    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        if not workflows_only:
            if args.cron:
                cases = _fetch_delta_cases(conn, args.delta_hours)
                case_count = _sync_cases(service, cases=cases, dry_run=args.dry_run, stats=stats)
                link_rows_seen = len(_fetch_delta_links(conn, args.delta_hours))
            else:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT case_id, customer_email, thread_id,
                               (SELECT message_id FROM mailbox_memory_messages m
                                WHERE m.case_id = c.case_id
                                ORDER BY m.received_at DESC NULLS LAST LIMIT 1) AS message_id
                        FROM mailbox_memory_cases c
                        WHERE customer_email <> ''
                        """
                    )
                    cases = cur.fetchall()
                case_count = _sync_cases(service, cases=cases, dry_run=args.dry_run, stats=stats)

    if args.from_orchestrator_workflows:
        with psycopg.connect(db_url, row_factory=dict_row) as orch_conn:
            workflow_rows = fetch_workflows_from_db(orch_conn)
        workflow_payloads = [
            {
                "id": row["workflow_id"],
                "client_email": row["client_email"],
                "message_id": row["message_id"],
                "trace_id": row["trace_id"],
                "external_key": row.get("external_key") or "",
            }
            for row in workflow_rows
        ]
        workflow_count += _sync_workflows(
            service,
            workflows=workflow_payloads,
            dry_run=args.dry_run,
            stats=stats,
        )

    if args.workflows_json:
        workflows = _load_workflow_rows(args.workflows_json)
        workflow_count += _sync_workflows(
            service,
            workflows=workflows,
            dry_run=args.dry_run,
            stats=stats,
        )

    if stats is not None:
        stats["cases_seen"] = case_count
        stats["workflows_seen"] = workflow_count
        print_dry_run_summary(
            stats,
            mode=mode,
            delta_hours=args.delta_hours if args.cron else None,
        )
        payload: dict[str, Any] = {
            "mode": mode,
            "dry_run": True,
            "delta_hours": args.delta_hours if args.cron else None,
            **stats,
        }
        if args.cron:
            payload["correlation_links_touched"] = link_rows_seen
    else:
        payload = {
            "mode": mode,
            "dry_run": False,
            "delta_hours": args.delta_hours if args.cron else None,
            "cases_synced": case_count,
            "workflows_synced": workflow_count,
            "correlation_links_touched": link_rows_seen if args.cron else None,
        }

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
