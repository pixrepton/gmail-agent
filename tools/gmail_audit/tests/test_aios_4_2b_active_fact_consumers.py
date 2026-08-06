"""AI-OS 4.2b: HIGH current-fact consumers must not surface superseded rows."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tools import handlers as handlers_mod
from drive_ingest_runtime import collect_fact_values, first_fact_value
from entity_linker import EntityLinker
from mailbox_memory.active_facts import fetch_current_facts_for_case
from mailbox_memory.inmemory import InMemoryMailboxMemoryStore
from signal_contract import build_canonical_signal
from similar_cases_precedent import _active_fact_keys, fetch_similar_case_precedent_refs


def _nip_pair(*, case_id: str, old_nip: str, new_nip: str) -> list[dict]:
    return [
        {
            "fact_id": "fact_nip_old",
            "case_id": case_id,
            "message_id": "msg1",
            "document_id": "doc1",
            "entity_scope": "case",
            "fact_key": "nip",
            "normalized_value": old_nip,
            "raw_value": old_nip,
            "confidence": 0.99,
            "observed_at": "2026-08-03T08:00:00Z",
            "source_type": "document_extraction",
            "source_ref": "doc:doc1",
            "status": "superseded",
            "metadata": {},
        },
        {
            "fact_id": "fact_nip_new",
            "case_id": case_id,
            "message_id": "msg2",
            "document_id": "doc2",
            "entity_scope": "case",
            "fact_key": "nip",
            "normalized_value": new_nip,
            "raw_value": new_nip,
            "confidence": 0.55,
            "observed_at": "2026-08-03T09:00:00Z",
            "source_type": "document_extraction",
            "source_ref": "doc:doc2",
            "status": "active",
            "metadata": {},
        },
    ]


def test_fetch_current_facts_for_case_prefers_active_api() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    case_id = "case_42b_active_api"
    store.upsert_case(
        {
            "case_id": case_id,
            "case_key": "TOP-42B",
            "case_family": "lead_opportunity",
            "mailbox": "test",
            "subject": "4.2b",
            "status": "open",
            "customer_name": "",
            "customer_email": "",
            "metadata": {},
        }
    )
    store.facts["seed"] = _nip_pair(case_id=case_id, old_nip="1111111111", new_nip="2222222222")

    active = fetch_current_facts_for_case(store, case_id)
    assert len(active) == 1
    assert active[0]["normalized_value"] == "2222222222"
    assert all(str(row.get("status") or "active") != "superseded" for row in active)

    all_rows = store.fetch_facts_for_case(case_id)
    assert len(all_rows) == 2


def test_entity_linker_ignores_superseded_nip_fact() -> None:
    case_id = "case_42b_linker"
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    store.upsert_case(
        {
            "case_id": case_id,
            "case_key": "TOP-42B-LINK",
            "case_family": "lead_opportunity",
            "mailbox": "test",
            "subject": "Linker",
            "status": "open",
            "customer_name": "",
            "customer_email": "",
            "metadata": {},
        }
    )
    store.facts["seed"] = _nip_pair(case_id=case_id, old_nip="5252440985", new_nip="1111111111")

    signal = build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"message_id": "m-42b"},
        observed_at="2026-08-03T10:00:00+02:00",
        effective_at=None,
        case_key_hint=None,
        thread_key_hint=None,
        business_lane="intake_llm",
        signal_summary_pl="Test",
        payload={
            "snapshot": {},
            "intake_result_final": {"extracted_data": {"references": {}}},
            # Hint matches only the superseded NIP — must NOT verify.
            "case_hints": {"nip": "5252440985"},
        },
        artifacts={},
        revision_marker="m-42b",
        created_by_runtime="test",
    )
    result = EntityLinker(store).find_case(signal)
    assert result.link_status != "VERIFIED"
    assert result.case_id != case_id


def test_invoice_fields_ignore_superseded_seller_nip(monkeypatch) -> None:
    monkeypatch.setenv("TOPINSTAL_OWN_NIP", "1234567890")
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    case_id = "case_42b_invoice"
    store.upsert_case(
        {
            "case_id": case_id,
            "case_key": "TOP-42B-INV",
            "case_family": "accounting",
            "mailbox": "test",
            "subject": "FV",
            "status": "open",
            "customer_name": "",
            "customer_email": "",
            "metadata": {},
        }
    )
    # Superseded row has own NIP as seller (would wrongly force sprzedaż); active does not.
    store.facts["seed"] = [
        {
            "fact_id": "inv_old",
            "case_id": case_id,
            "message_id": "m1",
            "document_id": "d1",
            "entity_scope": "case",
            "fact_key": "seller_nip",
            "normalized_value": "1234567890",
            "raw_value": "1234567890",
            "confidence": 0.99,
            "observed_at": "2026-08-03T08:00:00Z",
            "source_type": "document_extraction",
            "source_ref": "doc:d1",
            "status": "superseded",
            "metadata": {},
        },
        {
            "fact_id": "inv_new",
            "case_id": case_id,
            "message_id": "m2",
            "document_id": "d2",
            "entity_scope": "case",
            "fact_key": "seller_nip",
            "normalized_value": "9999999999",
            "raw_value": "9999999999",
            "confidence": 0.5,
            "observed_at": "2026-08-03T09:00:00Z",
            "source_type": "document_extraction",
            "source_ref": "doc:d2",
            "status": "active",
            "metadata": {},
        },
        {
            "fact_id": "inv_buyer",
            "case_id": case_id,
            "message_id": "m2",
            "document_id": "d2",
            "entity_scope": "case",
            "fact_key": "buyer_nip",
            "normalized_value": "1234567890",
            "raw_value": "1234567890",
            "confidence": 0.5,
            "observed_at": "2026-08-03T09:00:00Z",
            "source_type": "document_extraction",
            "source_ref": "doc:d2",
            "status": "active",
            "metadata": {},
        },
    ]
    fields = handlers_mod._fetch_invoice_fields(store, case_id)
    assert fields.get("seller_nip") == "9999999999"
    assert fields.get("buyer_nip") == "1234567890"

    snap = build_initial_snapshot(case_id=case_id, engagement_id="eng_42b", trace_id="t42b")
    ctx = ToolExecutionContext(
        snapshot=snap,
        settings=object(),
        mailbox_store=store,
        signal_payload={"subject": "Faktura", "body_text": "od dostawcy", "business_area": "finance"},
    )
    assert handlers_mod._invoice_direction_refine(ctx, "ksiegowosc") == "faktura_zakup"


def test_drive_first_fact_value_skips_superseded() -> None:
    facts = [
        {
            "fact_key": "customer_name",
            "normalized_value": "Stary Klient",
            "confidence": 0.99,
            "status": "superseded",
        },
        {
            "fact_key": "customer_name",
            "normalized_value": "Nowy Klient",
            "confidence": 0.4,
            "status": "active",
        },
    ]
    assert first_fact_value(facts, "customer_name") == "Nowy Klient"
    assert collect_fact_values(facts, {"customer_name"}) == ["Nowy Klient"]


def test_similar_precedent_uses_active_keys_only() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    case_id = "case_42b_prec"
    store.upsert_case(
        {
            "case_id": case_id,
            "case_key": "TOP-42B-PREC",
            "case_family": "lead_opportunity",
            "mailbox": "test",
            "subject": "Precedent source",
            "status": "open",
            "customer_name": "",
            "customer_email": "",
            "metadata": {},
        }
    )
    store.facts["seed"] = [
        {
            "fact_id": "area_old",
            "case_id": case_id,
            "message_id": "m1",
            "document_id": "d1",
            "entity_scope": "case",
            "fact_key": "heated_area_m2",
            "normalized_value": "120",
            "raw_value": "120",
            "confidence": 0.9,
            "observed_at": "2026-08-03T08:00:00Z",
            "source_type": "document_extraction",
            "source_ref": "doc:d1",
            "status": "superseded",
            "metadata": {},
        },
        {
            "fact_id": "city_live",
            "case_id": case_id,
            "message_id": "m2",
            "document_id": "d2",
            "entity_scope": "case",
            "fact_key": "city",
            "normalized_value": "Radlin",
            "raw_value": "Radlin",
            "confidence": 0.8,
            "observed_at": "2026-08-03T09:00:00Z",
            "source_type": "document_extraction",
            "source_ref": "doc:d2",
            "status": "active",
            "metadata": {},
        },
    ]
    keys = _active_fact_keys(fetch_current_facts_for_case(store, case_id))
    assert keys == {"city"}
    # No resolved peers → empty refs, but must not crash / invent overlap from superseded key.
    assert fetch_similar_case_precedent_refs(store, case_id=case_id, limit=3) == []
