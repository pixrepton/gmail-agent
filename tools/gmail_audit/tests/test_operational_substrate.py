"""Scenario tests for the 4 operational substrate layers: attachment, thread, event, confidence."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from action_planner import plan_actions
from attachment_intelligence import build_attachment_intelligence
from case_intelligence import build_case_intelligence
from confidence_review import apply_confidence_to_intelligence, build_confidence_domains, route_review
from event_memory import EventLog, emit_case_intelligence, emit_desk_note_event, emit_feedback_event, emit_signal_received
from intake_schema import validate_business_reasoning_result, validate_case_link_result, validate_intake_result, validate_reply_draft_result
from tests.fixture_helpers import build_fixture_intake_candidate, build_fixture_snapshot, run_fixture
from thread_memory import build_thread_memory


class AttachmentIntelligenceTests(unittest.TestCase):
    def test_technical_pdf_is_classified_and_summarized(self) -> None:
        snapshot = build_fixture_snapshot({
            "mailbox": "ops@topinstal.local",
            "source_message": {
                "message_id": "att-pdf-001",
                "thread_id": "att-thread-001",
                "date": "2026-04-01T09:00:00+02:00",
                "from": "supplier@example.com",
                "to": ["ops@topinstal.local"],
                "subject": "Karta katalogowa pompy ciepła",
                "snippet": "W załączniku karta.",
                "body": "Przesyłam kartę katalogową pompy ciepła XYZ-2000.",
                "labels": ["INBOX"],
                "attachment_names": ["Karta_katalogowa_XYZ2000.pdf"],
                "has_attachment": True,
            },
            "context_messages": [],
        })
        att = build_attachment_intelligence(snapshot)
        self.assertEqual(att["attachment_count"], 1)
        self.assertEqual(att["attachments"][0]["attachment_business_type"], "product_datasheet")
        self.assertIn("karta", att["attachments"][0]["attachment_summary_pl"].lower())
        self.assertEqual(att["attachments"][0]["case_relevance"], "significant")

    def test_invoice_attachment_raises_financial_risk(self) -> None:
        snapshot = build_fixture_snapshot({
            "mailbox": "ops@topinstal.local",
            "source_message": {
                "message_id": "att-inv-001",
                "thread_id": "att-thread-002",
                "date": "2026-04-02T10:00:00+02:00",
                "from": "finance@example.com",
                "to": ["ops@topinstal.local"],
                "subject": "Faktura VAT 04/2026",
                "snippet": "Faktura w załączniku.",
                "body": "Przesyłam fakturę VAT za usługi serwisowe.",
                "labels": ["INBOX"],
                "attachment_names": ["Faktura_VAT_042026.pdf"],
                "has_attachment": True,
            },
            "context_messages": [],
        })
        att = build_attachment_intelligence(snapshot)
        self.assertEqual(att["attachments"][0]["attachment_business_type"], "invoice")
        self.assertIn("financial_document_present", att["combined_risk_flags"])

    def test_unknown_attachment_gets_review_needed(self) -> None:
        snapshot = build_fixture_snapshot({
            "mailbox": "ops@topinstal.local",
            "source_message": {
                "message_id": "att-unk-001",
                "thread_id": "att-thread-003",
                "date": "2026-04-03T08:00:00+02:00",
                "from": "someone@example.com",
                "to": ["ops@topinstal.local"],
                "subject": "Dane",
                "snippet": "Dane w załączniku.",
                "body": "Proszę zobaczyć załącznik.",
                "labels": ["INBOX"],
                "attachment_names": ["data.xyz"],
                "has_attachment": True,
            },
            "context_messages": [],
        })
        att = build_attachment_intelligence(snapshot)
        self.assertEqual(att["attachments"][0]["attachment_business_type"], "unknown")
        self.assertEqual(att["attachments"][0]["case_relevance"], "review_needed")

    def test_no_attachments_returns_empty_summary(self) -> None:
        snapshot = build_fixture_snapshot({
            "mailbox": "ops@topinstal.local",
            "source_message": {
                "message_id": "att-none-001",
                "thread_id": "att-thread-004",
                "date": "2026-04-04T09:00:00+02:00",
                "from": "someone@example.com",
                "to": ["ops@topinstal.local"],
                "subject": "Pytanie",
                "snippet": "Pytanie bez załączników.",
                "body": "Czy możecie przygotować wycenę?",
                "labels": ["INBOX"],
            },
            "context_messages": [],
        })
        att = build_attachment_intelligence(snapshot)
        self.assertEqual(att["attachment_count"], 0)
        self.assertFalse(att["has_significant_attachments"])

    def test_delivery_confirmation_attachment(self) -> None:
        snapshot = build_fixture_snapshot({
            "mailbox": "ops@topinstal.local",
            "source_message": {
                "message_id": "att-del-001",
                "thread_id": "att-thread-005",
                "date": "2026-04-05T11:00:00+02:00",
                "from": "logistics@example.com",
                "to": ["ops@topinstal.local"],
                "subject": "Potwierdzenie dostawy",
                "snippet": "Potwierdzenie odbioru w załączniku.",
                "body": "W załączniku potwierdzenie odbioru przesyłki.",
                "labels": ["INBOX"],
                "attachment_names": ["Potwierdzenie_dostawy_2026.pdf"],
                "has_attachment": True,
            },
            "context_messages": [],
        })
        att = build_attachment_intelligence(snapshot)
        self.assertEqual(att["attachments"][0]["attachment_business_type"], "delivery_confirmation")
        self.assertIn("logistics_evidence_present", att["combined_risk_flags"])


class ThreadMemoryTests(unittest.TestCase):
    def test_thread_with_unanswered_question(self) -> None:
        snapshot = build_fixture_snapshot({
            "mailbox": "ops@topinstal.local",
            "source_message": {
                "message_id": "thr-q-001",
                "thread_id": "thr-thread-001",
                "date": "2026-04-02T10:00:00+02:00",
                "from": "client@example.com",
                "to": ["ops@topinstal.local"],
                "subject": "Pytanie o termin",
                "snippet": "Kiedy będzie montaż?",
                "body": "Kiedy możecie zrobić montaż? Czy termin jest już ustalony?",
                "labels": ["INBOX"],
            },
            "context_messages": [],
        })
        thread = build_thread_memory(snapshot)
        self.assertTrue(thread["has_unanswered_question"])
        self.assertGreaterEqual(len(thread["unresolved_questions"]), 1)

    def test_commitment_detected_in_thread(self) -> None:
        snapshot = build_fixture_snapshot({
            "mailbox": "ops@topinstal.local",
            "source_message": {
                "message_id": "thr-c-001",
                "thread_id": "thr-thread-002",
                "date": "2026-04-03T09:00:00+02:00",
                "from": "ops@topinstal.local",
                "to": ["client@example.com"],
                "subject": "Re: Termin",
                "snippet": "Dam znać do piątku.",
                "body": "Dzień dobry, dam znac do piątku z dokładnym terminem montażu.",
                "labels": ["SENT"],
            },
            "context_messages": [],
        })
        thread = build_thread_memory(snapshot)
        self.assertTrue(thread["has_open_commitment"])
        self.assertGreaterEqual(len(thread["commitments_made"]), 1)

    def test_customer_response_after_silence_changes_state(self) -> None:
        snapshot = build_fixture_snapshot({
            "mailbox": "ops@topinstal.local",
            "source_message": {
                "message_id": "thr-s-001",
                "thread_id": "thr-thread-003",
                "date": "2026-04-05T14:00:00+02:00",
                "from": "client@example.com",
                "to": ["ops@topinstal.local"],
                "subject": "Re: Wycena pompy ciepła",
                "snippet": "Wracam do sprawy.",
                "body": "Wracam do sprawy po dłuższej przerwie. Proszę o aktualną wycenę.",
                "labels": ["INBOX"],
            },
            "context_messages": [
                {
                    "message_id": "thr-s-ctx-001",
                    "thread_id": "thr-thread-003",
                    "date": "2026-03-20T10:00:00+02:00",
                    "from": "ops@topinstal.local",
                    "to": ["client@example.com"],
                    "subject": "Wycena pompy ciepła",
                    "snippet": "Wysłano wycenę.",
                    "body": "Dzień dobry, przesyłam wycenę pompy ciepła. Dam znac z terminem.",
                    "labels": ["SENT"],
                }
            ],
        })
        thread = build_thread_memory(snapshot)
        self.assertEqual(thread["message_count"], 2)
        self.assertTrue(thread["last_customer_action"])

    def test_mail_without_new_info_keeps_existing_state(self) -> None:
        snapshot = build_fixture_snapshot({
            "mailbox": "ops@topinstal.local",
            "source_message": {
                "message_id": "thr-n-001",
                "thread_id": "thr-thread-004",
                "date": "2026-04-06T08:00:00+02:00",
                "from": "newsletter@example.com",
                "to": ["ops@topinstal.local"],
                "subject": "Newsletter techniczny",
                "snippet": "Informacje branżowe.",
                "body": "Oto najnowsze informacje z branży HVAC.",
                "labels": ["INBOX"],
            },
            "context_messages": [],
        })
        thread = build_thread_memory(snapshot)
        self.assertFalse(thread["has_unanswered_question"])
        self.assertFalse(thread["has_open_commitment"])
        self.assertEqual(thread["thread_state"], "active")


class EventMemoryTests(unittest.TestCase):
    def test_case_creation_event_sequence(self) -> None:
        log = EventLog()
        snapshot = build_fixture_snapshot({
            "mailbox": "ops@topinstal.local",
            "source_message": {
                "message_id": "evt-001",
                "thread_id": "evt-thread-001",
                "date": "2026-04-01T09:00:00+02:00",
                "from": "client@example.com",
                "to": ["ops@topinstal.local"],
                "subject": "Nowe zapytanie",
                "snippet": "Proszę o kontakt.",
                "body": "Proszę o kontakt w sprawie instalacji.",
                "labels": ["INBOX"],
            },
            "context_messages": [],
        })
        e1 = emit_signal_received(log, snapshot=snapshot, case_id="case_test_001")
        e2 = emit_case_intelligence(log, case_id="case_test_001", intelligence_result={"case_understanding": {"business_priority": "medium", "confidence_overall": 0.75}, "desk_composition": {"presence_mode": "standard"}, "lifecycle_revision": {"lifecycle_intent": "create"}})
        self.assertEqual(len(log), 2)
        self.assertEqual(e1["event_type"], "signal_received")
        self.assertEqual(e2["event_type"], "case_intelligence_generated")

    def test_feedback_event_is_recorded(self) -> None:
        log = EventLog()
        e = emit_feedback_event(log, note_id="note_001", case_id="case_001", feedback_type="trafne")
        self.assertEqual(e["event_type"], "feedback_recorded")
        self.assertEqual(e["entity_id"], "note_001")

    def test_same_logical_signal_event_keeps_stable_id(self) -> None:
        log = EventLog()
        snapshot = build_fixture_snapshot({
            "mailbox": "ops@topinstal.local",
            "source_message": {
                "message_id": "evt-stable-001",
                "thread_id": "evt-thread-stable-001",
                "date": "2026-04-01T09:00:00+02:00",
                "from": "client@example.com",
                "to": ["ops@topinstal.local"],
                "subject": "Nowe zapytanie",
                "snippet": "Proszę o kontakt.",
                "body": "Proszę o kontakt w sprawie instalacji.",
                "labels": ["INBOX"],
            },
            "context_messages": [],
        })
        first = emit_signal_received(log, snapshot=snapshot, case_id="case_evt_stable")
        second = emit_signal_received(log, snapshot=snapshot, case_id="case_evt_stable")
        self.assertEqual(first["event_id"], second["event_id"])

    def test_replay_returns_sorted_events(self) -> None:
        log = EventLog()
        emit_signal_received(log, snapshot=build_fixture_snapshot({
            "mailbox": "ops@topinstal.local",
            "source_message": {"message_id": "r-001", "thread_id": "r-t-001", "date": "2026-04-01T09:00:00+02:00", "from": "a@b.com", "to": ["ops@topinstal.local"], "subject": "A", "snippet": "", "body": "", "labels": ["INBOX"]},
            "context_messages": [],
        }), case_id="case_r")
        emit_case_intelligence(log, case_id="case_r", intelligence_result={"case_understanding": {}, "desk_composition": {}, "lifecycle_revision": {}})
        emit_desk_note_event(log, event_type="desk_note_created", note_id="note_r", case_id="case_r")
        replay = log.replay_case("case_r")
        self.assertEqual(len(replay), 3)
        types = [e["event_type"] for e in replay]
        self.assertEqual(types[0], "signal_received")


class ConfidenceReviewTests(unittest.TestCase):
    def test_weak_case_link_routes_to_review_before_merge(self) -> None:
        result = run_fixture("weak_case_link")
        intelligence = result["case_intelligence"]
        self.assertEqual(intelligence["review_routing"]["review_mode"], "review_before_merge")
        self.assertTrue(intelligence["review_routing"]["review_required"])

    def test_reference_only_is_auto_safe(self) -> None:
        result = run_fixture("reference_only_mail")
        intelligence = result["case_intelligence"]
        self.assertEqual(intelligence["review_routing"]["review_mode"], "auto_safe")
        self.assertFalse(intelligence["review_routing"]["review_required"])

    def test_urgent_review_has_formal_review_routing(self) -> None:
        result = run_fixture("urgent_service")
        intelligence = result["case_intelligence"]
        self.assertTrue(intelligence["review_routing"]["review_required"])
        self.assertIn(intelligence["review_routing"]["review_mode"], {"review_before_write", "review_before_case_create"})

    def test_new_lead_confidence_domains_exist(self) -> None:
        result = run_fixture("new_lead")
        intelligence = result["case_intelligence"]
        domains = intelligence.get("confidence_domains") or {}
        self.assertIn("confidence_case_link", domains)
        self.assertIn("confidence_attachment_extraction", domains)
        self.assertIn("confidence_thread_memory", domains)
        self.assertIn("confidence_next_action", domains)
        self.assertIn("confidence_surface_decision", domains)

    def test_strong_escalation_blocked_by_low_confidence(self) -> None:
        domains = {
            "confidence_case_link": 0.3,
            "confidence_attachment_extraction": 0.2,
            "confidence_thread_memory": 0.4,
            "confidence_next_action": 0.3,
            "confidence_missing_info": 0.4,
            "confidence_merge_split": 0.3,
            "confidence_surface_decision": 0.25,
        }
        routing = route_review(domains, intake_result={}, case_intelligence_result={})
        self.assertNotEqual(routing["review_mode"], "auto_safe")
        self.assertTrue(routing["review_required"])

    def test_high_confidence_is_auto_safe(self) -> None:
        domains = {
            "confidence_case_link": 0.9,
            "confidence_attachment_extraction": 0.9,
            "confidence_thread_memory": 0.9,
            "confidence_next_action": 0.9,
            "confidence_missing_info": 0.9,
            "confidence_merge_split": 0.9,
            "confidence_surface_decision": 0.9,
        }
        routing = route_review(domains, intake_result={}, case_intelligence_result={})
        self.assertEqual(routing["review_mode"], "auto_safe")
        self.assertTrue(routing["automation_safe"])


class SubstrateIntegrationTests(unittest.TestCase):
    def test_attachment_intelligence_reaches_case_intelligence(self) -> None:
        result = run_fixture("new_lead")
        intelligence = result["case_intelligence"]
        self.assertIn("attachment_intelligence", intelligence)
        self.assertIsInstance(intelligence["attachment_intelligence"], dict)

    def test_thread_memory_reaches_case_intelligence(self) -> None:
        result = run_fixture("new_lead")
        intelligence = result["case_intelligence"]
        self.assertIn("thread_memory", intelligence)
        self.assertIsInstance(intelligence["thread_memory"], dict)
        self.assertTrue(intelligence["thread_memory"].get("thread_id"))

    def test_confidence_domains_reach_case_intelligence(self) -> None:
        result = run_fixture("new_lead")
        intelligence = result["case_intelligence"]
        self.assertIn("confidence_domains", intelligence)
        self.assertIn("review_routing", intelligence)


if __name__ == "__main__":
    unittest.main()
