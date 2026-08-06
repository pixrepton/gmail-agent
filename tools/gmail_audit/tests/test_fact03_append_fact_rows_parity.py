"""FACT-03: InMemory append_fact_rows must match Postgres supersession semantics."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory.inmemory import InMemoryMailboxMemoryStore
from mailbox_memory.protocol import MailboxMemoryStore


def _fact_rows_for_case(store: InMemoryMailboxMemoryStore, case_id: str, fact_key: str) -> list[dict]:
    return [
        item
        for items in store.facts.values()
        for item in items
        if str(item.get("case_id")) == case_id and str(item.get("fact_key")) == fact_key
    ]


def _base_row(**overrides: object) -> dict:
    row = {
        "case_id": "case_fact03",
        "message_id": "msg1",
        "document_id": "",
        "entity_scope": "building",
        "fact_key": "heated_area_m2",
        "raw_value": "120",
        "confidence": 0.8,
        "observed_at": "2026-08-03T08:00:00Z",
        "source_type": "agent_extraction",
        "source_ref": "agent:msg1",
        "status": "active",
        "metadata": {},
        "fact_id": "fact_v1",
        "normalized_value": "120",
    }
    row.update(overrides)
    return row


def test_protocol_declares_append_facts_with_supersession() -> None:
    assert hasattr(MailboxMemoryStore, "append_facts_with_supersession")


def test_append_fact_rows_supersedes_on_value_change() -> None:
    """Postgres append_fact_rows delegates to supersession; InMemory must match."""
    store = InMemoryMailboxMemoryStore()
    row_v1 = _base_row()
    row_v2 = _base_row(
        fact_id="fact_v2",
        message_id="msg2",
        source_ref="agent:msg2",
        normalized_value="140",
        raw_value="140",
        observed_at="2026-08-03T09:00:00Z",
    )

    store.append_fact_rows([row_v1])
    store.append_fact_rows([row_v2])

    rows = _fact_rows_for_case(store, "case_fact03", "heated_area_m2")
    active = [r for r in rows if str(r.get("status")) == "active"]
    superseded = [r for r in rows if str(r.get("status")) == "superseded"]

    assert len(active) == 1
    assert active[0]["normalized_value"] == "140"
    assert active[0]["fact_id"] == "fact_v2"
    assert len(superseded) == 1
    assert superseded[0]["normalized_value"] == "120"
    assert superseded[0]["fact_id"] == "fact_v1"
    assert superseded[0].get("metadata", {}).get("superseded_by_fact_id") == "fact_v2"


def test_append_fact_rows_idempotent_on_same_value() -> None:
    store = InMemoryMailboxMemoryStore()
    row = _base_row(case_id="case_fact03b", fact_key="city", normalized_value="Radlin", raw_value="Radlin")
    store.append_fact_rows([row])
    store.append_fact_rows([{**row, "fact_id": "fact_same2", "message_id": "msg2", "source_ref": "agent:msg2"}])

    rows = _fact_rows_for_case(store, "case_fact03b", "city")
    active = [r for r in rows if str(r.get("status")) == "active"]
    assert len(active) == 1
    assert active[0]["fact_id"] == "fact_v1"
    assert len(rows) == 1


def test_append_fact_rows_matches_explicit_supersession_path() -> None:
    """Same insert sequence via append_fact_rows vs append_facts_with_supersession."""
    via_rows = InMemoryMailboxMemoryStore()
    via_super = InMemoryMailboxMemoryStore()
    row_v1 = _base_row(case_id="case_fact03c")
    row_v2 = _base_row(
        case_id="case_fact03c",
        fact_id="fact_v2",
        message_id="msg2",
        source_ref="agent:msg2",
        normalized_value="140",
        raw_value="140",
        observed_at="2026-08-03T09:00:00Z",
    )

    via_rows.append_fact_rows([row_v1])
    via_rows.append_fact_rows([row_v2])
    via_super.append_facts_with_supersession([row_v1])
    via_super.append_facts_with_supersession([row_v2])

    def _summary(store: InMemoryMailboxMemoryStore) -> list[tuple[str, str, str]]:
        rows = _fact_rows_for_case(store, "case_fact03c", "heated_area_m2")
        return sorted(
            (
                str(r.get("fact_id") or ""),
                str(r.get("status") or ""),
                str(r.get("normalized_value") or ""),
            )
            for r in rows
        )

    assert _summary(via_rows) == _summary(via_super)
