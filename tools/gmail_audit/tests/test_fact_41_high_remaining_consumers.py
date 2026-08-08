"""FACT-4.1-HIGH-01: remaining CURRENT_STATE consumers must ignore superseded facts."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from calendar_models import infer_calendar_risk
from calendar_runtime import CalendarRuntime
from mailbox_memory.active_facts import fetch_current_facts_for_case, is_live_fact
from mailbox_memory.inmemory import InMemoryMailboxMemoryStore
from similar_cases_precedent import _active_fact_keys, fetch_similar_case_precedent_refs


def _case(store: InMemoryMailboxMemoryStore, *, case_id: str, family: str = "lead_opportunity", status: str = "open") -> None:
    store.upsert_case(
        {
            "case_id": case_id,
            "case_key": case_id.upper(),
            "case_family": family,
            "mailbox": "test",
            "subject": case_id,
            "status": status,
            "customer_name": "",
            "customer_email": "",
            "metadata": {},
            "updated_at": "2026-08-08T10:00:00Z",
        }
    )


def _heat_pair(*, case_id: str, old: str, new: str) -> list[dict]:
    return [
        {
            "fact_id": f"{case_id}_heat_old",
            "case_id": case_id,
            "message_id": "m1",
            "document_id": "",
            "entity_scope": "case",
            "fact_key": "heat_source",
            "normalized_value": old,
            "raw_value": old,
            "confidence": 0.9,
            "observed_at": "2026-08-01T08:00:00Z",
            "source_type": "llm",
            "source_ref": "msg:m1",
            "status": "superseded",
            "metadata": {},
        },
        {
            "fact_id": f"{case_id}_heat_new",
            "case_id": case_id,
            "message_id": "m2",
            "document_id": "",
            "entity_scope": "case",
            "fact_key": "heat_source",
            "normalized_value": new,
            "raw_value": new,
            "confidence": 0.7,
            "observed_at": "2026-08-02T08:00:00Z",
            "source_type": "llm",
            "source_ref": "msg:m2",
            "status": "active",
            "metadata": {},
        },
    ]


def test_canonical_active_reader_excludes_superseded() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    case_id = "case_41h_reader"
    _case(store, case_id=case_id)
    store.facts[case_id] = _heat_pair(case_id=case_id, old="gas", new="heat_pump")

    history = store.fetch_facts_for_case(case_id)
    active = store.fetch_active_facts_for_case(case_id)
    current = fetch_current_facts_for_case(store, case_id)

    assert len(history) == 2
    assert {str(r["status"]) for r in history} == {"active", "superseded"}
    assert len(active) == 1
    assert active[0]["normalized_value"] == "heat_pump"
    assert [r["fact_id"] for r in current] == [r["fact_id"] for r in active]


def test_calendar_context_ignores_superseded_proposed_date() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    case_id = "case_41h_cal"
    _case(store, case_id=case_id)
    store.facts[case_id] = [
        {
            "fact_id": "date_old",
            "case_id": case_id,
            "message_id": "m1",
            "document_id": "",
            "entity_scope": "case",
            "fact_key": "proposed_visit",
            "normalized_value": "2026-07-01",
            "raw_value": "2026-07-01",
            "confidence": 0.95,
            "observed_at": "2026-07-01T08:00:00Z",
            "source_type": "llm",
            "source_ref": "msg:m1",
            "status": "superseded",
            "metadata": {},
        },
        {
            "fact_id": "phone_active",
            "case_id": case_id,
            "message_id": "m2",
            "document_id": "",
            "entity_scope": "case",
            "fact_key": "phone",
            "normalized_value": "500600700",
            "raw_value": "500600700",
            "confidence": 0.8,
            "observed_at": "2026-08-02T08:00:00Z",
            "source_type": "llm",
            "source_ref": "msg:m2",
            "status": "active",
            "metadata": {},
        },
    ]

    runtime = CalendarRuntime(settings=SimpleNamespace(), store=store, client=None)
    ctx = runtime.context_for_case(case_id)
    assert ctx["calendar_risk"] == "calendar_event_missing"
    assert ctx["visit_lifecycle"] == "no_calendar_event"

    # Defense in depth: infer_calendar_risk itself must ignore superseded rows.
    assert (
        infer_calendar_risk(
            events=[],
            facts=store.fetch_facts_for_case(case_id),
        )
        == "calendar_event_missing"
    )


def test_calendar_context_sees_active_proposed_date() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    case_id = "case_41h_cal_active"
    _case(store, case_id=case_id)
    store.facts[case_id] = [
        {
            "fact_id": "date_live",
            "case_id": case_id,
            "message_id": "m1",
            "document_id": "",
            "entity_scope": "case",
            "fact_key": "proposed_visit",
            "normalized_value": "2026-08-20",
            "raw_value": "2026-08-20",
            "confidence": 0.9,
            "observed_at": "2026-08-08T08:00:00Z",
            "source_type": "llm",
            "source_ref": "msg:m1",
            "status": "active",
            "metadata": {},
        }
    ]
    runtime = CalendarRuntime(settings=SimpleNamespace(), store=store, client=None)
    ctx = runtime.context_for_case(case_id)
    assert ctx["calendar_risk"] == "customer_proposed_date"
    assert ctx["visit_lifecycle"] == "proposed_visit"


def test_precedent_overlap_ignores_superseded_heat_source() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    case_a = "case_41h_prec_a"
    case_b = "case_41h_prec_b"
    _case(store, case_id=case_a, status="open")
    _case(store, case_id=case_b, status="resolved")
    # A: gas superseded, heat_pump active — must NOT overlap B on gas.
    store.facts[case_a] = _heat_pair(case_id=case_a, old="gas", new="heat_pump")
    store.facts[case_b] = [
        {
            "fact_id": "b_gas",
            "case_id": case_b,
            "message_id": "mb",
            "document_id": "",
            "entity_scope": "case",
            "fact_key": "heat_source",
            "normalized_value": "gas",
            "raw_value": "gas",
            "confidence": 0.8,
            "observed_at": "2026-08-01T08:00:00Z",
            "source_type": "llm",
            "source_ref": "msg:mb",
            "status": "active",
            "metadata": {},
        }
    ]

    keys_a = _active_fact_keys(fetch_current_facts_for_case(store, case_a))
    assert keys_a == {"heat_source"}

    # Resolved-case SQL/InMemory overlap: B has heat_source but A's live value is heat_pump —
    # key overlap still exists (same key). Prove superseded-only key on B does not inflate.
    case_c = "case_41h_prec_c"
    _case(store, case_id=case_c, status="resolved")
    store.facts[case_c] = [
        {
            "fact_id": "c_old_only",
            "case_id": case_c,
            "message_id": "mc",
            "document_id": "",
            "entity_scope": "case",
            "fact_key": "legacy_only_key",
            "normalized_value": "x",
            "raw_value": "x",
            "confidence": 0.9,
            "observed_at": "2026-08-01T08:00:00Z",
            "source_type": "llm",
            "source_ref": "msg:mc",
            "status": "superseded",
            "metadata": {},
        }
    ]
    # Source case with only active heat_source — must not match C via superseded legacy_only_key.
    case_src = "case_41h_prec_src"
    _case(store, case_id=case_src, status="open")
    store.facts[case_src] = [
        {
            "fact_id": "src_legacy",
            "case_id": case_src,
            "message_id": "ms",
            "document_id": "",
            "entity_scope": "case",
            "fact_key": "legacy_only_key",
            "normalized_value": "y",
            "raw_value": "y",
            "confidence": 0.9,
            "observed_at": "2026-08-02T08:00:00Z",
            "source_type": "llm",
            "source_ref": "msg:ms",
            "status": "active",
            "metadata": {},
        }
    ]
    refs = fetch_similar_case_precedent_refs(store, case_id=case_src, limit=5)
    assert all(str(r.get("source_id") or "") != case_c for r in refs)

    scored = store.fetch_resolved_cases_by_family_and_fact_keys(
        case_family="lead_opportunity",
        fact_keys=["legacy_only_key"],
        exclude_case_id=case_src,
        limit=5,
    )
    assert all(str(row.get("case_id")) != case_c for row in scored)


def test_true_conflict_two_live_facts_preserved() -> None:
    """Supersession fix must not erase simultaneous live conflicts."""
    facts = [
        {
            "fact_key": "phone",
            "normalized_value": "111",
            "status": "active",
            "confidence": 0.6,
        },
        {
            "fact_key": "phone",
            "normalized_value": "222",
            "status": "active",
            "confidence": 0.6,
        },
        {
            "fact_key": "phone",
            "normalized_value": "000",
            "status": "superseded",
            "confidence": 0.99,
        },
    ]
    live = [f for f in facts if is_live_fact(f)]
    assert len(live) == 2
    assert {f["normalized_value"] for f in live} == {"111", "222"}


def test_postgres_supersession_metadata_json_roundtrip() -> None:
    """Live PG: supersede UPDATE must JSON-adapt metadata (psycopg3).

    Opt-in only via MAILBOX_MEMORY_DSN / FACT41_PROOF_DSN so Gate A stays hermetic.
    """
    import os

    import pytest

    dsn = (os.environ.get("FACT41_PROOF_DSN") or os.environ.get("MAILBOX_MEMORY_DSN") or "").strip()
    if not dsn:
        pytest.skip("set FACT41_PROOF_DSN or MAILBOX_MEMORY_DSN for live PG supersession proof")
    try:
        from mailbox_memory.postgres import PostgresMailboxMemoryStore

        store = PostgresMailboxMemoryStore(dsn)
        store.bootstrap()
        store.fetch_facts_for_case("__probe__")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"mailbox postgres unavailable: {exc}")

    case_id = "case_41h_pg_meta"
    store.upsert_case(
        {
            "case_id": case_id,
            "case_key": "TOP-41H-PG",
            "case_family": "lead_opportunity",
            "mailbox": "test",
            "subject": "pg meta",
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

    base = {
        "case_id": case_id,
        "message_id": "m1",
        "document_id": "",
        "entity_scope": "case",
        "fact_key": "phone",
        "raw_value": "111",
        "confidence": 0.9,
        "observed_at": "2026-08-01T08:00:00Z",
        "source_type": "llm",
        "source_ref": "msg:m1",
        "status": "active",
        "metadata": {},
    }
    assert store.append_facts_with_supersession([{**base, "fact_id": "pg_old", "normalized_value": "111"}])["inserted"] == 1
    stats = store.append_facts_with_supersession(
        [{**base, "fact_id": "pg_new", "message_id": "m2", "normalized_value": "222", "observed_at": "2026-08-02T08:00:00Z"}]
    )
    assert stats["superseded"] == 1
    assert stats["inserted"] == 1
    active = store.fetch_active_facts_for_case(case_id)
    hist = store.fetch_facts_for_case(case_id)
    assert len(active) == 1 and active[0]["normalized_value"] == "222"
    assert any(str(r.get("status")) == "superseded" for r in hist)
