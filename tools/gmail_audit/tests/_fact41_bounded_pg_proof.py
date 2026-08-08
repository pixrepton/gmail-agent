"""Bounded local Postgres proof for FACT-4.1-HIGH-01."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from calendar_runtime import CalendarRuntime
from mailbox_memory.active_facts import fetch_current_facts_for_case
from mailbox_memory.postgres import PostgresMailboxMemoryStore

DSN_CANDIDATES = [
    os.environ.get("MAILBOX_MEMORY_DSN"),
    os.environ.get("DATABASE_URL"),
    "postgresql://postgres:postgres@127.0.0.1:54129/mailbox_memory",
    "postgresql://mailbox_memory:mailbox_memory@127.0.0.1:54129/mailbox_memory",
    "postgresql://mailbox:mailbox@127.0.0.1:54129/mailbox_memory",
]


def _connect() -> PostgresMailboxMemoryStore:
    last: Exception | None = None
    for dsn in DSN_CANDIDATES:
        if not dsn:
            continue
        try:
            store = PostgresMailboxMemoryStore(dsn)
            store.bootstrap()
            store.fetch_facts_for_case("__probe__")
            print("CONNECTED", dsn.split("@")[-1])
            return store
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise SystemExit(f"NO_PG: {last}")


def _upsert(store: PostgresMailboxMemoryStore, case_id: str) -> None:
    store.upsert_case(
        {
            "case_id": case_id,
            "case_key": case_id.upper(),
            "case_family": "lead_opportunity",
            "mailbox": "test",
            "subject": "proof",
            "status": "open",
            "customer_name": "",
            "customer_email": "",
            "metadata": {},
        }
    )
    with store._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mailbox_memory_facts WHERE case_id=%s", (case_id,))
        conn.commit()


def main() -> None:
    store = _connect()
    cid = "case_fact41_high_proof"
    _upsert(store, cid)
    store.append_facts_with_supersession(
        [
            {
                "fact_id": "p_old",
                "case_id": cid,
                "message_id": "m1",
                "document_id": "",
                "entity_scope": "case",
                "fact_key": "proposed_visit",
                "normalized_value": "2026-07-01",
                "raw_value": "2026-07-01",
                "confidence": 0.9,
                "observed_at": "2026-07-01T08:00:00Z",
                "source_type": "llm",
                "source_ref": "msg:m1",
                "status": "active",
                "metadata": {},
            }
        ]
    )
    store.append_facts_with_supersession(
        [
            {
                "fact_id": "p_new",
                "case_id": cid,
                "message_id": "m2",
                "document_id": "",
                "entity_scope": "case",
                "fact_key": "proposed_visit",
                "normalized_value": "2026-08-20",
                "raw_value": "2026-08-20",
                "confidence": 0.8,
                "observed_at": "2026-08-08T08:00:00Z",
                "source_type": "llm",
                "source_ref": "msg:m2",
                "status": "active",
                "metadata": {},
            }
        ]
    )
    hist = store.fetch_facts_for_case(cid)
    active = store.fetch_active_facts_for_case(cid)
    current = fetch_current_facts_for_case(store, cid)
    print("HISTORY", [(r["fact_id"], r.get("status"), r.get("normalized_value")) for r in hist])
    print("ACTIVE", [(r["fact_id"], r.get("status"), r.get("normalized_value")) for r in active])
    assert len(hist) >= 2
    assert all(r.get("normalized_value") == "2026-08-20" for r in active)
    assert [r["fact_id"] for r in current] == [r["fact_id"] for r in active]
    ctx = CalendarRuntime(settings=SimpleNamespace(), store=store, client=None).context_for_case(cid)
    print("CALENDAR", ctx["calendar_risk"], ctx["visit_lifecycle"])
    assert ctx["calendar_risk"] == "customer_proposed_date"

    cid2 = "case_fact41_high_proof_super_only"
    _upsert(store, cid2)
    store.append_facts_with_supersession(
        [
            {
                "fact_id": "s_old",
                "case_id": cid2,
                "message_id": "m1",
                "document_id": "",
                "entity_scope": "case",
                "fact_key": "proposed_visit",
                "normalized_value": "2026-07-01",
                "raw_value": "2026-07-01",
                "confidence": 0.9,
                "observed_at": "2026-07-01T08:00:00Z",
                "source_type": "llm",
                "source_ref": "msg:m1",
                "status": "active",
                "metadata": {},
            }
        ]
    )
    with store._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE mailbox_memory_facts SET status=%s WHERE case_id=%s",
                ("superseded", cid2),
            )
        conn.commit()
    ctx2 = CalendarRuntime(settings=SimpleNamespace(), store=store, client=None).context_for_case(cid2)
    print("CALENDAR_SUPER", ctx2["calendar_risk"], ctx2["visit_lifecycle"])
    assert ctx2["calendar_risk"] == "calendar_event_missing"
    print("BOUNDED_PG_PROOF_OK")


if __name__ == "__main__":
    main()
