from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from ai_quality_runtime import build_ai_quality_summary
from calendar_case_linker import link_calendar_event_to_case
from document_intelligence_runtime import build_document_intelligence_result, detect_document_conflicts, document_fields_to_fact_rows
from execution_runtime import (
    approve_action_proposal,
    create_action_proposal,
    execute_action_proposal,
    reject_action_proposal,
)
from mailbox_memory_store import InMemoryMailboxMemoryStore
from operator_feedback_runtime import persist_routed_event, route_operator_payload


class AiNativeV1RuntimeTests(unittest.TestCase):
    def test_supervised_execution_set_case_status(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.upsert_case({"case_id": "case_1", "status": "new"})
        proposal = create_action_proposal(
            store,
            {
                "case_id": "case_1",
                "action_type": "set_case_status",
                "payload": {"status": "ready_to_reply"},
                "confidence": 0.8,
            },
        )
        approved = approve_action_proposal(store, proposal.proposal_id, approved_by="konrad", reason="ok")
        self.assertEqual(approved.status, "approved")
        result = execute_action_proposal(store, proposal.proposal_id, executed_by="darek", dry_run=True)
        self.assertEqual(result.execution_status, "executed")
        self.assertEqual(store.fetch_case("case_1")["status"], "ready_to_reply")

    def test_reject_requires_owner_and_persists_status(self) -> None:
        store = InMemoryMailboxMemoryStore()
        proposal = create_action_proposal(store, {"case_id": "case_2", "action_type": "prepare_reply_draft"})
        rejected = reject_action_proposal(store, proposal.proposal_id, rejected_by="darek", reason="bad source")
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(store.fetch_action_proposal(proposal.proposal_id)["decision_reason"], "bad source")

    def test_reject_replay_is_idempotent_and_keeps_single_final_event(self) -> None:
        store = InMemoryMailboxMemoryStore()
        proposal = create_action_proposal(store, {"case_id": "case_reject", "action_type": "prepare_reply_draft"})

        first = reject_action_proposal(store, proposal.proposal_id, rejected_by="darek", reason="bad source")
        second = reject_action_proposal(store, proposal.proposal_id, rejected_by="darek", reason="bad source")

        self.assertEqual(first.status, "rejected")
        self.assertEqual(second.status, "rejected")
        events = [row for row in store.fetch_events_for_case("case_reject", limit=20) if row.get("event_type") == "action_proposal_rejected"]
        self.assertEqual(len(events), 1)
        results = store.fetch_execution_results(proposal_id=proposal.proposal_id, limit=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["result_payload"].get("decision_status"), "rejected")

    def test_reject_conflicts_with_already_approved_decision(self) -> None:
        store = InMemoryMailboxMemoryStore()
        proposal = create_action_proposal(store, {"case_id": "case_conflict", "action_type": "prepare_reply_draft"})
        approve_action_proposal(store, proposal.proposal_id, approved_by="konrad", reason="ok")

        with self.assertRaisesRegex(ValueError, "cannot reject"):
            reject_action_proposal(store, proposal.proposal_id, rejected_by="darek", reason="late reject")

    def test_parallel_rejects_keep_single_final_event(self) -> None:
        store = InMemoryMailboxMemoryStore()
        proposal = create_action_proposal(store, {"case_id": "case_parallel_reject", "action_type": "prepare_reply_draft"})
        barrier = threading.Barrier(2)
        errors: list[str] = []

        def _run() -> None:
            try:
                barrier.wait(timeout=2)
                reject_action_proposal(store, proposal.proposal_id, rejected_by="darek", reason="dup")
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        t1 = threading.Thread(target=_run)
        t2 = threading.Thread(target=_run)
        t1.start()
        t2.start()
        t1.join(timeout=2)
        t2.join(timeout=2)

        self.assertEqual(errors, [])
        events = [row for row in store.fetch_events_for_case("case_parallel_reject", limit=20) if row.get("event_type") == "action_proposal_rejected"]
        self.assertEqual(len(events), 1)

    def test_calendar_linker_by_attendee_email(self) -> None:
        link = link_calendar_event_to_case(
            {"summary": "Serwis pompy", "attendees": [{"email": "client@example.com"}]},
            [{"case_id": "case_cal", "customer_email": "client@example.com", "subject": "Pompa ciepla"}],
        )
        self.assertEqual(link["case_id"], "case_cal")
        self.assertEqual(link["link_status"], "linked")
        self.assertGreaterEqual(link["link_confidence"], 0.45)

    def test_calendar_linker_does_not_auto_link_weak_similarity(self) -> None:
        link = link_calendar_event_to_case(
            {"summary": "Pompa serwis", "attendees": []},
            [{"case_id": "case_weak", "subject": "Pompa ciepla serwis gwarancyjny"}],
        )
        self.assertEqual(link["case_id"], "")
        self.assertIn(link["link_status"], {"candidate", "no_link"})
        self.assertTrue(link["candidates"])

    def test_calendar_linker_marks_close_matches_ambiguous(self) -> None:
        link = link_calendar_event_to_case(
            {"summary": "Serwis pompa", "attendees": []},
            [
                {"case_id": "case_a", "subject": "Serwis pompa"},
                {"case_id": "case_b", "subject": "Serwis pompa"},
            ],
        )
        self.assertEqual(link["case_id"], "")
        self.assertEqual(link["link_status"], "ambiguous")
        self.assertEqual(len(link["candidates"]), 2)

    def test_document_intelligence_invoice_and_conflict(self) -> None:
        doc1 = build_document_intelligence_result(
            source_type="gmail_attachment",
            source_id="att1",
            case_id="case_doc",
            filename="Faktura FV-1.pdf",
            mime_type="application/pdf",
            text="Faktura nr FV/1/2026 NIP 123-456-78-90 Razem 1000 PLN",
            parser="pdf_text",
            parser_confidence=0.8,
        ).to_dict()
        doc2 = build_document_intelligence_result(
            source_type="drive_file",
            source_id="drv1",
            case_id="case_doc",
            filename="Faktura FV-2.pdf",
            mime_type="application/pdf",
            text="Faktura nr FV/2/2026 NIP 123-456-78-90 Razem 1200 PLN",
            parser="docling",
            parser_confidence=0.9,
        ).to_dict()
        self.assertEqual(doc1["document_type"], "invoice")
        self.assertTrue(doc1["extracted_fields"])
        conflicts = detect_document_conflicts([doc1, doc2])
        self.assertTrue(any(item["field_name"] in {"invoice_number", "amount_total"} for item in conflicts))

    def test_document_intelligence_invoice_number_skips_vat_label(self) -> None:
        doc = build_document_intelligence_result(
            source_type="gmail_attachment",
            source_id="att-vat",
            case_id="case_doc",
            filename="FV-12-2026.pdf",
            mime_type="application/pdf",
            text=(
                "Faktura VAT FV/12/2026\n"
                "Sprzedawca: TOP-INSTAL Sp. z o.o.\n"
                "NIP: 1234567890\n"
                "Data wystawienia: 2026-04-20\n"
                "Termin platnosci: 2026-05-04\n"
                "Razem do zaplaty: 1230,00 PLN\n"
            ),
            parser="text_fixture",
            parser_confidence=0.7,
        ).to_dict()
        fields = {item["field_name"]: item["field_value"] for item in doc["extracted_fields"]}
        self.assertEqual(fields["invoice_number"], "FV/12/2026")
        self.assertEqual(fields["issue_date"], "2026-04-20")
        self.assertEqual(fields["due_date"], "2026-05-04")
        self.assertEqual(fields["amount_total"], "1230,00")

    def test_document_fields_promote_to_tentative_fact_rows_with_provenance(self) -> None:
        doc = build_document_intelligence_result(
            source_type="gmail_attachment",
            source_id="att-fact",
            case_id="case_doc",
            filename="FV-99.pdf",
            mime_type="application/pdf",
            text="Faktura nr FV/99/2026 Data wystawienia: 2026-04-20 Razem: 900 PLN",
        ).to_dict()
        facts = document_fields_to_fact_rows(doc, min_confidence=0.7)
        by_key = {row["fact_key"]: row for row in facts}
        self.assertIn("invoice_number", by_key)
        self.assertEqual(by_key["invoice_number"]["case_id"], "case_doc")
        self.assertEqual(by_key["invoice_number"]["entity_scope"], "document")
        self.assertEqual(by_key["invoice_number"]["source_type"], "document_intelligence")
        self.assertTrue(by_key["invoice_number"]["source_ref"])
        self.assertTrue(by_key["amount_total"]["metadata"]["tentative"])

    def test_document_intelligence_offer_and_protocol_fields(self) -> None:
        offer = build_document_intelligence_result(
            source_type="drive_file",
            source_id="offer-1",
            filename="Oferta_PC_2026.pdf",
            mime_type="application/pdf",
            text=(
                "Oferta\n"
                "Oferent: Dostawca ABC\n"
                "Model: HP-9000\n"
                "Cena: 24500 PLN\n"
                "Wazna do: 2026-06-30\n"
                "Zakres prac: Dostawa i montaz pompy ciepla z uruchomieniem\n"
                "Warunki: Platnosc po odbiorze, termin realizacji 14 dni\n"
            ),
        ).to_dict()
        protocol = build_document_intelligence_result(
            source_type="gmail_attachment",
            source_id="protocol-1",
            filename="protokol-serwisowy.pdf",
            mime_type="application/pdf",
            text=(
                "Protokol serwisowy\n"
                "Adres: ul. Testowa 12, Krakow\n"
                "Urzadzenie: Panasonic Aquarea 9kW\n"
                "Data serwisu: 2026-04-22\n"
                "Wykonano: Czyszczenie filtrow i kontrola cisnienia ukladu\n"
                "Zalecenia: Wymienic filtr przy nastepnej wizycie\n"
            ),
        ).to_dict()
        offer_fields = {item["field_name"]: item["field_value"] for item in offer["extracted_fields"]}
        protocol_fields = {item["field_name"]: item["field_value"] for item in protocol["extracted_fields"]}
        self.assertEqual(offer["document_type"], "offer")
        self.assertEqual(offer_fields["product_model"], "HP-9000")
        self.assertEqual(offer_fields["validity_date"], "2026-06-30")
        self.assertEqual(protocol["document_type"], "protocol")
        self.assertEqual(protocol_fields["service_date"], "2026-04-22")
        self.assertIn("Testowa", protocol_fields["address"])

    def test_ai_quality_summary_from_proposals_and_feedback(self) -> None:
        store = InMemoryMailboxMemoryStore()
        proposal = create_action_proposal(store, {"case_id": "case_q", "action_type": "prepare_reply_draft"})
        approve_action_proposal(store, proposal.proposal_id, approved_by="konrad")
        domain, event = route_operator_payload(
            {
                "case_id": "case_q",
                "target_type": "action_proposal",
                "target_id": proposal.proposal_id,
                "rating": "accurate",
                "tags": ["wrong_priority"],
                "operator_id": "konrad",
            }
        )
        persist_routed_event(store, domain, event)
        summary = build_ai_quality_summary(store)
        self.assertEqual(summary["total_ai_suggestions"], 1)
        self.assertEqual(summary["accepted_suggestions"], 1)
        self.assertEqual(summary["feedback_count"], 1)
        self.assertEqual(summary["wrong_priority_count"], 1)
        self.assertEqual(summary["by_target_type"]["action_proposal"]["feedback_count"], 1)
        self.assertEqual(summary["by_action_type"]["prepare_reply_draft"]["accepted"], 1)
        self.assertEqual(summary["by_rating"]["accurate"], 1)

    def test_document_intelligence_promote_facts_env_default_on(self) -> None:
        from config import document_intelligence_promote_facts_enabled

        base = {k: v for k, v in os.environ.items()}
        base.pop("DOCUMENT_INTELLIGENCE_PROMOTE_FACTS", None)
        with mock.patch.dict(os.environ, base, clear=True):
            self.assertTrue(document_intelligence_promote_facts_enabled())
        off = {**base, "DOCUMENT_INTELLIGENCE_PROMOTE_FACTS": "0"}
        with mock.patch.dict(os.environ, off, clear=True):
            self.assertFalse(document_intelligence_promote_facts_enabled())


if __name__ == "__main__":
    unittest.main()
