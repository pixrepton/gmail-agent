"""Local reconciliation for illegal dual-active mailbox_memory_facts identities.

Keeps newest active row per (case_id, entity_scope, fact_key) EXCEPT when multiple
distinct values share the same message_id (legal same-message conflict).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory.postgres import PostgresMailboxMemoryStore, _json_dump


CANDIDATE_URLS = [
    os.environ.get("MAILBOX_MEMORY_DATABASE_URL"),
    "postgresql://mailbox_memory:memorka@127.0.0.1:54129/mailbox_memory",
]


def _url() -> str:
    import psycopg

    for url in CANDIDATE_URLS:
        if not url:
            continue
        try:
            with psycopg.connect(url, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            return url
        except Exception:
            continue
    raise SystemExit("Postgres unavailable")


def main(*, apply: bool) -> None:
    store = PostgresMailboxMemoryStore(database_url=_url())
    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apply": apply,
        "groups_scanned": 0,
        "legal_conflicts_left": 0,
        "illegal_groups_fixed": 0,
        "rows_superseded": 0,
        "samples": [],
    }
    with store._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT case_id, entity_scope, fact_key
                FROM mailbox_memory_facts
                WHERE status = 'active'
                GROUP BY case_id, entity_scope, fact_key
                HAVING COUNT(*) > 1
                """
            )
            groups = cur.fetchall() or []
            report["groups_scanned"] = len(groups)
            for group in groups:
                if isinstance(group, dict):
                    case_id, entity_scope, fact_key = group["case_id"], group["entity_scope"], group["fact_key"]
                else:
                    case_id, entity_scope, fact_key = group[0], group[1], group[2]
                cur.execute(
                    """
                    SELECT fact_id, message_id, normalized_value, observed_at, metadata
                    FROM mailbox_memory_facts
                    WHERE case_id = %s AND entity_scope = %s AND fact_key = %s AND status = 'active'
                    ORDER BY observed_at DESC NULLS LAST, fact_id DESC
                    """,
                    (case_id, entity_scope, fact_key),
                )
                rows = cur.fetchall() or []
                parsed = []
                for row in rows:
                    if isinstance(row, dict):
                        parsed.append(row)
                    else:
                        parsed.append(
                            {
                                "fact_id": row[0],
                                "message_id": row[1],
                                "normalized_value": row[2],
                                "observed_at": row[3],
                                "metadata": row[4] if isinstance(row[4], dict) else {},
                            }
                        )
                by_message: dict[str, set[str]] = {}
                for item in parsed:
                    mid = str(item.get("message_id") or "")
                    by_message.setdefault(mid, set()).add(str(item.get("normalized_value") or "").strip())
                legal = any(len(values) > 1 for values in by_message.values())
                if legal and len(parsed) == sum(len(v) for v in by_message.values()):
                    # Pure same-message multi-value conflict(s); leave untouched.
                    report["legal_conflicts_left"] += 1
                    continue
                winner = parsed[0]
                loser_ids = [str(item["fact_id"]) for item in parsed[1:]]
                # If legal same-message conflict exists, keep all rows for that message_id.
                legal_mids = {mid for mid, values in by_message.items() if len(values) > 1}
                if legal_mids:
                    report["legal_conflicts_left"] += 1
                    loser_ids = [
                        str(item["fact_id"])
                        for item in parsed[1:]
                        if str(item.get("message_id") or "") not in legal_mids
                    ]
                    if not loser_ids:
                        continue
                sample = {
                    "case_id": str(case_id or "")[:16],
                    "entity_scope": entity_scope,
                    "fact_key": fact_key,
                    "active_before": len(parsed),
                    "supersede_count": len(loser_ids),
                    "winner_fact_id": str(winner["fact_id"])[:20],
                }
                if len(report["samples"]) < 10:
                    report["samples"].append(sample)
                if not apply:
                    report["illegal_groups_fixed"] += 1
                    report["rows_superseded"] += len(loser_ids)
                    continue
                for fact_id in loser_ids:
                    item = next(x for x in parsed if str(x["fact_id"]) == fact_id)
                    meta = dict(item.get("metadata") or {})
                    obs = winner.get("observed_at")
                    if hasattr(obs, "isoformat"):
                        obs = obs.isoformat()
                    meta["superseded_at"] = obs
                    meta["superseded_by_fact_id"] = str(winner["fact_id"])
                    meta["supersede_reason"] = "fact_supersession_write_01_reconcile"
                    cur.execute(
                        """
                        UPDATE mailbox_memory_facts
                        SET status = 'superseded', metadata = %s::jsonb
                        WHERE fact_id = %s AND status = 'active'
                        """,
                        (_json_dump(meta), fact_id),
                    )
                    report["rows_superseded"] += int(cur.rowcount or 0)
                report["illegal_groups_fixed"] += 1
            if apply:
                conn.commit()
            else:
                conn.rollback()
    out = Path(os.environ.get("TOP_CODE_SESSION_SCRATCH", r"C:\top-code-session-scratch")) / "fact-write-01-reconcile.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"report={out}")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    main(apply=apply)
