from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from intake_policy import OUTPUT_ORIGIN_NORMALIZED_VALID, OUTPUT_ORIGIN_REPAIRED_VALID
from intake_schema import (
    DECISION_OVER_SIGNAL_MARGIN,
    _validate_semantics,
    validate_output_with_repair,
)


def _mark_reference_supplier_candidate(*, signal_confidence: float, decision_confidence: float) -> dict:
    """Schema-shaped intake with mark_reference; tune confidences for semantic tests."""
    return {
        "schema_version": "1.0",
        "source": {
            "channel": "gmail",
            "mailbox": "biuro.topinstal@gmail.com",
            "observed_at": "2026-04-30T12:00:00+02:00",
        },
        "message": {
            "message_id": "19dd375c8c1cfc36",
            "date": "2026-04-30T11:00:00+02:00",
            "sender": "Vendor <vendor@example.com>",
            "to": ["biuro.topinstal@gmail.com"],
            "cc": [],
            "subject": "Promo",
            "snippet": "Short promo text.",
            "has_attachments": False,
            "labels": ["INBOX"],
        },
        "thread": {
            "thread_id": "19dd375c8c1cfc36",
            "thread_position": "new_thread",
            "is_reply_or_forward": False,
            "thread_summary": "Promo; context=weak",
            "linked_case_candidates": [],
        },
        "business_area": "supplier_commercial",
        "primary_signal": {
            "code": "SUPP_PROMO",
            "name": "Supplier Promotion",
            "description": "Supplier promotional content.",
            "business_significance": "low",
        },
        "secondary_signals": [],
        "case_assessment": {
            "case_family": "supplier_commercial_review",
            "is_new_case": False,
            "state_change": {"detected": False},
            "state_detected": "none",
        },
        "confidence": {
            "signal_confidence": signal_confidence,
            "case_link_confidence": 0.75,
            "decision_confidence": decision_confidence,
            "extraction_confidence": 0.8,
        },
        "decision": {
            "action": "mark_reference",
            "action_rationale": "Promotional supplier mail; keep as reference.",
        },
        "priority": "low",
        "reason": "Supplier promo; reference only.",
        "review": {"required": False, "flags": []},
        "extracted_data": {
            "entities": {"organizations": [], "people": [], "locations": [], "products": []},
            "dates": [],
            "amounts": [],
            "deadlines": [],
            "references": {
                "case_ids": [],
                "invoice_numbers": [],
                "order_numbers": [],
                "shipment_numbers": [],
                "transaction_numbers": [],
            },
            "lead_details": {},
        },
    }


class IntakeSchemaRepairTests(unittest.TestCase):
    def test_missing_primary_signal_repairs_live_schema_invalid_mail_to_review(self) -> None:
        snapshot = {
            "mailbox": "biuro.topinstal@gmail.com",
            "observed_at": "2026-04-07T02:15:16.748973+02:00",
            "thread_context_quality": "weak",
            "source_message": {
                "message_id": "19d52b9ee78be87a",
                "thread_id": "19d52b9ee78be87a",
                "date": "2026-04-03T11:43:29+02:00",
                "sender": "Allegro <powiadomienia@allegro.pl>",
                "to": ["biuro.topinstal@gmail.com"],
                "cc": [],
                "subject": "Przesyłka w drodze! Zawiera produkt ZESTAW WIERTEŁ STOPNIOWYCH 3 SZT WIERTŁO STOPNIOWE STOŻKOWE 4-35 DO METALU",
                "snippet": "Allegro Dzień dobry Topinnovations, Twoja paczka właśnie ruszyła w drogę.",
                "has_attachments": False,
                "labels": ["CATEGORY_UPDATES", "INBOX"],
            },
        }
        candidate = {
            "schema_version": "1.0",
            "source": {
                "channel": "gmail",
                "mailbox": "biuro.topinstal@gmail.com",
                "observed_at": "2026-04-07T02:15:16.748973+02:00",
            },
            "message": {
                "message_id": "19d52b9ee78be87a",
                "date": "2026-04-03T11:43:29+02:00",
                "sender": "Allegro <powiadomienia@allegro.pl>",
                "to": ["biuro.topinstal@gmail.com"],
                "cc": [],
                "subject": "Przesyłka w drodze! Zawiera produkt ZESTAW WIERTEŁ STOPNIOWYCH 3 SZT WIERTŁO STOPNIOWE STOŻKOWE 4-35 DO METALU",
                "snippet": "Allegro Dzień dobry Topinnovations, Twoja paczka właśnie ruszyła w drogę.",
                "has_attachments": False,
                "labels": ["CATEGORY_UPDATES", "INBOX"],
            },
            "thread": {
                "thread_id": "19d52b9ee78be87a",
                "thread_position": "new_thread",
                "is_reply_or_forward": False,
                "thread_summary": "Przesyłka w drodze! Zawiera produkt ZESTAW WIERTEŁ STOPNIOWYCH 3 SZT WIERTŁO STOPNIOWE STOŻKOWE 4-35 DO METALU; from Allegro ; context=weak",
                "linked_case_candidates": [],
            },
            "business_area": "procurement",
            "case_assessment": {
                "case_family": "procurement_delivery",
                "is_new_case": True,
                "state_change": {"detected": False},
                "state_detected": "none",
            },
            "confidence": {
                "signal_confidence": 0.9,
                "case_link_confidence": 0.0,
                "decision_confidence": 0.95,
                "extraction_confidence": 0.9,
            },
            "decision": {
                "action": "create_case",
                "action_rationale": "New shipment notification requires tracking of the purchased tool set delivery.",
            },
            "priority": "medium",
            "reason": "The email confirms that a recently ordered set of drill bits is in transit; a procurement case should be created to monitor receipt and update inventory.",
            "review": {"required": False, "flags": []},
            "extracted_data": {
                "entities": {
                    "organizations": ["Allegro"],
                    "people": [],
                    "locations": [],
                    "products": ["ZESTAW WIERTEŁ STOPNIOWYCH 3 SZT WIERTŁO STOPNIOWE STOŻKOWE 4-35 DO METALU"],
                },
                "dates": [{"kind": "estimated_delivery", "value": "2026-04-04"}],
                "amounts": [],
                "deadlines": [],
                "references": {
                    "case_ids": [],
                    "invoice_numbers": [],
                    "order_numbers": [],
                    "shipment_numbers": ["620999690743600435414266"],
                    "transaction_numbers": [],
                },
            },
            "secondary_signals": [],
        }

        trace = validate_output_with_repair(json.dumps(candidate, ensure_ascii=False), snapshot=snapshot)

        self.assertTrue(trace.result.is_valid)
        self.assertTrue(trace.repair_applied)
        self.assertEqual(trace.final_output_origin, OUTPUT_ORIGIN_REPAIRED_VALID)
        self.assertIn("filled_primary_signal_for_review", trace.repair_notes)
        self.assertIsNotNone(trace.result.data)
        self.assertEqual(trace.result.data["decision"]["action"], "create_case")
        self.assertTrue(trace.result.data["review"]["required"])
        self.assertEqual(trace.result.data["primary_signal"]["code"], "manual_review_required")

    def test_implausible_decision_vs_signal_confidence_semantic_error(self) -> None:
        candidate = _mark_reference_supplier_candidate(signal_confidence=0.4, decision_confidence=0.8)
        errors = _validate_semantics(candidate)
        self.assertTrue(any("implausibly higher" in e for e in errors))

    def test_repair_clamps_decision_confidence_to_signal_plus_margin_cohort_case(self) -> None:
        """Matches validator ceiling: decision_confidence <= signal_confidence + margin."""
        snapshot = {"thread_context_quality": "weak"}
        candidate = _mark_reference_supplier_candidate(signal_confidence=0.4, decision_confidence=0.8)
        trace = validate_output_with_repair(json.dumps(candidate, ensure_ascii=False), snapshot=snapshot)
        self.assertTrue(trace.repair_applied)
        self.assertTrue(trace.result.is_valid)
        self.assertEqual(trace.final_output_origin, OUTPUT_ORIGIN_REPAIRED_VALID)
        self.assertIn("clamped_decision_confidence_to_signal_plus_margin", trace.repair_notes)
        out = trace.result.data
        assert out is not None
        self.assertEqual(out["decision"]["action"], "review")
        self.assertTrue(out["review"]["required"])
        self.assertIn("downgraded_action_to_review", trace.repair_notes)
        self.assertAlmostEqual(float(out["confidence"]["signal_confidence"]), 0.4)
        self.assertAlmostEqual(float(out["confidence"]["decision_confidence"]), 0.4 + DECISION_OVER_SIGNAL_MARGIN)

    def test_repair_does_not_raise_signal_confidence(self) -> None:
        snapshot = {"thread_context_quality": "weak"}
        candidate = _mark_reference_supplier_candidate(signal_confidence=0.35, decision_confidence=0.9)
        trace = validate_output_with_repair(json.dumps(candidate, ensure_ascii=False), snapshot=snapshot)
        self.assertTrue(trace.result.is_valid)
        out = trace.result.data
        assert out is not None
        self.assertAlmostEqual(float(out["confidence"]["signal_confidence"]), 0.35)

    def test_coherent_confidence_no_repair_needed(self) -> None:
        snapshot = {"thread_context_quality": "weak"}
        candidate = _mark_reference_supplier_candidate(signal_confidence=0.9, decision_confidence=0.88)
        trace = validate_output_with_repair(json.dumps(candidate, ensure_ascii=False), snapshot=snapshot)
        self.assertFalse(trace.repair_applied)
        self.assertTrue(trace.result.is_valid)
        out = trace.result.data
        assert out is not None
        self.assertAlmostEqual(float(out["confidence"]["decision_confidence"]), 0.88)
        self.assertAlmostEqual(float(out["confidence"]["signal_confidence"]), 0.9)

    def test_cerebras_drift_shape_normalizes_without_repair(self) -> None:
        snapshot = {
            "mailbox": "biuro.topinstal@gmail.com",
            "observed_at": "2026-07-10T18:39:00+02:00",
            "thread_context_quality": "weak",
            "source_message": {
                "message_id": "19ee5e5feec89939",
                "thread_id": "19ee5e5feec89939",
                "date": "2026-07-10T10:00:00+02:00",
                "sender": "client@example.com",
                "to": ["biuro.topinstal@gmail.com"],
                "cc": [],
                "subject": "Zapytanie: dom jednorodzinny parterowy podpiwniczony (~180 mkw.)",
                "snippet": "Lead HVAC",
                "has_attachments": False,
                "labels": ["INBOX"],
            },
        }
        candidate = {
            "business_area": "sales",
            "case_assessment": {"case_family": "lead_opportunity"},
            "decision": {
                "action": "create_case",
                "priority": "medium",
                "reason": "Incoming lead request for an air-source split heat pump.",
            },
            "thread": {
                "thread_summary": "Zapytanie: dom jednorodzinny parterowy podpiwniczony (~180 mkw.)",
                "linked_case_candidates": [],
            },
            "secondary_signals": [],
            "review": {"required": False, "flags": []},
        }
        trace = validate_output_with_repair(json.dumps(candidate, ensure_ascii=False), snapshot=snapshot)
        self.assertTrue(trace.result.is_valid)
        self.assertFalse(trace.repair_applied)
        self.assertEqual(trace.final_output_origin, OUTPUT_ORIGIN_NORMALIZED_VALID)
        out = trace.result.data
        assert out is not None
        self.assertEqual(out["priority"], "medium")
        self.assertEqual(out["decision"]["action"], "create_case")
        self.assertEqual(out["primary_signal"]["code"], "lead_inquiry")
        self.assertTrue(out["case_assessment"]["is_new_case"])

    def test_cerebras_create_case_and_task_root_reason_normalizes(self) -> None:
        snapshot = {
            "mailbox": "biuro.topinstal@gmail.com",
            "observed_at": "2026-07-10T18:39:00+02:00",
            "thread_context_quality": "weak",
            "source_message": {
                "message_id": "19ee5e5feec89939",
                "thread_id": "19ee5e5feec89939",
                "date": "2026-07-10T10:00:00+02:00",
                "sender": "dozorca@cieplo.app",
                "to": ["biuro.topinstal@gmail.com"],
                "cc": [],
                "subject": "Zapytanie: dom jednorodzinny parterowy podpiwniczony (~180 mkw.)",
                "snippet": "Lead HVAC",
                "has_attachments": False,
                "labels": ["INBOX"],
            },
        }
        candidate = {
            "business_area": "sales",
            "case_assessment": {"case_family": "lead_opportunity"},
            "decision": {"action": "create_case_and_task", "priority": "medium"},
            "thread": {
                "thread_summary": "Zapytanie: dom jednorodzinny parterowy podpiwniczony (~180 mkw.)",
                "linked_case_candidates": [],
            },
            "secondary_signals": [],
            "review": {"required": False, "flags": []},
            "reason": "Incoming lead inquiry for heat-pump installation; collect missing sizing data.",
        }
        trace = validate_output_with_repair(json.dumps(candidate, ensure_ascii=False), snapshot=snapshot)
        self.assertTrue(trace.result.is_valid)
        out = trace.result.data
        assert out is not None
        self.assertEqual(out["decision"]["action"], "create_case_and_task")
        self.assertTrue(out["case_assessment"]["is_new_case"])

    def test_cerebras_nested_decision_review_is_hoisted(self) -> None:
        snapshot = {
            "mailbox": "unknown",
            "observed_at": "2026-06-20T16:38:35+00:00",
            "thread_context_quality": "weak",
            "source_message": {
                "message_id": "19ee5e5feec89939",
                "thread_id": "19ee5e5feec89939",
                "date": "2026-06-20T16:38:35+00:00",
                "sender": '"Cieplo.app" <dozorca@cieplo.app>',
                "to": ["biuro.topinstal@gmail.com"],
                "cc": [],
                "subject": "Zapytanie: dom jednorodzinny parterowy podpiwniczony (~180 mkw.) - Adamow (42-270)",
                "snippet": "Uzytkownik aplikacji CieploWlasciwie.pl prosi o oferty.",
                "has_attachments": False,
                "labels": ["IMPORTANT", "CATEGORY_UPDATES", "INBOX"],
            },
        }
        candidate = {
            "thread": {
                "thread_summary": "Zapytanie: dom jednorodzinny parterowy podpiwniczony (~180 mkw.) - Adamow (42-270)"
            },
            "business_area": "sales",
            "case_assessment": {"case_family": "lead_opportunity"},
            "decision": {
                "action": "create_case_and_task",
                "priority": "medium",
                "reason": "Received a new lead request for an air-source split heat pump.",
                "review": {"required": False, "flags": []},
                "secondary_signals": [],
            },
        }

        trace = validate_output_with_repair(json.dumps(candidate, ensure_ascii=False), snapshot=snapshot)

        self.assertTrue(trace.result.is_valid)
        self.assertFalse(trace.repair_applied)
        self.assertEqual(trace.final_output_origin, OUTPUT_ORIGIN_NORMALIZED_VALID)
        self.assertIn("hoisted_decision_review", trace.normalization_notes)
        out = trace.result.data
        assert out is not None
        self.assertEqual(out["decision"]["action"], "create_case_and_task")
        self.assertNotIn("review", out["decision"])
        self.assertEqual(out["review"], {"required": False, "flags": []})

    def test_decision_confidence_object_is_hoisted(self) -> None:
        snapshot = {
            "mailbox": "unknown",
            "observed_at": "2026-06-20T16:38:35+00:00",
            "thread_context_quality": "weak",
            "source_message": {
                "message_id": "19ee5e5feec89939",
                "thread_id": "19ee5e5feec89939",
                "date": "2026-06-20T16:38:35+00:00",
                "sender": '"Cieplo.app" <dozorca@cieplo.app>',
                "to": ["biuro.topinstal@gmail.com"],
                "cc": [],
                "subject": "Zapytanie: dom jednorodzinny (~180 mkw.) - Adamow",
                "snippet": "Lead HVAC",
                "has_attachments": False,
                "labels": ["INBOX"],
            },
        }
        candidate = {
            "business_area": "sales",
            "case_assessment": {"case_family": "lead_opportunity"},
            "decision": {
                "action": "create_case_and_task",
                "priority": "high",
                "reason": "New lead inquiry for a heat-pump installation.",
                "confidence": {
                    "signal_confidence": "high",
                    "case_link_confidence": "high",
                    "decision_confidence": "high",
                    "extraction_confidence": "high",
                },
            },
            "thread": {
                "thread_summary": "Zapytanie: dom jednorodzinny (~180 mkw.) - Adamow",
                "linked_case_candidates": [],
            },
            "secondary_signals": [],
            "review": {"required": False, "flags": []},
        }

        trace = validate_output_with_repair(json.dumps(candidate, ensure_ascii=False), snapshot=snapshot)

        self.assertTrue(trace.result.is_valid)
        self.assertIn("hoisted_decision_confidence_object", trace.normalization_notes)
        out = trace.result.data
        assert out is not None
        self.assertNotIn("confidence", out["decision"])
        self.assertEqual(out["confidence"]["signal_confidence"], 0.85)
        self.assertEqual(out["confidence"]["decision_confidence"], 0.85)

    def test_decision_business_area_is_hoisted(self) -> None:
        snapshot = {
            "mailbox": "unknown",
            "observed_at": "2026-06-20T16:38:35+00:00",
            "thread_context_quality": "weak",
            "source_message": {
                "message_id": "19ef0d025e0a0001",
                "thread_id": "19ef0d025e0a0001",
                "date": "2026-06-20T16:38:35+00:00",
                "sender": "Supplier <supplier@example.com>",
                "to": ["biuro.topinstal@gmail.com"],
                "cc": [],
                "subject": "Producent wspornikow do klimatyzacji",
                "snippet": "Supplier portfolio inquiry.",
                "has_attachments": False,
                "labels": ["INBOX"],
            },
        }
        candidate = {
            "thread": {
                "thread_summary": "Supplier offers brackets and bases for heat pumps.",
                "linked_case_candidates": [],
            },
            "case_assessment": {"case_family": "supplier_commercial_review"},
            "primary_signal": {
                "code": "supplier_offer",
                "name": "Supplier offer",
                "description": "Supplier offers brackets and bases for HVAC installations.",
                "business_significance": "Potential procurement source requires evaluation.",
            },
            "decision": {
                "action": "create_case_and_task",
                "business_area": "procurement",
                "priority": "medium",
                "reason": "Supplier offer requires portfolio evaluation.",
            },
            "confidence": {
                "signal_confidence": "high",
                "case_link_confidence": "low",
                "decision_confidence": "high",
                "extraction_confidence": "high",
            },
            "review": {"required": False, "flags": []},
            "secondary_signals": [],
        }

        trace = validate_output_with_repair(json.dumps(candidate, ensure_ascii=False), snapshot=snapshot)

        self.assertTrue(trace.result.is_valid)
        self.assertIn("hoisted_decision_business_area", trace.normalization_notes)
        out = trace.result.data
        assert out is not None
        self.assertEqual(out["business_area"], "procurement")
        self.assertNotIn("business_area", out["decision"])

    def test_case_assessment_business_area_and_confidence_are_hoisted(self) -> None:
        snapshot = {
            "mailbox": "unknown",
            "observed_at": "2026-06-20T16:38:35+00:00",
            "thread_context_quality": "strong",
            "source_message": {
                "message_id": "19df825f1b6512e0",
                "thread_id": "19df736c07911e98",
                "date": "2026-06-20T16:38:35+00:00",
                "sender": "Supplier <supplier@example.com>",
                "to": ["biuro.topinstal@gmail.com"],
                "cc": [],
                "subject": "ODP: Zapytanie ze strony internetowej",
                "snippet": "Account setup confirmation.",
                "has_attachments": False,
                "labels": ["INBOX"],
            },
        }
        candidate = {
            "thread": {
                "thread_summary": "ODP: Zapytanie ze strony internetowej; context=strong",
                "linked_case_candidates": [
                    {
                        "case_key": "thread:19df736c07911e98",
                        "case_type": "thread_context",
                        "match_confidence": 0.93,
                        "evidence": ["same_thread_id"],
                    }
                ],
            },
            "case_assessment": {
                "case_family": "supplier_commercial_review",
                "business_area": "supplier_commercial",
                "signal_confidence": "high",
                "case_link_confidence": "high",
                "decision_confidence": "high",
                "extraction_confidence": "high",
            },
            "decision": {"action": "append_to_existing_case", "priority": "medium"},
            "secondary_signals": [],
            "review": {"required": False, "flags": []},
            "reason": "Supplier account setup confirmation should be appended to the existing thread case.",
        }

        trace = validate_output_with_repair(json.dumps(candidate, ensure_ascii=False), snapshot=snapshot)

        self.assertTrue(trace.result.is_valid)
        out = trace.result.data
        assert out is not None
        self.assertEqual(out["business_area"], "supplier_commercial")
        self.assertNotIn("business_area", out["case_assessment"])
        self.assertNotIn("signal_confidence", out["case_assessment"])
        self.assertEqual(out["confidence"]["case_link_confidence"], 0.85)
        self.assertEqual(out["thread"]["linked_case_candidates"][0]["match_confidence"], 0.93)
        self.assertNotIn("evidence", out["thread"]["linked_case_candidates"][0])

    def test_top_level_linked_case_candidates_move_to_thread_contract(self) -> None:
        snapshot = {
            "mailbox": "unknown",
            "observed_at": "2026-06-20T16:38:35+00:00",
            "thread_context_quality": "weak",
            "source_message": {
                "message_id": "19f183eb1e809174",
                "thread_id": "19f183eb1e809174",
                "date": "2026-06-20T16:38:35+00:00",
                "sender": "Partner <partner@example.com>",
                "to": ["biuro.topinstal@gmail.com"],
                "cc": [],
                "subject": "System ratalny Santander-wspolpraca",
                "snippet": "Partnership inquiry.",
                "has_attachments": False,
                "labels": ["INBOX"],
            },
        }
        candidate = {
            "thread": {"thread_id": "19f183eb1e809174", "thread_summary": "System ratalny Santander-wspolpraca"},
            "business_area": "sales",
            "case_assessment": {"case_family": "lead_opportunity"},
            "decision": {
                "action": "create_case",
                "priority": "medium",
                "reason": "Incoming partnership inquiry requiring evaluation.",
            },
            "secondary_signals": [],
            "review": {"required": False, "flags": []},
            "linked_case_candidates": [{"case_key": "thread:19f183eb1e809174"}],
        }

        trace = validate_output_with_repair(json.dumps(candidate, ensure_ascii=False), snapshot=snapshot)

        self.assertTrue(trace.result.is_valid)
        self.assertIn("moved_top_level_linked_case_candidates_to_thread", trace.normalization_notes)
        out = trace.result.data
        assert out is not None
        self.assertNotIn("linked_case_candidates", out)
        self.assertEqual(
            out["thread"]["linked_case_candidates"],
            [{"case_key": "thread:19f183eb1e809174", "case_type": "thread_context", "match_confidence": 0.0}],
        )

    def test_missing_reason_uses_thread_summary_for_schema_rationale(self) -> None:
        snapshot = {
            "mailbox": "unknown",
            "observed_at": "2026-06-20T16:38:35+00:00",
            "thread_context_quality": "weak",
            "source_message": {
                "message_id": "19f124a846ff7c32",
                "thread_id": "19f124a846ff7c32",
                "date": "2026-06-20T16:38:35+00:00",
                "sender": '"Cieplo.app" <dozorca@cieplo.app>',
                "to": ["biuro.topinstal@gmail.com"],
                "cc": [],
                "subject": "Zapytanie: dom jednorodzinny parterowy (~150 mkw.) - Libiaz",
                "snippet": "Lead HVAC",
                "has_attachments": False,
                "labels": ["INBOX"],
            },
        }
        candidate = {
            "thread": {"thread_summary": "Zapytanie: dom jednorodzinny parterowy (~150 mkw.) - Libiaz"},
            "case_assessment": {"business_area": "sales", "case_family": "lead_opportunity"},
            "decision": {"action": "create_case_and_task"},
            "priority": "high",
            "secondary_signals": [],
            "review": {"required": False, "flags": []},
        }

        trace = validate_output_with_repair(json.dumps(candidate, ensure_ascii=False), snapshot=snapshot)

        self.assertTrue(trace.result.is_valid)
        out = trace.result.data
        assert out is not None
        self.assertEqual(out["business_area"], "sales")
        self.assertIn("thread summary", out["reason"])
        self.assertEqual(out["decision"]["action_rationale"], out["reason"])

    def test_flat_thread_summary_root_normalizes_without_repair(self) -> None:
        snapshot = {
            "mailbox": "biuro.topinstal@gmail.com",
            "observed_at": "2026-07-10T18:39:00+02:00",
            "thread_context_quality": "weak",
            "source_message": {
                "message_id": "19ee5e5feec89939",
                "thread_id": "19ee5e5feec89939",
                "date": "2026-07-10T10:00:00+02:00",
                "sender": "client@example.com",
                "to": ["biuro.topinstal@gmail.com"],
                "subject": "Lead HVAC",
                "has_attachments": False,
            },
        }
        candidate = {
            "thread_summary": "Lead HVAC inquiry",
            "business_area": "sales",
            "case_assessment": {"case_family": "lead_opportunity"},
            "decision": {
                "action": "create_case",
                "priority": "medium",
                "reason": "Incoming lead request.",
            },
            "secondary_signals": [],
            "review": {"required": False, "flags": []},
        }
        trace = validate_output_with_repair(json.dumps(candidate, ensure_ascii=False), snapshot=snapshot)
        self.assertTrue(trace.result.is_valid)
        self.assertFalse(trace.repair_applied)
        out = trace.result.data
        assert out is not None
        self.assertEqual(out["thread"]["thread_summary"], "Lead HVAC inquiry")


if __name__ == "__main__":
    unittest.main()
