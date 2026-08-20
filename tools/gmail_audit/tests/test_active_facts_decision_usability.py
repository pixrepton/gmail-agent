from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory.active_facts import (
    action_conflict_block,
    annotate_decision_fact_use,
    fetch_current_facts_for_case,
)
from mailbox_memory.inmemory import InMemoryMailboxMemoryStore
from mailbox_memory_models import CaseContextPack
from mailbox_memory_runtime import build_case_context_pack, split_conflicting_facts
from case_context_contract import build_case_context_pack_vnext


def _fact(
    *,
    value: str,
    fact_id: str,
    observed_at: str,
    status: str = "active",
) -> dict:
    return {
        "fact_id": fact_id,
        "case_id": "case_active_facts",
        "message_id": fact_id,
        "entity_scope": "building",
        "fact_key": "heated_area_m2",
        "normalized_value": value,
        "raw_value": value,
        "confidence": 0.8,
        "observed_at": observed_at,
        "source_type": "message",
        "source_ref": fact_id,
        "status": status,
        "metadata": {},
    }


def test_same_value_does_not_create_false_decision_conflict() -> None:
    active, conflicts = split_conflicting_facts(
        [
            _fact(value="180", fact_id="f1", observed_at="2026-08-20T08:00:00Z"),
            _fact(value="180", fact_id="f2", observed_at="2026-08-20T09:00:00Z"),
        ]
    )

    annotated = annotate_decision_fact_use(active, conflicts)

    assert conflicts == []
    assert annotated[0]["trust_state"] == "confirmed"
    assert annotated[0]["decision_usable"] is True
    assert annotated[0]["decision_block_reason"] is None


def test_conflicting_values_remain_unusable_regardless_of_timestamp_order() -> None:
    older = _fact(value="180", fact_id="older", observed_at="2026-08-20T08:00:00Z")
    newer = _fact(value="220", fact_id="newer", observed_at="2026-08-20T09:00:00Z")

    for rows in ([older, newer], [newer, older]):
        active, conflicts = split_conflicting_facts(list(rows))
        annotated = annotate_decision_fact_use(active, conflicts)

        assert conflicts == [
            {"entity_scope": "building", "fact_key": "heated_area_m2", "values": ["180", "220"]}
        ]
        assert annotated[0]["trust_state"] == "conflicted"
        assert annotated[0]["decision_usable"] is False
        assert annotated[0]["decision_block_reason"] == "fact_conflict"


def test_explicit_supersession_restores_decision_usable_fact() -> None:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case({"case_id": "case_active_facts", "status": "open"})
    assert store.append_facts_with_supersession(
        [_fact(value="180", fact_id="f_old", observed_at="2026-08-20T08:00:00Z")]
    )["inserted"] == 1
    assert store.append_facts_with_supersession(
        [_fact(value="220", fact_id="f_new", observed_at="2026-08-20T09:00:00Z")]
    )["superseded"] == 1

    current = fetch_current_facts_for_case(store, "case_active_facts")
    active, conflicts = split_conflicting_facts(current)
    annotated = annotate_decision_fact_use(active, conflicts)

    assert len(annotated) == 1
    assert annotated[0]["normalized_value"] == "220"
    assert annotated[0]["trust_state"] == "confirmed"
    assert annotated[0]["decision_usable"] is True
    assert annotated[0]["decision_block_reason"] is None


def test_conflicted_critical_fact_blocks_only_dependent_action() -> None:
    annotated = annotate_decision_fact_use(
        [_fact(value="180", fact_id="winner", observed_at="2026-08-20T09:00:00Z")],
        [{"entity_scope": "building", "fact_key": "heated_area_m2", "values": ["180", "220"]}],
    )

    assert action_conflict_block(action_type="prepare_offer", facts=annotated) == {
        "blocked": True,
        "blocked_fact_keys": ["heated_area_m2"],
        "decision_block_reason": "fact_conflict",
    }
    assert action_conflict_block(action_type="acknowledge_documents", facts=annotated) == {
        "blocked": False,
        "blocked_fact_keys": [],
        "decision_block_reason": None,
    }


def test_case_context_vnext_preserves_decision_safety_metadata() -> None:
    pack = CaseContextPack(
        case_id="case_contract_fact_use",
        active_facts=[
            _fact(value="180", fact_id="winner", observed_at="2026-08-20T09:00:00Z")
        ],
        conflicting_facts=[
            {"entity_scope": "building", "fact_key": "heated_area_m2", "values": ["180", "220"]}
        ],
    )

    contract = build_case_context_pack_vnext(pack)
    fact = contract["facts"][0]

    assert fact["predicate"] == "heated_area_m2"
    assert fact["trust_state"] == "conflicted"
    assert fact["decision_usable"] is False
    assert fact["decision_block_reason"] == "fact_conflict"


def test_production_case_context_pack_preserves_decision_safety_metadata() -> None:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case({"case_id": "case_pack_fact_use", "status": "open"})
    store.facts["msg_conflict"] = [
        {
            **_fact(value="180", fact_id="f_body", observed_at="2026-08-20T08:00:00Z"),
            "case_id": "case_pack_fact_use",
            "message_id": "msg_conflict",
        },
        {
            **_fact(value="220", fact_id="f_doc", observed_at="2026-08-20T09:00:00Z"),
            "case_id": "case_pack_fact_use",
            "message_id": "msg_conflict",
        },
    ]

    pack = build_case_context_pack(store=store, case_id="case_pack_fact_use")

    area = next(f for f in pack.active_facts if f.get("fact_key") == "heated_area_m2")
    assert area["trust_state"] == "conflicted"
    assert area["decision_usable"] is False
    assert area["decision_block_reason"] == "fact_conflict"
