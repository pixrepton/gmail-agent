"""Bounded Postgres proof for FACT-SUPERSESSION-WRITE-01."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory.postgres import PostgresMailboxMemoryStore


CANDIDATE_URLS = [
    os.environ.get("MAILBOX_MEMORY_DATABASE_URL"),
    os.environ.get("DATABASE_URL"),
    "postgresql://mailbox_memory:memorka@127.0.0.1:54129/mailbox_memory",
    "postgresql://postgres:postgres@127.0.0.1:54129/mailbox_memory",
    "postgresql://mailbox_memory:mailbox_memory@127.0.0.1:54129/mailbox_memory",
]


def _connect_url() -> str:
    import psycopg

    errors: list[str] = []
    for url in CANDIDATE_URLS:
        if not url:
            continue
        try:
            with psycopg.connect(url, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            return url
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
    raise SystemExit("No local Postgres available:\n" + "\n".join(errors))


def _row(*, case_id: str, message_id: str, fact_id: str, value: str, observed_at: str) -> dict:
    return {
        "fact_id": fact_id,
        "case_id": case_id,
        "message_id": message_id,
        "document_id": "",
        "entity_scope": "customer",
        "fact_key": "customer_phone",
        "normalized_value": value,
        "raw_value": value,
        "confidence": 0.9,
        "observed_at": observed_at,
        "source_type": "message",
        "source_ref": message_id,
        "status": "active",
        "metadata": {},
    }


def _active(store: PostgresMailboxMemoryStore, case_id: str) -> list[dict]:
    return [
        item
        for item in store.fetch_active_facts_for_case(case_id)
        if str(item.get("fact_key")) == "customer_phone"
    ]


def main() -> None:
    url = _connect_url()
    store = PostgresMailboxMemoryStore(database_url=url)
    suffix = uuid.uuid4().hex[:8]
    case_a = f"fact_write_a_{suffix}"
    case_b = f"fact_write_b_{suffix}"
    try:
        store.replace_message_facts(
            message_id=f"m1_{suffix}",
            rows=[_row(case_id=case_a, message_id=f"m1_{suffix}", fact_id=f"f1_{suffix}", value="111", observed_at="2026-08-08T10:00:00Z")],
        )
        assert [x["normalized_value"] for x in _active(store, case_a)] == ["111"]

        store.replace_message_facts(
            message_id=f"m2_{suffix}",
            rows=[_row(case_id=case_a, message_id=f"m2_{suffix}", fact_id=f"f2_{suffix}", value="222", observed_at="2026-08-08T11:00:00Z")],
        )
        assert [x["normalized_value"] for x in _active(store, case_a)] == ["222"]
        hist = [
            item
            for item in store.fetch_facts_for_case(case_a)
            if str(item.get("fact_key")) == "customer_phone"
        ]
        assert any(str(item.get("status")) == "superseded" and str(item.get("normalized_value")) == "111" for item in hist)

        # Re-ingest same source snapshot: current stays 222 for other message; re-assert m2.
        store.replace_message_facts(
            message_id=f"m2_{suffix}",
            rows=[_row(case_id=case_a, message_id=f"m2_{suffix}", fact_id=f"f2b_{suffix}", value="222", observed_at="2026-08-08T11:00:00Z")],
        )
        assert [x["normalized_value"] for x in _active(store, case_a)] == ["222"]

        store.replace_message_facts(
            message_id=f"mb_{suffix}",
            rows=[_row(case_id=case_b, message_id=f"mb_{suffix}", fact_id=f"fb_{suffix}", value="999", observed_at="2026-08-08T12:00:00Z")],
        )
        stats = store.reassign_case_facts(source_case_id=case_a, target_case_id=case_b)
        assert int(stats.get("moved") or 0) >= 1
        active_b = _active(store, case_b)
        assert len(active_b) == 1
        assert active_b[0]["normalized_value"] in {"222", "999"}

        # Consistency probe on synthetic cases: no dual-active identities.
        with store._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT entity_scope, fact_key, COUNT(*) AS n
                    FROM mailbox_memory_facts
                    WHERE case_id = %s AND status = 'active'
                    GROUP BY entity_scope, fact_key
                    HAVING COUNT(*) > 1
                    """,
                    (case_b,),
                )
                dual = cur.fetchall() or []
        assert dual == [], dual

        # Read-only inventory of existing dual-active violations in whole DB (report only).
        with store._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT case_id, entity_scope, fact_key, COUNT(*) AS n
                    FROM mailbox_memory_facts
                    WHERE status = 'active'
                    GROUP BY case_id, entity_scope, fact_key
                    HAVING COUNT(*) > 1
                    ORDER BY n DESC
                    LIMIT 20
                    """
                )
                existing = cur.fetchall() or []
        print("PROOF_PASS")
        print(f"database_url_host={url.split('@')[-1]}")
        print(f"existing_dual_active_identities={len(existing)}")
        for row in existing[:5]:
            print(f"dual={row}")
    finally:
        with store._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM mailbox_memory_facts WHERE case_id IN (%s, %s)", (case_a, case_b))
            conn.commit()


if __name__ == "__main__":
    main()
