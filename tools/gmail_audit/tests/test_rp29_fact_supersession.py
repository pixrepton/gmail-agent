"""RP-29: DQ-10 fact supersession over ON CONFLICT DO NOTHING."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory.inmemory import InMemoryMailboxMemoryStore
from mailbox_memory_runtime import build_case_context_pack


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
        "metadata": {
            "allow_subject_supersession": True,
            "source_origin": "OPERATOR",
            "evidence_authority": "OPERATOR_STATEMENT",
            "instruction_authority": "NONE",
        },
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


def test_supersession_survives_into_the_assembled_case_context_pack() -> None:
    """Journey E (document -> fact -> CaseContextPack -> planner/operator), real store.

    A first document is extracted (120 m2, high confidence 0.95). A second, later document
    corrects it (140 m2, lower confidence 0.6) -- RP-29's write path marks the first row
    superseded. Before this fix, `build_case_context_pack` -> `split_conflicting_facts`
    ignored `status` and ranked purely by confidence, so the superseded 120 m2 would still
    win and reach the planner/operator context pack as the "active" fact. This exercises the
    real write path (`append_facts_with_supersession`) and the real read path
    (`build_case_context_pack`) together, end to end, with no mocking.
    """
    store = InMemoryMailboxMemoryStore()
    store.cases = {"case_rp29c": {"case_id": "case_rp29c"}}
    base = {
        "case_id": "case_rp29c",
        "message_id": "msg1",
        "document_id": "doc1",
        "entity_scope": "building",
        "fact_key": "heated_area_m2",
        "raw_value": "120",
        "confidence": 0.95,
        "observed_at": "2026-08-03T08:00:00Z",
        "source_type": "document_extraction",
        "source_ref": "doc:doc1",
        "status": "active",
        "metadata": {},
    }
    row_v1 = {**base, "fact_id": "fact_v1", "normalized_value": "120"}
    row_v2 = {
        **base,
        "fact_id": "fact_v2",
        "document_id": "doc2",
        "source_ref": "doc:doc2",
        "normalized_value": "140",
        "confidence": 0.6,
        "observed_at": "2026-08-03T09:00:00Z",
        "metadata": {
            "allow_subject_supersession": True,
            "source_origin": "OPERATOR",
            "evidence_authority": "OPERATOR_STATEMENT",
            "instruction_authority": "NONE",
        },
    }
    assert store.append_facts_with_supersession([row_v1])["inserted"] == 1
    assert store.append_facts_with_supersession([row_v2])["superseded"] == 1

    pack = build_case_context_pack(store=store, case_id="case_rp29c")

    assert len(pack.active_facts) == 1
    assert pack.active_facts[0]["normalized_value"] == "140"
    assert pack.active_facts[0]["status"] == "active"
    assert pack.conflicting_facts == []


def test_pack_embedded_snapshot_uses_same_active_fact_filter_as_pack_sections() -> None:
    """FACT-05: every pack section AND the embedded snapshot must share one
    active-fact filter (split_conflicting_facts semantics).

    Pack correctly exposes only the active winner in `active_facts`, but historically
    passed raw `facts` (including superseded) into `build_current_case_context_snapshot`
    → `build_case_snapshot`. That let superseded values reappear in snapshot
    `key_facts` and manufacture false open-question conflicts from settled rows.
    """
    store = InMemoryMailboxMemoryStore()
    store.cases = {"case_fact05": {"case_id": "case_fact05"}}
    base = {
        "case_id": "case_fact05",
        "message_id": "msg1",
        "document_id": "doc1",
        "entity_scope": "building",
        "fact_key": "heated_area_m2",
        "raw_value": "120",
        "confidence": 0.95,
        "observed_at": "2026-08-03T08:00:00Z",
        "source_type": "document_extraction",
        "source_ref": "doc:doc1",
        "status": "active",
        "metadata": {},
    }
    row_v1 = {**base, "fact_id": "fact_v1", "normalized_value": "120"}
    row_v2 = {
        **base,
        "fact_id": "fact_v2",
        "document_id": "doc2",
        "source_ref": "doc:doc2",
        "normalized_value": "140",
        "confidence": 0.6,
        "observed_at": "2026-08-03T09:00:00Z",
        "metadata": {
            "allow_subject_supersession": True,
            "source_origin": "OPERATOR",
            "evidence_authority": "OPERATOR_STATEMENT",
            "instruction_authority": "NONE",
        },
    }
    assert store.append_facts_with_supersession([row_v1])["inserted"] == 1
    assert store.append_facts_with_supersession([row_v2])["superseded"] == 1

    pack = build_case_context_pack(store=store, case_id="case_fact05")

    assert len(pack.active_facts) == 1
    assert pack.active_facts[0]["normalized_value"] == "140"
    assert pack.conflicting_facts == []

    snapshot = pack.snapshot if isinstance(pack.snapshot, dict) else {}
    key_facts = list(snapshot.get("key_facts") or [])
    area_facts = [item for item in key_facts if str(item.get("fact_key")) == "heated_area_m2"]
    assert len(area_facts) == 1
    assert area_facts[0]["value"] == "140"

    open_questions = [str(q) for q in list(snapshot.get("open_questions") or [])]
    assert not any("120" in q and "heated_area_m2" in q for q in open_questions)
    assert not any("Konflikt danych dla heated_area_m2" in q for q in open_questions)

    snapshot_conflicts = list(snapshot.get("conflicting_facts") or [])
    assert not any(str(item.get("fact_key")) == "heated_area_m2" for item in snapshot_conflicts)
