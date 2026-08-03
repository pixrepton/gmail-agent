"""RP-29: DQ-10 fact supersession over ON CONFLICT DO NOTHING."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory.inmemory import InMemoryMailboxMemoryStore


def test_supersession_replaces_active_value() -> None:
    store = InMemoryMailboxMemoryStore()
    base = {
        "case_id": "case_rp29",
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
    }
    row_v1 = {
        **base,
        "fact_id": "fact_v1",
        "normalized_value": "120",
    }
    row_v2 = {
        **base,
        "fact_id": "fact_v2",
        "message_id": "msg2",
        "source_ref": "agent:msg2",
        "normalized_value": "140",
        "observed_at": "2026-08-03T09:00:00Z",
    }
    stats1 = store.append_facts_with_supersession([row_v1])
    assert stats1["inserted"] == 1

    stats2 = store.append_facts_with_supersession([row_v2])
    assert stats2["inserted"] == 1
    assert stats2["superseded"] == 1

    active = [
        item
        for items in store.facts.values()
        for item in items
        if str(item.get("case_id")) == "case_rp29"
        and str(item.get("fact_key")) == "heated_area_m2"
        and str(item.get("status")) == "active"
    ]
    superseded = [
        item
        for items in store.facts.values()
        for item in items
        if str(item.get("case_id")) == "case_rp29"
        and str(item.get("fact_key")) == "heated_area_m2"
        and str(item.get("status")) == "superseded"
    ]
    assert len(active) == 1
    assert active[0]["normalized_value"] == "140"
    assert len(superseded) == 1
    assert superseded[0]["normalized_value"] == "120"


def test_supersession_idempotent_on_same_value() -> None:
    store = InMemoryMailboxMemoryStore()
    row = {
        "fact_id": "fact_same",
        "case_id": "case_rp29b",
        "message_id": "msg1",
        "document_id": "",
        "entity_scope": "building",
        "fact_key": "city",
        "normalized_value": "Radlin",
        "raw_value": "Radlin",
        "confidence": 0.8,
        "observed_at": "2026-08-03T08:00:00Z",
        "source_type": "agent_extraction",
        "source_ref": "agent:msg1",
        "status": "active",
        "metadata": {},
    }
    stats1 = store.append_facts_with_supersession([row])
    stats2 = store.append_facts_with_supersession([{**row, "fact_id": "fact_same2", "message_id": "msg2"}])
    assert stats1["inserted"] == 1
    assert stats2["unchanged"] == 1
    assert stats2["inserted"] == 0
