"""P1.5: proposition identity, same-value consolidation, provenance union."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory import InMemoryMailboxMemoryStore
from mailbox_memory.active_facts import fetch_current_facts_for_case
from mailbox_memory.facts import merge_fact_evidence, row_evidence_refs
from mailbox_memory_runtime import split_conflicting_facts


def _row(
    *,
    case_id: str,
    fact_key: str,
    value: str,
    source_type: str,
    source_ref: str,
    entity_scope: str = "customer",
    message_id: str = "",
    document_id: str = "",
    fact_id: str = "",
    confidence: float = 0.8,
    observed_at: str = "2026-08-23T10:00:00Z",
    metadata: dict | None = None,
) -> dict:
    return {
        "fact_id": fact_id or f"f_{source_type}_{source_ref}_{fact_key}",
        "case_id": case_id,
        "message_id": message_id,
        "document_id": document_id,
        "entity_scope": entity_scope,
        "fact_key": fact_key,
        "normalized_value": value,
        "raw_value": value,
        "confidence": confidence,
        "observed_at": observed_at,
        "source_type": source_type,
        "source_ref": source_ref,
        "status": "active",
        "metadata": dict(metadata or {}),
    }


def _store(case_id: str = "case_c") -> InMemoryMailboxMemoryStore:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case({"case_id": case_id, "status": "open"})
    return store


def test_proposition_identity_is_scope_and_key() -> None:
    store = _store()
    store.append_facts_with_supersession(
        [
            _row(case_id="case_c", fact_key="device_model", value="WH-XYZ", source_type="gmail_message", source_ref="m1", entity_scope="customer"),
            _row(case_id="case_c", fact_key="device_model", value="WH-XYZ", source_type="structured_document_parse", source_ref="doc1", entity_scope="document", document_id="doc1"),
        ]
    )
    active, conflicts = split_conflicting_facts(store.fetch_facts_for_case("case_c"))
    values = {str(f.get("normalized_value")) for f in active if f.get("fact_key") == "device_model"}
    assert values == {"WH-XYZ"}
    assert not conflicts


def test_same_value_across_mail_and_attachment_is_one_effective_view() -> None:
    store = _store()
    mail = _row(
        case_id="case_c", fact_key="device_model", value="WH-XYZ",
        source_type="gmail_message", source_ref="m1", entity_scope="customer", message_id="m1",
        metadata={"source_origin": "CUSTOMER_EMAIL", "evidence_authority": "CUSTOMER_STATEMENT", "instruction_authority": "NONE"},
    )
    attachment = _row(
        case_id="case_c", fact_key="device_model", value="WH-XYZ",
        source_type="structured_document_parse", source_ref="doc1", entity_scope="document",
        document_id="doc1", metadata={"source_origin": "ATTACHMENT", "evidence_authority": "CUSTOMER_DOCUMENT", "instruction_authority": "NONE"},
    )
    store.append_facts_with_supersession([mail])
    store.append_facts_with_supersession([attachment])
    active, conflicts = split_conflicting_facts(store.fetch_facts_for_case("case_c"))
    effective = {str(f.get("normalized_value")) for f in active if f.get("fact_key") == "device_model"}
    assert effective == {"WH-XYZ"}
    assert not conflicts
    # Provenance of BOTH sources is reachable from the fact trail.
    rows = [f for f in store.fetch_facts_for_case("case_c") if f.get("fact_key") == "device_model"]
    origins = {str((f.get("metadata") or {}).get("source_origin")) for f in rows}
    assert origins == {"CUSTOMER_EMAIL", "ATTACHMENT"}


def test_same_scope_same_value_append_merges_evidence() -> None:
    store = _store()
    store.append_facts_with_supersession(
        [
            _row(
                case_id="case_c", fact_key="device_model", value="WH-XYZ",
                source_type="structured_document_parse", source_ref="doc1", entity_scope="document",
                document_id="doc1", metadata={"evidence_ref": {"source_type": "document", "source_id": "doc1", "page": 1}},
            )
        ]
    )
    store.append_facts_with_supersession(
        [
            _row(
                case_id="case_c", fact_key="device_model", value="WH-XYZ",
                source_type="structured_document_parse", source_ref="doc2", entity_scope="document",
                document_id="doc2", metadata={"evidence_ref": {"source_type": "document", "source_id": "doc2", "page": 3}},
            )
        ]
    )
    active = [f for f in store.fetch_active_facts_for_case("case_c") if f.get("fact_key") == "device_model"]
    assert len(active) == 1
    refs = (active[0].get("metadata") or {}).get("evidence_refs") or []
    source_ids = {str(r.get("source_id") or r.get("document_id")) for r in refs}
    assert "doc1" in source_ids
    assert "doc2" in source_ids


def test_same_value_append_merge_is_idempotent() -> None:
    store = _store()
    row = _row(
        case_id="case_c", fact_key="device_model", value="WH-XYZ",
        source_type="structured_document_parse", source_ref="doc1", entity_scope="document",
        document_id="doc1", metadata={"evidence_ref": {"source_type": "document", "source_id": "doc1"}},
    )
    store.append_facts_with_supersession([row])
    for _ in range(3):
        store.append_facts_with_supersession([dict(row)])
    active = [f for f in store.fetch_active_facts_for_case("case_c") if f.get("fact_key") == "device_model"]
    assert len(active) == 1
    refs = (active[0].get("metadata") or {}).get("evidence_refs") or []
    assert len(refs) <= 2


def test_duplicate_support_does_not_inflate_truth() -> None:
    store = _store()
    for i in range(3):
        store.replace_message_facts(
            message_id=f"m{i}",
            rows=[
                _row(
                    case_id="case_c", fact_key="heated_area_m2", value="120",
                    source_type="gmail_message", source_ref=f"m{i}", entity_scope="customer",
                    message_id=f"m{i}",
                )
            ],
        )
    active, conflicts = split_conflicting_facts(store.fetch_facts_for_case("case_c"))
    values = {str(f.get("normalized_value")) for f in active if f.get("fact_key") == "heated_area_m2"}
    assert values == {"120"}
    assert not conflicts


def test_derived_claim_keeps_origin_after_persist_and_reload() -> None:
    store = _store()
    store.append_facts_with_supersession(
        [
            _row(
                case_id="case_c", fact_key="device_fault_cause", value="pompa obiegowa",
                source_type="inference", source_ref="llm", entity_scope="customer",
                metadata={"source_origin": "DERIVED", "evidence_authority": "DERIVED_LLM_CLAIM", "instruction_authority": "NONE"},
            )
        ]
    )
    store2 = InMemoryMailboxMemoryStore()
    store2.facts = dict(store.facts)
    store2.cases = dict(store.cases)
    active = store2.fetch_active_facts_for_case("case_c")
    row = [f for f in active if f.get("fact_key") == "device_fault_cause"][0]
    meta = row.get("metadata") or {}
    assert meta.get("source_origin") == "DERIVED"
    assert meta.get("evidence_authority") == "DERIVED_LLM_CLAIM"
    assert meta.get("instruction_authority") == "NONE"


def test_quoted_content_origin_is_preserved_not_operator() -> None:
    store = _store()
    store.append_facts_with_supersession(
        [
            _row(
                case_id="case_c", fact_key="device_model", value="WH-OLD",
                source_type="gmail_message", source_ref="m1", entity_scope="customer",
                metadata={"source_origin": "QUOTED_CONTENT", "evidence_authority": "CUSTOMER_STATEMENT", "instruction_authority": "NONE"},
            )
        ]
    )
    active = store.fetch_active_facts_for_case("case_c")
    row = [f for f in active if f.get("fact_key") == "device_model"][0]
    meta = row.get("metadata") or {}
    assert meta.get("source_origin") == "QUOTED_CONTENT"
    assert meta.get("instruction_authority") == "NONE"


def test_merge_fact_evidence_helper_dedup_and_cap() -> None:
    meta = {"evidence_ref": {"source_type": "document", "source_id": "doc1"}}
    out1 = merge_fact_evidence(meta, {"document_id": "doc2", "source_type": "structured_document_parse"})
    out2 = merge_fact_evidence(out1, {"document_id": "doc2", "source_type": "structured_document_parse"})
    assert len(out2.get("evidence_refs") or []) == len(out1.get("evidence_refs") or [])
    assert row_evidence_refs({"source_type": "x", "source_ref": "r"})
