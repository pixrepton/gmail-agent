from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from entity_linker import EntityLinker, extract_identity_hints
from mailbox_memory_store import InMemoryMailboxMemoryStore
from signal_contract import build_canonical_signal


def _store_with_case(
    *,
    case_id: str,
    case_key: str,
    metadata: dict | None = None,
    facts: list[dict] | None = None,
) -> InMemoryMailboxMemoryStore:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    store.upsert_case(
        {
            "case_id": case_id,
            "case_key": case_key,
            "thread_id": "",
            "case_family": "lead_opportunity",
            "mailbox": "test",
            "subject": "Test case",
            "status": "open",
            "customer_name": "",
            "customer_email": "",
            "metadata": metadata or {},
        }
    )
    for row in facts or []:
        store.append_fact_rows([row])
    return store


def test_deterministic_nip_match() -> None:
    nip = "5252440985"
    store = _store_with_case(
        case_id="case-nip-1",
        case_key="TOP-1001",
        metadata={"nip": nip},
    )
    signal = build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"message_id": "m1"},
        observed_at="2026-04-16T10:00:00+02:00",
        effective_at=None,
        case_key_hint=None,
        thread_key_hint=None,
        business_lane="intake_llm",
        signal_summary_pl="Test",
        payload={
            "snapshot": {},
            "intake_result_final": {"extracted_data": {"references": {}}},
            "case_hints": {"nip": nip},
        },
        artifacts={},
        revision_marker="m1",
        created_by_runtime="test",
    )
    result = EntityLinker(store).find_case(signal)
    assert result.link_status == "VERIFIED"
    assert result.phase == "deterministic"
    assert result.case_id == "case-nip-1"
    assert result.case_key == "TOP-1001"
    assert result.confidence == 1.0


def test_fuzzy_address_match() -> None:
    addr = "ul. kwiatowa 12, 00-001 warszawa"
    store = _store_with_case(
        case_id="case-addr-1",
        case_key="TOP-2002",
        facts=[
            {
                "fact_id": "f1",
                "case_id": "case-addr-1",
                "message_id": "",
                "document_id": "",
                "entity_scope": "location",
                "fact_key": "address",
                "normalized_value": addr,
                "raw_value": addr,
                "confidence": 0.9,
                "observed_at": None,
                "source_type": "test",
                "source_ref": "",
                "status": "active",
                "metadata": {},
            }
        ],
    )
    signal = build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"message_id": "m2"},
        observed_at="2026-04-16T10:00:00+02:00",
        effective_at=None,
        case_key_hint=None,
        thread_key_hint=None,
        business_lane="intake_llm",
        signal_summary_pl="Lead",
        payload={
            "snapshot": {},
            "intake_result_final": {
                "extracted_data": {
                    "entities": {"locations": [addr]},
                    "references": {},
                    "dates": [],
                    "amounts": [],
                    "deadlines": [],
                }
            },
        },
        artifacts={},
        revision_marker="m2",
        created_by_runtime="test",
    )
    result = EntityLinker(store).find_case(signal)
    assert result.link_status == "VERIFIED"
    assert result.phase == "fuzzy"
    assert result.case_id == "case-addr-1"
    assert result.confidence >= 0.85


def test_new_client_case_proposal() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    signal = build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"message_id": "m3"},
        observed_at="2026-04-16T10:00:00+02:00",
        effective_at=None,
        case_key_hint=None,
        thread_key_hint=None,
        business_lane="intake_llm",
        signal_summary_pl="Nowy lead",
        payload={
            "snapshot": {},
            "intake_result_final": {
                "extracted_data": {
                    "entities": {"organizations": ["Nieznana Firma XYZ"]},
                    "references": {},
                    "dates": [],
                    "amounts": [],
                    "deadlines": [],
                }
            },
        },
        artifacts={},
        revision_marker="m3",
        created_by_runtime="test",
    )
    result = EntityLinker(store).find_case(signal)
    assert result.link_status == "CASE_PROPOSAL"
    assert result.case_proposal.get("kind") == "new_case"


def test_extract_identity_hints_smoke() -> None:
    hints = extract_identity_hints(
        {
            "intake_result_final": {
                "extracted_data": {
                    "references": {"invoice_numbers": ["FV/2026/01"]},
                }
            }
        }
    )
    assert "FV/2026/01".upper().replace(" ", "") in "".join(hints.get("invoice_id", []))
