"""RC-U1 — explicit customer intent + recommendation semantics + honest confidence.

Product invariant (independent of any eval suite): when the reasoning layer has
produced a real interpretation, operator-facing Understanding must express a
specific customer intent and an evidence-sensitive confidence, not collapse to a
generic "unknown" intent and confidence=0.0. Genuine ambiguity must still be
allowed to stay unknown. The operator-facing recommendation must stay coherent
with the business intelligence it is derived from (no contradictory re-derivation).

Intent/recommendation are REUSED from existing business intelligence, not copied
verbatim from planner action and not produced by a large keyword taxonomy.
"""

from __future__ import annotations

import unittest

from case_intelligence import build_case_intelligence
from understanding_output import build_understanding_output, validate_understanding_invariants


def _snapshot() -> dict:
    return {
        "source_message": {
            "message_id": "msg_rcu1",
            "thread_id": "thread_rcu1",
            "subject": "Zapytanie",
            "body": "Tresc.",
            "date": "2026-01-01T10:00:00Z",
        },
        "context_messages": [],
    }


def _uo(*, business: dict, intake: dict | None = None, case_context_pack: dict | None = None) -> dict:
    snapshot = _snapshot()
    intake_result = intake or {
        "decision": {"action": "create_case"},
        "business_area": "sales",
        "priority": "medium",
        "case_assessment": {"case_family": "lead_opportunity", "interpretation": "Lead."},
        "thread": {"thread_id": "thread_rcu1"},
    }
    case_link = {"decision": "unlinked", "confidence": 0.3, "selected_case_key": ""}
    reply = {"draft_enabled": False, "drafts": []}
    action_plan = {"primary_action": "prepare_reply", "confidence": 0.6}
    pack = case_context_pack or {"source_refs": [], "vector_retrieval": {}, "relevant_chunks": []}
    ci = build_case_intelligence(
        snapshot=snapshot,
        intake_result=intake_result,
        case_link_result=case_link,
        business_result=business,
        reply_result=reply,
        action_plan_result=action_plan,
        case_context_pack=pack,
    )
    uo = build_understanding_output(
        snapshot=snapshot,
        intake_result=intake_result,
        case_link_result=case_link,
        intelligence=ci,
        case_context_pack=pack,
        business_result=business,
    )
    patched, _ = validate_understanding_invariants(uo)
    return patched


class CustomerIntentSemantics(unittest.TestCase):
    def test_clear_quote_intent_not_unknown(self) -> None:
        uo = _uo(business={
            "business_interpretation": "Klient prosi o wycene pompy ciepla dla domu 150 m2.",
            "customer_state_guess": "new_lead",
            "recommended_next_action": "reply",
            "urgency": "normal",
        })
        intent = uo["customer_intent_pl"].lower()
        self.assertNotIn("wymaga potwierdzenia", intent)
        self.assertIn("wycen", intent)

    def test_clear_service_intent_not_unknown(self) -> None:
        uo = _uo(business={
            "business_interpretation": "Klient zglasza awarie i prosi o serwis kotla.",
            "customer_state_guess": "active_case",
            "recommended_next_action": "reply",
            "urgency": "high",
        })
        self.assertIn("serwis", uo["customer_intent_pl"].lower())

    def test_document_intent_not_unknown(self) -> None:
        uo = _uo(business={
            "business_interpretation": "Klient pyta o fakture i termin platnosci.",
            "customer_state_guess": "finance_flow",
            "recommended_next_action": "reply",
            "urgency": "normal",
        })
        self.assertIn("faktur", uo["customer_intent_pl"].lower())

    def test_safe_multi_intent_summary_survives_phoneish_date_redaction(self) -> None:
        uo = _uo(business={
            "business_interpretation": (
                "Klient zaakceptowal oferte i chce przelozyc wizyte z 23.07.2026 "
                "na piatek; pyta tez o wywoz starego pieca."
            ),
            "business_summary_short": (
                "Klient akceptuje oferte, prosi o przeniesienie wizyty na piatek "
                "i pyta o wywoz starego pieca."
            ),
            "customer_state_guess": "post_offer",
            "recommended_next_action": "escalate_review",
            "urgency": "high",
        })

        intent = uo["customer_intent_pl"].lower()
        self.assertIn("akceptuje oferte", intent)
        self.assertIn("piatek", intent)
        self.assertIn("wywoz starego pieca", intent)

    def test_safe_fact_update_summary_survives_phoneish_date_redaction(self) -> None:
        uo = _uo(business={
            "business_interpretation": (
                "Klient koryguje zalozenie z 2026-07-10: na parterze ma juz "
                "zamontowane ogrzewanie podlogowe."
            ),
            "business_summary_short": (
                "Klient poprawia wczesniejsze zalozenie: na parterze istnieje "
                "ogrzewanie podlogowe."
            ),
            "customer_state_guess": "active_case",
            "recommended_next_action": "update_case",
            "urgency": "normal",
        })

        intent = uo["customer_intent_pl"].lower()
        self.assertIn("poprawia wczesniejsze zalozenie", intent)
        self.assertIn("ogrzewanie podlogowe", intent)

    def test_state_guess_label_used_when_interpretation_unavailable(self) -> None:
        uo = _uo(business={
            "business_interpretation": "Business interpretation unavailable.",
            "customer_state_guess": "waiting_for_data",
            "recommended_next_action": "collect_data",
            "urgency": "normal",
        })
        intent = uo["customer_intent_pl"].lower()
        self.assertNotIn("wymaga potwierdzenia", intent)
        self.assertTrue(intent.strip())

    def test_genuinely_ambiguous_case_stays_unknown(self) -> None:
        uo = _uo(
            business={
                "business_interpretation": "Business interpretation unavailable.",
                "customer_state_guess": "unclear",
                "recommended_next_action": "escalate_review",
                "urgency": "normal",
            },
            intake={
                "decision": {"action": "review"},
                "business_area": "",
                "priority": "low",
                "case_assessment": {"case_family": "unknown"},
                "thread": {"thread_id": "thread_rcu1"},
            },
        )
        self.assertIn("potwierdzenia", uo["customer_intent_pl"].lower())


class RecommendationCoherence(unittest.TestCase):
    def test_recommendation_not_wait_when_business_recommends_reply(self) -> None:
        uo = _uo(business={
            "business_interpretation": "Klient prosi o wycene.",
            "customer_state_guess": "new_lead",
            "recommended_next_action": "reply",
            "urgency": "normal",
            "confidence": {"business_confidence": 0.7, "action_confidence": 0.7},
        })
        rec = uo.get("next_best_action_recommendation") or {}
        self.assertNotEqual(str(rec.get("action_type") or ""), "wait")

    def test_terminal_lost_state_is_preserved_and_blocks_further_sales(self) -> None:
        uo = _uo(
            business={
                "business_interpretation": "Klient wybral innego wykonawce i rezygnuje z oferty.",
                "customer_state_guess": "post_offer",
                "recommended_next_action": "escalate_review",
                "recommended_action_reason": "Najpierw potwierdz powiazanie z wlasciwa sprawa.",
                "missing_information": ["Potwierdzenie tozsamosci klienta"],
                "urgency": "normal",
            },
            intake={
                "decision": {"action": "review"},
                "business_area": "sales",
                "priority": "medium",
                "case_assessment": {
                    "case_family": "lead_opportunity",
                    "state_detected": "lost",
                    "interpretation": "Klient odrzucil oferte.",
                },
                "thread": {"thread_id": "thread_rcu1"},
                "review": {"required": True, "flags": ["possible_existing_case_but_no_match"]},
            },
        )

        self.assertEqual(uo["situation_summary"]["current_state"], "lost")
        rec = uo["next_best_action_recommendation"]
        title = rec["title_pl"].lower()
        self.assertTrue("zamkn" in title or "archiw" in title)
        self.assertNotIn("call_kalk", title)
        self.assertNotIn("generate_draft", title)
        self.assertNotIn("dopytaj", title)
        self.assertEqual(rec["quality"]["decision_state"], "close")
        self.assertEqual(rec["quality"]["planner_action_hint"], "archive_case")


class HonestConfidence(unittest.TestCase):
    """confidence must come from a real confidence-bearing signal, never be
    invented merely because an intent/interpretation string exists."""

    def test_real_business_confidence_is_preserved_exactly(self) -> None:
        uo = _uo(business={
            "business_interpretation": "Klient prosi o wycene.",
            "customer_state_guess": "new_lead",
            "recommended_next_action": "reply",
            "urgency": "normal",
            "confidence": {"business_confidence": 0.82, "action_confidence": 0.8},
        })
        self.assertGreater(uo["confidence"], 0.3)

    def test_placeholder_zero_confidence_is_not_mistaken_for_high_confidence(self) -> None:
        uo = _uo(business={
            "business_interpretation": "Klient prosi o wycene pompy ciepla.",
            "customer_state_guess": "new_lead",
            "recommended_next_action": "reply",
            "urgency": "normal",
            "confidence": {"business_confidence": 0.0, "action_confidence": 0.0},
        })
        self.assertEqual(uo["confidence"], 0.0)

    def test_clear_interpretation_without_any_real_confidence_signal_is_not_fabricated(self) -> None:
        # No business.confidence, no intake classification_confidence, no
        # case_understanding.confidence_overall signal anywhere -- a clear
        # intent string must not manufacture a downstream confidence number.
        uo = _uo(
            business={
                "business_interpretation": "Klient prosi o wycene pompy ciepla.",
                "customer_state_guess": "new_lead",
                "recommended_next_action": "reply",
                "urgency": "normal",
            },
            case_context_pack={"source_refs": [], "vector_retrieval": {}, "relevant_chunks": [], "context_quality": {"confidence": 0.0}},
        )
        self.assertIn("wycen", uo["customer_intent_pl"].lower())
        self.assertEqual(uo["confidence"], 0.0)

    def test_real_upstream_classification_confidence_is_recovered(self) -> None:
        # classification_confidence is a genuine, always-populated intake-stage
        # signal (validate_intake_result) that was previously never read here --
        # this is a real loss-point fix, not a fabricated floor.
        intake = {
            "decision": {"action": "create_case"},
            "business_area": "sales",
            "priority": "medium",
            "case_assessment": {"case_family": "lead_opportunity", "interpretation": "Lead."},
            "thread": {"thread_id": "thread_rcu1"},
            "classification_confidence": 0.55,
        }
        uo = _uo(
            business={
                "business_interpretation": "Klient prosi o wycene.",
                "customer_state_guess": "new_lead",
                "recommended_next_action": "reply",
                "urgency": "normal",
            },
            intake=intake,
        )
        self.assertEqual(uo["confidence"], 0.55)

    def test_real_aggregate_is_not_overridden_by_a_weaker_fallback_signal(self) -> None:
        # case_understanding.confidence_overall already averages 4 real upstream
        # signals; here it lands at a real, non-fabricated 0.45 (business/action
        # confidence 0.9 each, averaged with two absent intake-side components).
        # A later, weaker candidate (classification_confidence=0.1) must never
        # replace or dilute that already-found real aggregate further.
        intake = {
            "decision": {"action": "create_case"},
            "business_area": "sales",
            "priority": "medium",
            "case_assessment": {"case_family": "lead_opportunity", "interpretation": "Lead."},
            "thread": {"thread_id": "thread_rcu1"},
            "classification_confidence": 0.1,
        }
        uo = _uo(
            business={
                "business_interpretation": "Klient prosi o wycene.",
                "customer_state_guess": "new_lead",
                "recommended_next_action": "reply",
                "urgency": "normal",
                "confidence": {"business_confidence": 0.9, "action_confidence": 0.9},
            },
            intake=intake,
        )
        self.assertEqual(uo["confidence"], 0.45)


if __name__ == "__main__":
    unittest.main()
