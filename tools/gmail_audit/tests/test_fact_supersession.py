"""P1.5: legal supersession vs conflict; history and provenance preserved."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory import InMemoryMailboxMemoryStore
from mailbox_memory.active_facts import fetch_current_facts_for_case
from mailbox_memory_runtime import split_conflicting_facts


def _row(
    *,
    case_id: str,
    fact_key: str,
    value: str,
    message_id: str,
    fact_id: str,
    entity_scope: str = "customer",
    observed_at: str = "2026-08-23T10:00:00Z",
    source_type: str = "gmail_message",
    source_ref: str = "",
    document_id: str = "",
) -> dict:
    return {
        "fact_id": fact_id,
        "case_id": case_id,
        "message_id": message_id,
        "document_id": document_id,
        "entity_scope": entity_scope,
        "fact_key": fact_key,
        "normalized_value": value,
        "raw_value": value,
        "confidence": 0.8,
        "observed_at": observed_at,
        "source_type": source_type,
        "source_ref": source_ref or message_id,
        "status": "active",
        "metadata": {},
    }


def _store() -> InMemoryMailboxMemoryStore:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case({"case_id": "case_s", "status": "open"})
    return store


def test_explicit_customer_correction_supersedes_and_surfaces_conflict_per_ctx03() -> None:
    store = _store()
    store.replace_message_facts(
        message_id="m1",
        rows=[_row(case_id="case_s", fact_key="customer_phone", value="111", message_id="m1", fact_id="f1", observed_at="2026-08-23T10:00:00Z")],
    )
    # New customer message changes the value (CTX-03: replace_message_facts
    # supersessions surface as a real disagreement, not silently settled).
    store.replace_message_facts(
        message_id="m2",
        rows=[
            _row(
                case_id="case_s", fact_key="customer_phone", value="222", message_id="m2", fact_id="f2",
                observed_at="2026-08-23T11:00:00Z",
            )
        ],
    )
    current = fetch_current_facts_for_case(store, "case_s")
    values = [c["normalized_value"] for c in current if c.get("fact_key") == "customer_phone"]
    assert values == ["222"]
    history = [f for f in store.fetch_facts_for_case("case_s") if f.get("fact_key") == "customer_phone"]
    old = [f for f in history if f.get("status") == "superseded"]
    assert any(f["normalized_value"] == "111" for f in old)
    _, conflicts = split_conflicting_facts(store.fetch_facts_for_case("case_s"))
    # CTX-03 documented semantics: replace supersession is a genuine conflict
    # until an explicit settled resolution exists (same-scope authoritative
    # append, tested below).
    assert any(c.get("fact_key") == "customer_phone" for c in conflicts)


def test_superseded_row_keeps_provenance_reachable() -> None:
    store = _store()
    store.replace_message_facts(
        message_id="m1",
        rows=[
            _row(
                case_id="case_s", fact_key="city", value="Radlin", message_id="m1", fact_id="f1",
                source_type="gmail_message", source_ref="m1",
            )
        ],
    )
    store.replace_message_facts(
        message_id="m2",
        rows=[_row(case_id="case_s", fact_key="city", value="Rybnik", message_id="m2", fact_id="f2")],
    )
    history = [f for f in store.fetch_facts_for_case("case_s") if f.get("fact_key") == "city"]
    superseded = [f for f in history if f.get("status") == "superseded"]
    assert superseded
    old = superseded[0]
    assert old.get("source_ref") == "m1"
    meta = old.get("metadata") or {}
    assert meta.get("superseded_by_fact_id") == "f2"


def test_same_scope_authoritative_append_is_settled_supersession() -> None:
    store = _store()
    store.append_facts_with_supersession(
        [
            _row(
                case_id="case_s", fact_key="amount_total", value="1000,00", message_id="", fact_id="a1",
                entity_scope="case", source_type="document_intelligence", source_ref="doc1", document_id="doc1",
            )
        ]
    )
    stats = store.append_facts_with_supersession(
        [
            _row(
                case_id="case_s", fact_key="amount_total", value="1200,00", message_id="", fact_id="a2",
                entity_scope="case", source_type="document_intelligence", source_ref="doc2", document_id="doc2",
            )
        ]
    )
    assert stats["superseded"] >= 1
    current = fetch_current_facts_for_case(store, "case_s")
    values = [c["normalized_value"] for c in current if c.get("fact_key") == "amount_total"]
    assert values == ["1200,00"]
    _, conflicts = split_conflicting_facts(store.fetch_facts_for_case("case_s"))
    assert not any(c.get("fact_key") == "amount_total" for c in conflicts)


def test_case_merge_reconciliation_is_bounded_to_merge() -> None:
    from agent_runtime.tools.write_executors import execute_merge_cases

    store = _store()
    store.upsert_case({"case_id": "case_a", "status": "open"})
    store.replace_message_facts(
        message_id="ma",
        rows=[_row(case_id="case_a", fact_key="customer_phone", value="111", message_id="ma", fact_id="fa", observed_at="2026-08-23T10:00:00Z")],
    )
    store.replace_message_facts(
        message_id="mb",
        rows=[_row(case_id="case_s", fact_key="customer_phone", value="222", message_id="mb", fact_id="fb", observed_at="2026-08-23T12:00:00Z")],
    )
    result = execute_merge_cases(
        {"source_case_id": "case_a", "target_case_id": "case_s"},
        mailbox_store=store,
        correlation_store=None,
    )
    assert result["status"] == "ok"
    current = fetch_current_facts_for_case(store, "case_s")
    values = [c["normalized_value"] for c in current if c.get("fact_key") == "customer_phone"]
    assert values == ["222"]
    history = [f for f in store.fetch_facts_for_case("case_s") if f.get("fact_key") == "customer_phone"]
    assert any(
        f.get("normalized_value") == "111" and f.get("status") == "superseded"
        for f in history
    )
    # Merge reconciliation must not leak into general resolution: no conflict flag.
    _, conflicts = split_conflicting_facts(store.fetch_facts_for_case("case_s"))
    assert not any(c.get("fact_key") == "customer_phone" for c in conflicts)
