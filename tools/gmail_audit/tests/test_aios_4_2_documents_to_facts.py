"""AI-OS 4.2 (bounded): Documents → mailbox_memory_facts via supersession."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from daszek_v3_operational_feed import assemble_mailbox_pack_dict
from document_intelligence_runtime import (
    build_document_intelligence_result,
    document_fields_to_fact_rows,
    promote_document_intelligence_facts,
)
from mailbox_memory.inmemory import InMemoryMailboxMemoryStore
from mailbox_memory_runtime import build_case_context_pack, split_conflicting_facts


INVOICE_TEXT_V1 = (
    "Faktura VAT FV/10/2026\n"
    "Sprzedawca: TOP-INSTAL Sp. z o.o.\n"
    "NIP: 1234567890\n"
    "Data wystawienia: 2026-04-20\n"
    "Termin platnosci: 2026-05-04\n"
    "Razem do zaplaty: 1000,00 PLN\n"
)

INVOICE_TEXT_V2 = (
    "Faktura VAT FV/10/2026\n"
    "Sprzedawca: TOP-INSTAL Sp. z o.o.\n"
    "NIP: 1234567890\n"
    "Data wystawienia: 2026-04-20\n"
    "Termin platnosci: 2026-05-04\n"
    "Razem do zaplaty: 1500,00 PLN\n"
)


def _invoice_doc(*, source_id: str, case_id: str, text: str, filename: str = "FV-10.pdf") -> dict:
    return build_document_intelligence_result(
        source_type="drive_file",
        source_id=source_id,
        case_id=case_id,
        filename=filename,
        mime_type="application/pdf",
        text=text,
        parser="text_fixture",
        parser_confidence=0.9,
    ).to_dict()


def test_doc_field_extract_promotes_fact_row_with_evidence_ref() -> None:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case({"case_id": "case_42_doc", "status": "open", "subject": "FV"})
    doc = _invoice_doc(source_id="drv-v1", case_id="case_42_doc", text=INVOICE_TEXT_V1)

    result = promote_document_intelligence_facts(store, doc, min_confidence=0.7)

    assert result["write_stats"]["inserted"] >= 1
    rows = result["rows"]
    assert rows
    amount = next(row for row in rows if row["fact_key"] == "amount_total")
    assert amount["normalized_value"] == "1000,00"
    assert amount["status"] == "active"
    evidence = (amount.get("metadata") or {}).get("evidence_ref") or {}
    assert evidence.get("source_id")
    assert evidence.get("excerpt")
    assert "1000" in str(evidence.get("excerpt") or "") or "Razem" in str(evidence.get("excerpt") or "")

    active = store.fetch_active_facts_for_case("case_42_doc")
    assert any(r["fact_key"] == "amount_total" and r["normalized_value"] == "1000,00" for r in active)
    assert all(str(r.get("status") or "active") != "superseded" for r in active)


def test_document_value_change_supersedes_prior_active() -> None:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case({"case_id": "case_42_super", "status": "open", "subject": "FV"})

    doc_v1 = _invoice_doc(source_id="drv-a", case_id="case_42_super", text=INVOICE_TEXT_V1, filename="FV-a.pdf")
    doc_v2 = _invoice_doc(source_id="drv-b", case_id="case_42_super", text=INVOICE_TEXT_V2, filename="FV-b.pdf")

    first = promote_document_intelligence_facts(store, doc_v1, min_confidence=0.7)
    assert first["write_stats"]["inserted"] >= 1

    second = promote_document_intelligence_facts(store, doc_v2, min_confidence=0.7)
    assert second["write_stats"]["superseded"] >= 1
    assert second["write_stats"]["inserted"] >= 1

    all_facts = store.fetch_facts_for_case("case_42_super")
    amounts = [f for f in all_facts if f.get("fact_key") == "amount_total"]
    active_amounts = [f for f in amounts if str(f.get("status") or "active") == "active"]
    superseded_amounts = [f for f in amounts if str(f.get("status") or "") == "superseded"]
    assert len(active_amounts) == 1
    assert active_amounts[0]["normalized_value"] == "1500,00"
    assert superseded_amounts
    assert any(f["normalized_value"] == "1000,00" for f in superseded_amounts)

    pack = build_case_context_pack(store=store, case_id="case_42_super")
    pack_amounts = [f for f in pack.active_facts if f.get("fact_key") == "amount_total"]
    assert len(pack_amounts) == 1
    assert pack_amounts[0]["normalized_value"] == "1500,00"


def _seed_concurrent_active_disagreement(store: InMemoryMailboxMemoryStore, rows_by_message: dict[str, list[dict]]) -> None:
    """Test-only fixture: dual-active disagreement without canonical write supersession.

    Production ``replace_message_facts`` now enforces supersession. Conflict UI tests still
    need a way to seed concurrent live values for the same logical identity.
    """
    for message_id, rows in rows_by_message.items():
        store.facts[str(message_id)] = [dict(item) for item in rows]


def test_conflict_and_superseded_audit_surface_in_operational_feed() -> None:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case({"case_id": "case_42_conflict", "status": "open", "subject": "Konflikt"})

    # Same (entity_scope, fact_key) dual actives via test-only seed (bypasses supersession).
    _seed_concurrent_active_disagreement(
        store,
        {
            "msg_a": [
                {
                    "fact_id": "fact_a",
                    "case_id": "case_42_conflict",
                    "message_id": "msg_a",
                    "document_id": "doc_a",
                    "entity_scope": "document",
                    "fact_key": "device_model",
                    "normalized_value": "model-a",
                    "raw_value": "MODEL-A",
                    "confidence": 0.9,
                    "observed_at": "2026-08-05T10:00:00Z",
                    "source_type": "document_intelligence",
                    "source_ref": "document_intelligence:doc_a",
                    "status": "active",
                    "metadata": {"evidence_ref": {"source_id": "doc_a", "excerpt": "MODEL-A"}},
                }
            ],
            "msg_b": [
                {
                    "fact_id": "fact_b",
                    "case_id": "case_42_conflict",
                    "message_id": "msg_b",
                    "document_id": "doc_b",
                    "entity_scope": "document",
                    "fact_key": "device_model",
                    "normalized_value": "model-b",
                    "raw_value": "MODEL-B",
                    "confidence": 0.85,
                    "observed_at": "2026-08-05T11:00:00Z",
                    "source_type": "document_intelligence",
                    "source_ref": "document_intelligence:doc_b",
                    "status": "active",
                    "metadata": {"evidence_ref": {"source_id": "doc_b", "excerpt": "MODEL-B"}},
                }
            ],
        },
    )

    active, conflicts = split_conflicting_facts(store.fetch_facts_for_case("case_42_conflict"))
    assert any(c.get("fact_key") == "device_model" for c in conflicts)
    assert len([f for f in active if f.get("fact_key") == "device_model"]) == 1

    feed_before = assemble_mailbox_pack_dict(store, "case_42_conflict")
    assert any(c.get("fact_key") == "device_model" for c in feed_before.get("conflicting_facts") or [])

    # Supersession path also leaves an audit trail visible to the operator pack.
    # Invoice promotion uses different fact keys, so device_model conflicts remain live.
    doc_v1 = _invoice_doc(source_id="drv-c1", case_id="case_42_conflict", text=INVOICE_TEXT_V1, filename="c1.pdf")
    doc_v2 = _invoice_doc(source_id="drv-c2", case_id="case_42_conflict", text=INVOICE_TEXT_V2, filename="c2.pdf")
    promote_document_intelligence_facts(store, doc_v1, min_confidence=0.7)
    promote_document_intelligence_facts(store, doc_v2, min_confidence=0.7)

    feed_pack = assemble_mailbox_pack_dict(store, "case_42_conflict")
    assert any(c.get("fact_key") == "device_model" for c in feed_pack.get("conflicting_facts") or [])
    superseded = feed_pack.get("superseded_facts") or []
    assert superseded
    assert any(
        str(row.get("fact_key") or "") == "amount_total" and str(row.get("normalized_value") or "") == "1000,00"
        for row in superseded
    )
    # Active projection must not reintroduce superseded amount.
    active_amounts = [
        f for f in (feed_pack.get("active_facts") or []) if f.get("fact_key") == "amount_total"
    ]
    assert len(active_amounts) == 1
    assert active_amounts[0]["normalized_value"] == "1500,00"


def test_document_fields_to_fact_rows_always_carry_evidence_ref() -> None:
    doc = _invoice_doc(source_id="drv-ev", case_id="case_42_ev", text=INVOICE_TEXT_V1)
    rows = document_fields_to_fact_rows(doc, min_confidence=0.7)
    assert rows
    for row in rows:
        evidence = (row.get("metadata") or {}).get("evidence_ref")
        assert isinstance(evidence, dict)
        assert evidence.get("source_id")
        assert evidence.get("excerpt")
