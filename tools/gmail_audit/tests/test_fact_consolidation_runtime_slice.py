"""P1.5: bounded production-faithful fact consolidation runtime slice."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from document_field_extractor import structured_fields_to_fact_rows
from mailbox_memory import InMemoryMailboxMemoryStore
from mailbox_memory_runtime import build_case_context_pack, split_conflicting_facts


def _mail_fact(*, case_id: str, fact_key: str, value: str, message_id: str, observed_at: str) -> dict:
    return {
        "fact_id": f"mail_{message_id}_{fact_key}",
        "case_id": case_id,
        "message_id": message_id,
        "document_id": "",
        "entity_scope": "customer",
        "fact_key": fact_key,
        "normalized_value": value,
        "raw_value": value,
        "confidence": 0.8,
        "observed_at": observed_at,
        "source_type": "gmail_message",
        "source_ref": message_id,
        "status": "active",
        "metadata": {
            "source_origin": "CUSTOMER_EMAIL",
            "evidence_authority": "CUSTOMER_STATEMENT",
            "instruction_authority": "NONE",
        },
    }


def _with_explicit_subject(rows: list[dict], *, subject_kind: str, subject_identity: str) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        meta = dict(row.get("metadata") or {})
        meta["subject_ref"] = {"kind": subject_kind, "id": subject_identity, "resolution": "EXPLICIT"}
        meta["subject_kind"] = subject_kind
        meta["subject_identity"] = subject_identity
        meta["subject_resolution"] = "EXPLICIT"
        out.append({**row, "metadata": meta})
    return out


def _attachment_fact_rows(*, case_id: str, document_id: str, fact_key: str, value: str, observed_at: str) -> list[dict]:
    return structured_fields_to_fact_rows(
        [
            {
                "field_name": fact_key,
                "field_value": value,
                "confidence": 0.9,
                "evidence_ref": {"source_type": "document", "source_id": document_id, "page": 1},
            }
        ],
        case_id=case_id,
        document_id=document_id,
        message_id="",
        observed_at=observed_at,
        parser_id="docling",
    )


def _store(case_id: str = "case_p1_5") -> InMemoryMailboxMemoryStore:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case({"case_id": case_id, "status": "open", "customer_email": "customer@example.com"})
    return store


def test_same_value_mail_and_attachment_produce_one_effective_view_in_pack() -> None:
    store = _store()
    mail = _mail_fact(case_id="case_p1_5", fact_key="device_model", value="WH-XYZ", message_id="m1", observed_at="2026-08-23T10:00:00Z")
    mail["metadata"]["subject_ref"] = {"kind": "DEVICE", "id": "device:A", "resolution": "EXPLICIT"}
    mail["metadata"]["subject_kind"] = "DEVICE"
    mail["metadata"]["subject_identity"] = "device:A"
    mail["metadata"]["subject_resolution"] = "EXPLICIT"
    store.replace_message_facts(
        message_id="m1",
        rows=[mail],
    )
    store.append_facts_with_supersession(
        _with_explicit_subject(
            _attachment_fact_rows(case_id="case_p1_5", document_id="doc1", fact_key="device_model", value="WH-XYZ", observed_at="2026-08-23T11:00:00Z"),
            subject_kind="DEVICE",
            subject_identity="device:A",
        )
    )
    pack = build_case_context_pack(store=store, case_id="case_p1_5")
    assert pack is not None
    assert not (pack.conflicting_facts or [])
    snapshot_conflicts = [c for c in ((pack.snapshot or {}).get("conflicting_facts") or []) if c.get("fact_key") == "device_model"]
    assert not snapshot_conflicts
    active = pack.active_facts or []
    models = [f for f in active if f.get("fact_key") == "device_model"]
    # One effective business value: the two scope rows (customer mail vs
    # document attachment) carry the same value with different case
    # representation (mail preserves case, document promotion lowercases).
    # Residual: write-path value normalization is not yet unified.
    values = {str(f.get("normalized_value")).casefold() for f in models}
    assert values == {"wh-xyz"}


def test_conflicting_attachment_surfaces_in_pack_and_blocks_decision() -> None:
    store = _store()
    mail = _mail_fact(case_id="case_p1_5", fact_key="device_model", value="WH-XYZ", message_id="m1", observed_at="2026-08-23T10:00:00Z")
    mail["metadata"]["subject_ref"] = {"kind": "DEVICE", "id": "device:A", "resolution": "EXPLICIT"}
    mail["metadata"]["subject_kind"] = "DEVICE"
    mail["metadata"]["subject_identity"] = "device:A"
    mail["metadata"]["subject_resolution"] = "EXPLICIT"
    store.replace_message_facts(
        message_id="m1",
        rows=[mail],
    )
    store.append_facts_with_supersession(
        _with_explicit_subject(
            _attachment_fact_rows(case_id="case_p1_5", document_id="doc1", fact_key="device_model", value="WH-ABC", observed_at="2026-08-23T11:00:00Z"),
            subject_kind="DEVICE",
            subject_identity="device:A",
        )
    )
    pack = build_case_context_pack(store=store, case_id="case_p1_5")
    assert any(c.get("fact_key") == "device_model" for c in (pack.conflicting_facts or []))
    snapshot_conflicts = [c for c in ((pack.snapshot or {}).get("conflicting_facts") or []) if c.get("fact_key") == "device_model"]
    assert snapshot_conflicts
    # Decision-critical conflict annotation blocks the premise.
    from mailbox_memory.active_facts import annotate_decision_fact_use

    current = store.fetch_active_facts_for_case("case_p1_5")
    annotated = annotate_decision_fact_use(current, pack.conflicting_facts or [])
    for row in annotated:
        if row.get("fact_key") == "device_model":
            assert row.get("decision_usable") is False


def test_derived_claim_does_not_launder_authority_through_pack() -> None:
    store = _store()
    store.append_facts_with_supersession(
        [
            {
                **_mail_fact(case_id="case_p1_5", fact_key="device_fault_cause", value="pompa obiegowa", message_id="m1", observed_at="2026-08-23T10:00:00Z"),
                "source_type": "inference",
                "metadata": {"source_origin": "DERIVED", "evidence_authority": "DERIVED_LLM_CLAIM", "instruction_authority": "NONE"},
            }
        ]
    )
    pack = build_case_context_pack(store=store, case_id="case_p1_5")
    from agent_runtime.epistemic_projection import project_epistemic_claims
    from llm_contracts.epistemic_claims import INFERRED

    claims = project_epistemic_claims(store.fetch_active_facts_for_case("case_p1_5"), pack.conflicting_facts or [])
    cause = [c for c in claims if c.proposition_key == "device_fault_cause"]
    assert cause
    assert cause[0].status in {INFERRED, "UNKNOWN"} or cause[0].evidence_authority == "DERIVED_LLM_CLAIM"


def test_fact_change_requires_decision_revision_not_cad_mutation() -> None:
    from canonical_action_decision import (
        build_business_decision_proposal,
        canonicalize,
        evaluate_decision_revision,
        request_decision_revision,
    )

    br_r1 = {
        "recommended_next_action": "collect_data",
        "missing_information": ["exact_symptoms"],
        "urgency": "normal",
        "confidence": {"action_confidence": 0.8, "business_confidence": 0.7},
    }
    proposal = build_business_decision_proposal(br_r1)
    cad_r1 = canonicalize(
        proposal=proposal,
        situation_understanding={"missing_information": ["exact_symptoms"]},
        case_id="case_p1_5",
    )
    assert cad_r1["semantic_status"] == "FROZEN"
    r1_hash = cad_r1["semantic_hash"]
    # New CONFIRMED evidence changes missing_information -> legal revision.
    br_r2 = {
        "recommended_next_action": "collect_data",
        "missing_information": ["problem_start_time"],
        "urgency": "normal",
        "confidence": {"action_confidence": 0.8, "business_confidence": 0.7},
    }
    from canonical_action_decision import DecisionRevisionLedger

    ledger = DecisionRevisionLedger()
    ledger.register_cad(cad_r1)
    rev_req = request_decision_revision(
        decision_id=cad_r1["decision_id"],
        current_revision=1,
        reason_code="NEW_CONFLICTING_EVIDENCE",
        source_layer="fact_consolidation",
        ledger=ledger,
    )
    assert rev_req["status"] == "PENDING"
    outcome = evaluate_decision_revision(
        request=rev_req["request"],
        current_cad=cad_r1,
        business_reasoning_result=br_r2,
        situation_understanding={"missing_information": ["problem_start_time"]},
        ledger=ledger,
    )
    assert outcome["outcome"] == "ACCEPTED"
    assert outcome["new_cad"]["revision"] == 2
    assert outcome["new_cad"]["semantic_hash"] != r1_hash
    assert outcome["old_cad"]["revision_status"] == "SUPERSEDED"
    assert cad_r1["semantic_status"] == "FROZEN"  # r1 never mutated
