"""P1.5: conflict semantics - no winner, no latest-wins, decision_usable=false."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory import InMemoryMailboxMemoryStore
from mailbox_memory.active_facts import annotate_decision_fact_use, fetch_current_facts_for_case
from mailbox_memory_runtime import split_conflicting_facts


def _row(
    *,
    case_id: str,
    fact_key: str,
    value: str,
    entity_scope: str,
    source_type: str,
    source_ref: str,
    message_id: str = "",
    observed_at: str = "2026-08-23T10:00:00Z",
    confidence: float = 0.8,
) -> dict:
    return {
        "fact_id": f"f_{source_type}_{source_ref}_{fact_key}_{value}",
        "case_id": case_id,
        "message_id": message_id,
        "document_id": "",
        "entity_scope": entity_scope,
        "fact_key": fact_key,
        "normalized_value": value,
        "raw_value": value,
        "confidence": confidence,
        "observed_at": observed_at,
        "source_type": source_type,
        "source_ref": source_ref,
        "status": "active",
        "metadata": {},
    }


def _store() -> InMemoryMailboxMemoryStore:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case({"case_id": "case_conflict", "status": "open"})
    return store


def _conflict_store(*, x_ts: str, y_ts: str, y_confidence: float = 0.9):
    store = _store()
    store.append_facts_with_supersession(
        [
            _row(
                case_id="case_conflict", fact_key="device_model", value="WH-XYZ",
                entity_scope="customer", source_type="gmail_message", source_ref="m1", message_id="m1",
                observed_at=x_ts,
            )
        ]
    )
    store.append_facts_with_supersession(
        [
            _row(
                case_id="case_conflict", fact_key="device_model", value="WH-ABC",
                entity_scope="document", source_type="structured_document_parse", source_ref="doc1",
                observed_at=y_ts, confidence=y_confidence,
            )
        ]
    )
    return store


def test_mail_x_attachment_y_is_unresolved_conflict() -> None:
    store = _conflict_store(x_ts="2026-08-23T10:00:00Z", y_ts="2026-08-23T11:00:00Z")
    active, conflicts = split_conflicting_facts(store.fetch_facts_for_case("case_conflict"))
    device_conflicts = [c for c in conflicts if c.get("fact_key") == "device_model"]
    assert device_conflicts
    assert set(device_conflicts[0].get("values") or []) == {"WH-ABC", "WH-XYZ"}
    current = fetch_current_facts_for_case(store, "case_conflict")
    annotated = annotate_decision_fact_use(current, conflicts)
    for row in annotated:
        if row.get("fact_key") == "device_model":
            assert row.get("decision_usable") is False
            assert row.get("trust_state") == "conflicted"


def test_authoritative_vs_customer_does_not_pick_winner() -> None:
    store = _conflict_store(
        x_ts="2026-08-23T12:00:00Z",
        y_ts="2026-08-23T09:00:00Z",
        y_confidence=0.99,
    )
    _, conflicts = split_conflicting_facts(store.fetch_facts_for_case("case_conflict"))
    device_conflicts = [c for c in conflicts if c.get("fact_key") == "device_model"]
    assert device_conflicts, "authoritative document must not silently win"


def test_timestamp_permutation_keeps_same_conflict_verdict() -> None:
    a = _conflict_store(x_ts="2026-08-23T10:00:00Z", y_ts="2026-08-23T11:00:00Z")
    b = _conflict_store(x_ts="2026-08-23T11:00:00Z", y_ts="2026-08-23T10:00:00Z")
    _, ca = split_conflicting_facts(a.fetch_facts_for_case("case_conflict"))
    _, cb = split_conflicting_facts(b.fetch_facts_for_case("case_conflict"))
    ka = sorted((c.get("fact_key"), sorted(c.get("values") or [])) for c in ca)
    kb = sorted((c.get("fact_key"), sorted(c.get("values") or [])) for c in cb)
    assert ka == kb
    assert any(key == "device_model" for key, _ in ka)


def test_duplicate_evidence_does_not_inflate_or_resolve_conflict() -> None:
    store = _conflict_store(x_ts="2026-08-23T10:00:00Z", y_ts="2026-08-23T11:00:00Z")
    for _ in range(5):
        store.append_facts_with_supersession(
            [
                _row(
                    case_id="case_conflict", fact_key="device_model", value="WH-ABC",
                    entity_scope="document", source_type="structured_document_parse", source_ref="doc1",
                    observed_at="2026-08-23T11:00:00Z",
                )
            ]
        )
    _, conflicts = split_conflicting_facts(store.fetch_facts_for_case("case_conflict"))
    device_conflicts = [c for c in conflicts if c.get("fact_key") == "device_model"]
    assert device_conflicts, "count must not resolve an unresolved conflict"


def test_conflict_is_never_promoted_to_confirmed_by_p1_3_projection() -> None:
    from agent_runtime.epistemic_projection import project_epistemic_claims
    from llm_contracts.epistemic_claims import CONFLICTED

    store = _conflict_store(x_ts="2026-08-23T10:00:00Z", y_ts="2026-08-23T11:00:00Z")
    facts = store.fetch_facts_for_case("case_conflict")
    active, conflicts = split_conflicting_facts(facts)
    conflict_keys = {str(c.get("fact_key")) for c in conflicts}
    claims = project_epistemic_claims(active, conflicts)
    device_claims = [c for c in claims if c.proposition_key == "device_model"]
    assert device_claims
    assert all(c.status == CONFLICTED and c.decision_usable is False for c in device_claims)
