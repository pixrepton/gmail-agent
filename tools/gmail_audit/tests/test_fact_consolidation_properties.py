"""P1.5: metamorphic/property invariants of fact consolidation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory import InMemoryMailboxMemoryStore
from mailbox_memory_runtime import split_conflicting_facts


def _row(
    *,
    fact_key: str,
    value: str,
    entity_scope: str,
    source_ref: str,
    observed_at: str,
    confidence: float = 0.8,
) -> dict:
    return {
        "fact_id": f"f_{source_ref}_{fact_key}_{value}",
        "case_id": "case_p",
        "message_id": source_ref,
        "document_id": "",
        "entity_scope": entity_scope,
        "fact_key": fact_key,
        "normalized_value": value,
        "raw_value": value,
        "confidence": confidence,
        "observed_at": observed_at,
        "source_type": "gmail_message",
        "source_ref": source_ref,
        "status": "active",
        "metadata": {},
    }


def _resolve(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case({"case_id": "case_p", "status": "open"})
    store.append_facts_with_supersession(rows)
    return split_conflicting_facts(store.fetch_facts_for_case("case_p"))


def _state_key(active: list[dict], conflicts: list[dict]) -> str:
    return (
        str(sorted((f.get("entity_scope"), f.get("fact_key"), f.get("normalized_value")) for f in active))
        + "|"
        + str(sorted((c.get("fact_key"), sorted(c.get("values") or [])) for c in conflicts))
    )


def test_source_order_permutation_same_resolved_state() -> None:
    rows_a = [
        _row(fact_key="device_model", value="WH-XYZ", entity_scope="customer", source_ref="m1", observed_at="2026-08-23T10:00:00Z"),
        _row(fact_key="device_model", value="WH-XYZ", entity_scope="document", source_ref="d1", observed_at="2026-08-23T11:00:00Z"),
    ]
    rows_b = list(reversed(rows_a))
    assert _state_key(*_resolve(rows_a)) == _state_key(*_resolve(rows_b))


def test_timestamp_permutation_same_conflict_state() -> None:
    def build(x_ts: str, y_ts: str):
        return [
            _row(fact_key="device_model", value="WH-XYZ", entity_scope="customer", source_ref="m1", observed_at=x_ts),
            _row(fact_key="device_model", value="WH-ABC", entity_scope="document", source_ref="d1", observed_at=y_ts),
        ]

    assert _state_key(*_resolve(build("2026-08-23T10:00:00Z", "2026-08-23T11:00:00Z"))) == _state_key(
        *_resolve(build("2026-08-23T11:00:00Z", "2026-08-23T10:00:00Z"))
    )


def test_duplicate_evidence_same_effective_value() -> None:
    base = [
        _row(fact_key="heated_area_m2", value="120", entity_scope="customer", source_ref="m1", observed_at="2026-08-23T10:00:00Z"),
    ]
    dup = base + [
        _row(fact_key="heated_area_m2", value="120", entity_scope="customer", source_ref="m2", observed_at="2026-08-23T11:00:00Z"),
        _row(fact_key="heated_area_m2", value="120", entity_scope="customer", source_ref="m3", observed_at="2026-08-23T12:00:00Z"),
    ]
    assert _state_key(*_resolve(base)) == _state_key(*_resolve(dup))


def test_removing_all_evidence_removes_support() -> None:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case({"case_id": "case_p", "status": "open"})
    store.append_facts_with_supersession(
        [_row(fact_key="city", value="Radlin", entity_scope="customer", source_ref="m1", observed_at="2026-08-23T10:00:00Z")]
    )
    # Mark the single supporting row superseded (evidence removed).
    for bucket, items in list(store.facts.items()):
        for idx, item in enumerate(items):
            if str(item.get("fact_key")) == "city":
                store.facts[bucket][idx] = {**item, "status": "superseded"}
    active, _ = split_conflicting_facts(store.fetch_facts_for_case("case_p"))
    assert not any(f.get("fact_key") == "city" for f in active)


def test_irrelevant_evidence_does_not_change_unrelated_proposition() -> None:
    base = [_row(fact_key="city", value="Radlin", entity_scope="customer", source_ref="m1", observed_at="2026-08-23T10:00:00Z")]
    extra = base + [
        _row(fact_key="heated_area_m2", value="120", entity_scope="customer", source_ref="m2", observed_at="2026-08-23T11:00:00Z")
    ]
    def city_state(rows):
        active, conflicts = _resolve(rows)
        city_active = sorted(
            (f.get("entity_scope"), f.get("fact_key"), f.get("normalized_value"))
            for f in active
            if f.get("fact_key") == "city"
        )
        city_conflicts = sorted(
            (c.get("fact_key"), sorted(c.get("values") or []))
            for c in conflicts
            if c.get("fact_key") == "city"
        )
        return str(city_active) + "|" + str(city_conflicts)

    assert city_state(base) == city_state(extra)


def test_persist_reload_same_resolved_state() -> None:
    rows = [
        _row(fact_key="device_model", value="WH-XYZ", entity_scope="customer", source_ref="m1", observed_at="2026-08-23T10:00:00Z"),
        _row(fact_key="device_model", value="WH-ABC", entity_scope="document", source_ref="d1", observed_at="2026-08-23T11:00:00Z"),
    ]
    store = InMemoryMailboxMemoryStore()
    store.upsert_case({"case_id": "case_p", "status": "open"})
    store.append_facts_with_supersession(rows)
    before = _state_key(*split_conflicting_facts(store.fetch_facts_for_case("case_p")))
    store2 = InMemoryMailboxMemoryStore()
    store2.cases = dict(store.cases)
    store2.facts = dict(store.facts)
    after = _state_key(*split_conflicting_facts(store2.fetch_facts_for_case("case_p")))
    assert before == after


def test_derived_confidence_change_does_not_change_authority() -> None:
    low = _row(fact_key="device_fault_cause", value="pompa", entity_scope="customer", source_ref="llm", observed_at="2026-08-23T10:00:00Z", confidence=0.3)
    high = _row(fact_key="device_fault_cause", value="pompa", entity_scope="customer", source_ref="llm", observed_at="2026-08-23T10:00:00Z", confidence=0.95)
    low["metadata"] = {"source_origin": "DERIVED", "evidence_authority": "DERIVED_LLM_CLAIM", "instruction_authority": "NONE"}
    high["metadata"] = {"source_origin": "DERIVED", "evidence_authority": "DERIVED_LLM_CLAIM", "instruction_authority": "NONE"}
    store = InMemoryMailboxMemoryStore()
    store.upsert_case({"case_id": "case_p", "status": "open"})
    store.append_facts_with_supersession([low])
    store.append_facts_with_supersession([high])
    active = store.fetch_active_facts_for_case("case_p")
    row = [f for f in active if f.get("fact_key") == "device_fault_cause"][0]
    meta = row.get("metadata") or {}
    assert meta.get("evidence_authority") == "DERIVED_LLM_CLAIM"
    assert meta.get("instruction_authority") == "NONE"


def test_source_origin_change_keeps_proposition_identity() -> None:
    customer = _row(fact_key="device_model", value="WH-XYZ", entity_scope="customer", source_ref="m1", observed_at="2026-08-23T10:00:00Z")
    customer["metadata"] = {"source_origin": "CUSTOMER_EMAIL"}
    document = _row(fact_key="device_model", value="WH-XYZ", entity_scope="document", source_ref="d1", observed_at="2026-08-23T11:00:00Z")
    document["metadata"] = {"source_origin": "ATTACHMENT"}
    active, conflicts = _resolve([customer, document])
    values = {str(f.get("normalized_value")) for f in active if f.get("fact_key") == "device_model"}
    assert values == {"WH-XYZ"}
    assert not conflicts
