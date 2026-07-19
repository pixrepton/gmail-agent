from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from action_planner import plan_actions
from case_intelligence import build_case_intelligence
from intake_schema import (
    validate_business_reasoning_result,
    validate_case_link_result,
    validate_intake_result,
    validate_reply_draft_result,
)
from tests.fixture_helpers import build_fixture_intake_candidate, build_fixture_snapshot


class CaseIntelligenceScenarioTests(unittest.TestCase):
    def test_delivery_delay_surfaces_supplier_follow_up_on_desk(self) -> None:
        result = self._build_intelligence(
            message_payload={
                "mailbox": "ops@topinstal.local",
                "source_message": {
                    "message_id": "fixture-delivery-delay-001",
                    "thread_id": "delivery-thread-001",
                    "date": "2026-04-02T08:10:00+02:00",
                    "from": "supplier@example.com",
                    "to": ["ops@topinstal.local"],
                    "subject": "Opóźnienie dostawy części do montażu",
                    "snippet": "Nowy termin dostawy będzie późniejszy.",
                    "body": "Dostawa części do montażu przesuwa się na przyszły tydzień.",
                    "labels": ["INBOX"],
                },
                "context_messages": [],
            },
            intake_spec={
                "business_area": "procurement",
                "case_family": "procurement_delivery",
                "primary_signal_code": "delivery_delay",
                "primary_signal_name": "Delivery delay",
                "decision_action": "update_case_state",
                "action_rationale": "Delay changes the operational state of an existing delivery case.",
                "priority": "high",
                "reason": "Supplier reports a delay that may affect installation readiness.",
                "review_required": False,
                "review_flags": [],
                "confidence": {
                    "signal_confidence": 0.9,
                    "case_link_confidence": 0.82,
                    "decision_confidence": 0.86,
                    "extraction_confidence": 0.78,
                },
                "state_detected": "delivery_at_risk",
                "state_change": {"detected": True, "from_state": "ordered", "to_state": "delayed"},
                "is_new_case": False,
            },
            case_link_spec={
                "decision": "linked",
                "selected_case_key": "CASE-DEL-001",
                "confidence": 0.82,
                "candidates": [
                    {"case_key": "CASE-DEL-001", "case_type": "procurement_delivery", "match_confidence": 0.82}
                ],
            },
            business_spec={
                "business_interpretation": "Dostawa części do montażu jest opóźniona i może wpłynąć na gotowość instalacji.",
                "business_area": "supplier",
                "customer_state_guess": "supplier_thread",
                "recommended_next_action": "update_case",
                "recommended_action_reason": "Trzeba potwierdzić nowy termin i ocenić wpływ na plan prac.",
                "missing_information": ["confirmed delivery date"],
                "risks": ["supplier_dependency"],
                "urgency": "high",
                "operator_note": "Skontaktuj się z dostawcą i sprawdź wpływ na harmonogram.",
                "confidence": {"business_confidence": 0.88, "action_confidence": 0.84},
            },
            reply_spec={"draft_enabled": False, "drafts": [], "do_not_send_reasons": ["internal_follow_up"], "confidence": 0.0},
        )

        primary = result["next_best_action"]["primary_next_action"]
        self.assertEqual(primary["action_type"], "follow_up_supplier")
        self.assertEqual(result["desk_composition"]["presence_mode"], "strong")
        self.assertEqual(result["desk_composition"]["surface_zone"], "desk")

    def test_delivery_confirmed_moves_topic_out_of_primary_desk(self) -> None:
        result = self._build_intelligence(
            message_payload={
                "mailbox": "ops@topinstal.local",
                "source_message": {
                    "message_id": "fixture-delivery-confirmed-001",
                    "thread_id": "delivery-thread-001",
                    "date": "2026-04-04T12:15:00+02:00",
                    "from": "supplier@example.com",
                    "to": ["ops@topinstal.local"],
                    "subject": "Potwierdzenie dostawy części",
                    "snippet": "Dostawa została potwierdzona.",
                    "body": "Części dotarły i są gotowe do odbioru.",
                    "labels": ["INBOX"],
                },
                "context_messages": [],
            },
            intake_spec={
                "business_area": "procurement",
                "case_family": "procurement_delivery",
                "primary_signal_code": "delivery_confirmed",
                "primary_signal_name": "Delivery confirmed",
                "decision_action": "update_case_state",
                "action_rationale": "Delivery confirmation updates the case state.",
                "priority": "low",
                "reason": "Supplier confirms delivery, so desk pressure should drop.",
                "review_required": False,
                "review_flags": [],
                "confidence": {
                    "signal_confidence": 0.9,
                    "case_link_confidence": 0.84,
                    "decision_confidence": 0.88,
                    "extraction_confidence": 0.8,
                },
                "state_detected": "delivered",
                "state_change": {"detected": True, "from_state": "delayed", "to_state": "delivered"},
                "is_new_case": False,
            },
            case_link_spec={
                "decision": "linked",
                "selected_case_key": "CASE-DEL-001",
                "confidence": 0.84,
                "candidates": [
                    {"case_key": "CASE-DEL-001", "case_type": "procurement_delivery", "match_confidence": 0.84}
                ],
            },
            business_spec={
                "business_interpretation": "Dostawa została potwierdzona i nie wymaga już mocnej ekspozycji na biurku.",
                "business_area": "supplier",
                "customer_state_guess": "supplier_thread",
                "recommended_next_action": "wait",
                "recommended_action_reason": "Wystarczy zachować temat w pamięci sprawy bez dalszej presji.",
                "missing_information": [],
                "risks": [],
                "urgency": "low",
                "operator_note": "Można zejść z uwagą, dopóki nie pojawi się nowy problem.",
                "confidence": {"business_confidence": 0.87, "action_confidence": 0.83},
            },
            reply_spec={"draft_enabled": False, "drafts": [], "do_not_send_reasons": ["wait"], "confidence": 0.0},
            current_note_state={"presence_mode": "strong", "age_days": 1},
        )

        self.assertFalse(result["desk_composition"]["should_surface"])
        self.assertEqual(result["next_best_action"]["primary_next_action"]["action_type"], "wait")
        self.assertEqual(result["lifecycle_revision"]["lifecycle_intent"], "move_to_case_only")

    def test_split_suspicion_is_raised_for_competing_signals(self) -> None:
        result = self._build_intelligence(
            message_payload={
                "mailbox": "ops@topinstal.local",
                "source_message": {
                    "message_id": "fixture-split-review-001",
                    "thread_id": "split-thread-001",
                    "date": "2026-04-03T14:20:00+02:00",
                    "from": "mixed@example.com",
                    "to": ["ops@topinstal.local"],
                    "subject": "Oferta i jednocześnie potwierdzenie dostawy",
                    "snippet": "Mieszany sygnał.",
                    "body": "W jednej wiadomości dostajecie temat oferty i nowy status dostawy.",
                    "labels": ["INBOX"],
                },
                "context_messages": [],
            },
            intake_spec={
                "business_area": "operations",
                "case_family": "internal_coordination",
                "primary_signal_code": "mixed_signal",
                "primary_signal_name": "Mixed signal",
                "secondary_signals": [{"code": "delivery_delay", "name": "Delivery delay"}],
                "decision_action": "review",
                "action_rationale": "Mixed signals need review before splitting or merging.",
                "priority": "medium",
                "reason": "One message appears to combine two separate operational threads.",
                "review_required": True,
                "review_flags": ["multiple_competing_signals"],
                "confidence": {
                    "signal_confidence": 0.74,
                    "case_link_confidence": 0.25,
                    "decision_confidence": 0.71,
                    "extraction_confidence": 0.66,
                },
                "state_detected": "follow_up",
                "is_new_case": False,
                "references": {
                    "shipment_numbers": ["SHIP-001"],
                    "order_numbers": ["ORD-777"],
                },
            },
            case_link_spec={"decision": "no_link", "selected_case_key": "", "confidence": 0.0, "candidates": []},
            business_spec={
                "business_interpretation": "Wiadomość miesza dwa wątki i nie powinna być prowadzona bez ręcznej decyzji operatora.",
                "business_area": "internal",
                "customer_state_guess": "unclear",
                "recommended_next_action": "escalate_review",
                "recommended_action_reason": "Najpierw trzeba rozdzielić znaczenia i dopiero potem podjąć ruch.",
                "missing_information": ["confirmed case reference"],
                "risks": ["weak_case_link"],
                "urgency": "normal",
                "operator_note": "Sprawdź, czy to jedna sprawa czy dwa różne wątki.",
                "confidence": {"business_confidence": 0.72, "action_confidence": 0.7},
            },
            reply_spec={"draft_enabled": False, "drafts": [], "do_not_send_reasons": ["review_required_first"], "confidence": 0.0},
        )

        self.assertGreaterEqual(len(result["merge_split_suggestions"]["split_suspicions"]), 1)
        self.assertEqual(result["next_best_action"]["primary_next_action"]["action_type"], "review_required")

    def test_stale_unresolved_waiting_adds_aging_risk_and_deescalates(self) -> None:
        result = self._build_intelligence(
            message_payload={
                "mailbox": "ops@topinstal.local",
                "source_message": {
                    "message_id": "fixture-stale-waiting-001",
                    "thread_id": "waiting-thread-001",
                    "date": "2026-04-05T09:30:00+02:00",
                    "from": "client@example.com",
                    "to": ["ops@topinstal.local"],
                    "subject": "Czekam na informację zwrotną",
                    "snippet": "Klient czeka.",
                    "body": "Dajcie znać, gdy będzie decyzja.",
                    "labels": ["INBOX"],
                },
                "context_messages": [],
            },
            intake_spec={
                "business_area": "sales",
                "case_family": "lead_opportunity",
                "primary_signal_code": "waiting_reply",
                "primary_signal_name": "Waiting reply",
                "decision_action": "append_to_existing_case",
                "action_rationale": "This is a continuation of an existing waiting case.",
                "priority": "low",
                "reason": "Temat jest w stanie oczekiwania i nie wymaga już mocnego biurka.",
                "review_required": False,
                "review_flags": [],
                "confidence": {
                    "signal_confidence": 0.72,
                    "case_link_confidence": 0.81,
                    "decision_confidence": 0.75,
                    "extraction_confidence": 0.64,
                },
                "state_detected": "waiting_for_reply",
                "is_new_case": False,
            },
            case_link_spec={
                "decision": "linked",
                "selected_case_key": "CASE-WAIT-001",
                "confidence": 0.81,
                "candidates": [{"case_key": "CASE-WAIT-001", "case_type": "lead_opportunity", "match_confidence": 0.81}],
            },
            business_spec={
                "business_interpretation": "Sprawa czeka na odpowiedź i nie wymaga już mocnej ekspozycji.",
                "business_area": "lead",
                "customer_state_guess": "waiting_for_data",
                "recommended_next_action": "wait",
                "recommended_action_reason": "Na razie trzeba zachować pamięć sprawy, ale nie eskalować uwagi.",
                "missing_information": [],
                "risks": [],
                "urgency": "low",
                "operator_note": "Zostaw temat w tle i wróć, gdy pojawi się nowa odpowiedź.",
                "confidence": {"business_confidence": 0.8, "action_confidence": 0.76},
            },
            reply_spec={"draft_enabled": False, "drafts": [], "do_not_send_reasons": ["wait"], "confidence": 0.0},
            current_note_state={"presence_mode": "strong", "age_days": 7},
        )

        risk_types = {risk["risk_type"] for risk in result["risk_assessment"]["risks"]}
        self.assertIn("aging_risk", risk_types)
        self.assertIn(result["lifecycle_revision"]["lifecycle_intent"], {"deescalate_presence", "move_to_case_only"})

    def test_waiting_customer_reply_stays_in_day_not_main_desk(self) -> None:
        result = self._build_intelligence(
            message_payload={
                "mailbox": "ops@topinstal.local",
                "source_message": {
                    "message_id": "fixture-waiting-customer-001",
                    "thread_id": "waiting-thread-002",
                    "date": "2026-04-03T13:00:00+02:00",
                    "from": "ops@topinstal.local",
                    "to": ["client@example.com"],
                    "subject": "Re: Czekamy na dane do oferty",
                    "snippet": "Wysłano prośbę o dane.",
                    "body": "Czekamy na odpowiedź klienta z brakującymi danymi.",
                    "labels": ["SENT"],
                },
                "context_messages": [],
            },
            intake_spec={
                "business_area": "sales",
                "case_family": "lead_opportunity",
                "primary_signal_code": "waiting_customer_reply",
                "primary_signal_name": "Waiting customer reply",
                "decision_action": "append_to_existing_case",
                "action_rationale": "The case should remain visible as waiting, not as a hot desk item.",
                "priority": "medium",
                "reason": "Klient musi jeszcze odesłać dane do oferty.",
                "review_required": False,
                "review_flags": [],
                "confidence": {
                    "signal_confidence": 0.78,
                    "case_link_confidence": 0.8,
                    "decision_confidence": 0.79,
                    "extraction_confidence": 0.7,
                },
                "state_detected": "waiting_for_reply",
                "is_new_case": False,
            },
            case_link_spec={
                "decision": "linked",
                "selected_case_key": "CASE-WAIT-002",
                "confidence": 0.8,
                "candidates": [{"case_key": "CASE-WAIT-002", "case_type": "lead_opportunity", "match_confidence": 0.8}],
            },
            business_spec={
                "business_interpretation": "Sprawa czeka na odpowiedź klienta i powinna zostać na radarze bez zajmowania Biurka.",
                "business_area": "lead",
                "customer_state_guess": "waiting_for_data",
                "recommended_next_action": "wait",
                "recommended_action_reason": "Bez odpowiedzi klienta nie ma sensu eskalować kolejnego ruchu.",
                "missing_information": [],
                "risks": ["customer_silence_risk"],
                "urgency": "normal",
                "operator_note": "Jeśli klient długo nie odpowiada, wróć do sprawy później.",
                "confidence": {"business_confidence": 0.82, "action_confidence": 0.78},
            },
            reply_spec={"draft_enabled": False, "drafts": [], "do_not_send_reasons": ["wait"], "confidence": 0.0},
        )

        self.assertEqual(result["desk_composition"]["surface_zone"], "day")
        self.assertIn(result["desk_composition"]["presence_mode"], {"subtle", "standard"})

    def test_reply_intent_without_draft_still_points_to_customer_response(self) -> None:
        result = self._build_intelligence(
            message_payload={
                "mailbox": "ops@topinstal.local",
                "source_message": {
                    "message_id": "fixture-procurement-reply-001",
                    "thread_id": "procurement-reply-001",
                    "date": "2026-04-03T16:00:00+02:00",
                    "from": "supplier@example.com",
                    "to": ["ops@topinstal.local"],
                    "subject": "Potrzebujemy potwierdzenia z Waszej strony",
                    "snippet": "Dajcie odpowiedź jeszcze dziś.",
                    "body": "Potrzebujemy odpowiedzi, czy akceptujecie przesunięty termin i czy mamy ruszać dalej.",
                    "labels": ["INBOX"],
                },
                "context_messages": [],
            },
            intake_spec={
                "business_area": "procurement",
                "case_family": "procurement_delivery",
                "primary_signal_code": "supplier_confirmation_request",
                "primary_signal_name": "Supplier confirmation request",
                "decision_action": "append_to_existing_case",
                "action_rationale": "Supplier asks for a concrete response inside an existing case.",
                "priority": "medium",
                "reason": "Dostawca potrzebuje krótkiej odpowiedzi, ale draft nie jest gotowy.",
                "review_required": False,
                "review_flags": [],
                "confidence": {
                    "signal_confidence": 0.8,
                    "case_link_confidence": 0.78,
                    "decision_confidence": 0.79,
                    "extraction_confidence": 0.7,
                },
                "state_detected": "waiting_for_reply",
                "is_new_case": False,
            },
            case_link_spec={
                "decision": "linked",
                "selected_case_key": "CASE-PROC-REPLY-001",
                "confidence": 0.78,
                "candidates": [{"case_key": "CASE-PROC-REPLY-001", "case_type": "procurement_delivery", "match_confidence": 0.78}],
            },
            business_spec={
                "business_interpretation": "Dostawca potrzebuje krótkiej odpowiedzi, czy firma akceptuje przesunięty termin.",
                "business_area": "supplier",
                "customer_state_guess": "supplier_thread",
                "recommended_next_action": "reply",
                "recommended_action_reason": "Najbardziej sensowny ruch to odpowiedź do dostawcy, nawet jeśli draft trzeba dopracować ręcznie.",
                "missing_information": [],
                "risks": ["supplier_dependency"],
                "urgency": "normal",
                "operator_note": "Odpowiedz dostawcy i zamknij niepewność po jego stronie.",
                "confidence": {"business_confidence": 0.81, "action_confidence": 0.76},
            },
            reply_spec={"draft_enabled": False, "drafts": [], "do_not_send_reasons": ["manual_edit_needed"], "confidence": 0.0},
        )

        self.assertEqual(result["next_best_action"]["primary_next_action"]["action_type"], "answer_customer")

    def _build_intelligence(
        self,
        *,
        message_payload: dict,
        intake_spec: dict,
        case_link_spec: dict,
        business_spec: dict,
        reply_spec: dict,
        current_note_state: dict | None = None,
    ) -> dict:
        snapshot = build_fixture_snapshot(message_payload)
        intake_spec = dict(intake_spec)
        if not intake_spec.get("linked_case_candidates") and case_link_spec.get("selected_case_key"):
            intake_spec["linked_case_candidates"] = [
                {
                    "case_key": str(case_link_spec.get("selected_case_key") or ""),
                    "case_type": str(intake_spec.get("case_family") or "message_context"),
                    "match_confidence": float(case_link_spec.get("confidence") or 0.0),
                }
            ]
        intake_candidate = build_fixture_intake_candidate(snapshot, intake_spec)
        intake_result = validate_intake_result(intake_candidate, final_output_origin="raw_valid")
        case_link_result = validate_case_link_result(case_link_spec)
        business_result = validate_business_reasoning_result(business_spec)
        reply_result = validate_reply_draft_result(reply_spec)
        action_plan = plan_actions(intake_result, case_link_result, business_result, reply_result)
        return build_case_intelligence(
            snapshot=snapshot,
            intake_result=intake_result,
            case_link_result=case_link_result,
            business_result=business_result,
            reply_result=reply_result,
            action_plan_result=action_plan,
            current_note_state=current_note_state or {},
        )


if __name__ == "__main__":
    unittest.main()
