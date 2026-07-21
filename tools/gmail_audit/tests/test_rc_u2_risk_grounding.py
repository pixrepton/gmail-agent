"""RC-U2 — strict PER-RISK grounding of operator-facing risks.

Invariant (independent of any eval suite): a materialized operator risk must be
supported by a SPECIFIC case fact or deterministic state, with provenance to that
fact. Neither ``severity=high`` nor overall case urgency is per-risk evidence: an
urgent case does not ground an unrelated generic risk. Generic business-reasoner
truisms carry no supporting fact of their own and must not become material risks.

Grounded (material) risks derive from a concrete constructor: attachment finding,
concrete unresolved question, missing-critical lead state, detected delivery
state, aging state, or an evidenced contradiction.

Tests exercise the real path (build_case_intelligence -> build_understanding_output)
and assert on failure CLASSES, never on literal benchmark fixtures or case ids.
"""

from __future__ import annotations

import unittest

from case_intelligence import build_case_intelligence
from understanding_output import UNDERSTANDING_SCHEMA_VERSION, build_understanding_output, validate_understanding_invariants


def _snapshot(message_id: str = "msg_rcu2") -> dict:
    return {
        "source_message": {
            "message_id": message_id,
            "thread_id": "thread_rcu2",
            "subject": "Temat testowy",
            "body": "Tresc testowa.",
            "date": "2026-01-01T10:00:00Z",
        },
        "context_messages": [],
    }


def _uo(
    *,
    business: dict,
    intake: dict | None = None,
    thread_memory: dict | None = None,
    current_note_state: dict | None = None,
    attachment_intelligence: dict | None = None,
    case_context_pack: dict | None = None,
) -> dict:
    snapshot = _snapshot()
    intake_result = intake or {
        "decision": {"action": "create_case"},
        "business_area": "service",
        "priority": "low",
        "case_assessment": {"case_family": "general", "interpretation": "Test."},
        "thread": {"thread_id": "thread_rcu2"},
    }
    case_link = {"decision": "unlinked", "confidence": 0.3, "selected_case_key": ""}
    reply = {"draft_enabled": False, "drafts": []}
    action_plan = {"primary_action": "hold", "confidence": 0.5}
    pack = case_context_pack or {"source_refs": [], "vector_retrieval": {}, "relevant_chunks": []}
    ci = build_case_intelligence(
        snapshot=snapshot,
        intake_result=intake_result,
        case_link_result=case_link,
        business_result=business,
        reply_result=reply,
        action_plan_result=action_plan,
        current_note_state=current_note_state or {},
        attachment_intelligence=attachment_intelligence or {},
        thread_memory=thread_memory or {},
        case_context_pack=pack,
    )
    uo = build_understanding_output(
        snapshot=snapshot,
        intake_result=intake_result,
        case_link_result=case_link,
        intelligence=ci,
        thread_memory=thread_memory or {},
        attachment_intelligence=attachment_intelligence or {},
        case_context_pack=pack,
        business_result=business,
    )
    patched, _ = validate_understanding_invariants(uo)
    return patched


def _material_types(uo: dict) -> set[str]:
    return {str(r.get("risk_type") or "") for r in uo.get("risks") or []}


def _material_text(uo: dict) -> str:
    return " ".join(str(r.get("summary_pl") or "") for r in uo.get("risks") or []).lower()


class StrictPerRiskGrounding(unittest.TestCase):
    def test_generic_high_severity_truism_remains_non_material(self) -> None:
        # priority=high -> _map assigns severity=high, but the truism has no fact.
        intake = {
            "decision": {"action": "create_case"},
            "business_area": "service",
            "priority": "high",
            "case_assessment": {"case_family": "general", "interpretation": "x"},
            "thread": {"thread_id": "thread_rcu2"},
        }
        uo = _uo(business={"risks": ["delay in customer contact may worsen dissatisfaction"], "urgency": "normal"}, intake=intake)
        self.assertNotIn("operational_delay_risk", _material_types(uo))

    def test_urgent_case_does_not_ground_unrelated_spam_risk(self) -> None:
        intake = {
            "decision": {"action": "create_case"},
            "business_area": "service",
            "priority": "high",
            "case_assessment": {"case_family": "general", "interpretation": "Awaria pilna."},
            "thread": {"thread_id": "thread_rcu2"},
        }
        uo = _uo(business={"risks": ["possible spam message"], "urgency": "high"}, intake=intake)
        self.assertNotIn("spam", _material_text(uo))

    def test_urgent_case_does_not_ground_unrelated_generic_truism(self) -> None:
        uo = _uo(business={"risks": ["ogolne ryzyko biznesowe do obserwacji"], "urgency": "high"})
        self.assertEqual([], uo.get("risks") or [])

    def test_risk_with_only_source_id_but_no_supporting_fact_is_unsupported(self) -> None:
        raw = {
            "schema_version": UNDERSTANDING_SCHEMA_VERSION,
            "source_signal_id": "msg_present",
            "situation_summary_pl": "s",
            "customer_intent_pl": "i",
            "risks": [{"risk_type": "interpretation_risk", "severity": "high", "summary_pl": "cos"}],
        }
        patched, _ = validate_understanding_invariants(raw)
        self.assertTrue(patched["risks"][0].get("unsupported"))

    def test_all_materialized_risks_are_grounded(self) -> None:
        uo = _uo(business={"risks": ["possible spam", "delay risk"], "urgency": "high"}, current_note_state={"age_days": 9})
        for row in uo.get("risks") or []:
            self.assertTrue((row.get("grounding") or {}).get("grounded"), row)
            self.assertFalse(row.get("unsupported"))

    def test_no_public_risk_hypotheses_surface(self) -> None:
        uo = _uo(business={"risks": ["delay risk"], "urgency": "normal"})
        self.assertNotIn("risk_hypotheses", uo)


class GroundedRisksSurvive(unittest.TestCase):
    def test_aging_risk_with_concrete_state_survives(self) -> None:
        uo = _uo(business={"risks": [], "urgency": "normal"}, current_note_state={"age_days": 7})
        self.assertIn("aging_risk", _material_types(uo))
        aging = next(r for r in uo["risks"] if r["risk_type"] == "aging_risk")
        self.assertEqual(aging["grounding"]["basis"], "age_days")
        self.assertTrue(aging["grounding"]["supporting_fact_pl"])

    def test_service_risk_with_attachment_evidence_survives(self) -> None:
        uo = _uo(
            business={"risks": [], "urgency": "normal"},
            attachment_intelligence={"combined_risk_flags": ["Protokol serwisowy: awaria sprezarki"]},
        )
        self.assertTrue(uo.get("risks"))
        top = uo["risks"][0]
        self.assertTrue(top["grounding"]["grounded"])
        self.assertEqual(top["grounding"]["basis"], "attachment_finding")
        self.assertTrue(top["grounding"]["supporting_fact_pl"])

    def test_lead_loss_with_missing_critical_survives(self) -> None:
        intake = {
            "decision": {"action": "create_case"},
            "business_area": "sales",
            "priority": "medium",
            "case_assessment": {"case_family": "lead_opportunity", "interpretation": "Lead."},
            "thread": {"thread_id": "thread_rcu2"},
        }
        uo = _uo(
            business={"risks": [], "urgency": "normal", "missing_information": ["installation address"]},
            intake=intake,
        )
        self.assertIn("lead_loss_risk", _material_types(uo))
        lead = next(r for r in uo["risks"] if r["risk_type"] == "lead_loss_risk")
        self.assertEqual(lead["grounding"]["basis"], "missing_critical_fields")

    def test_concrete_unresolved_question_survives_and_bare_flag_does_not(self) -> None:
        grounded = _uo(
            business={"risks": [], "urgency": "normal"},
            thread_memory={"has_unanswered_question": True, "unresolved_questions": ["Kiedy przyjedziecie?"]},
        )
        self.assertTrue(grounded.get("risks"))
        bare = _uo(
            business={"risks": [], "urgency": "normal"},
            thread_memory={"has_unanswered_question": True, "unresolved_questions": []},
        )
        self.assertEqual([], bare.get("risks") or [])

    def test_contradiction_with_evidence_becomes_grounded_risk(self) -> None:
        pack = {
            "source_refs": [],
            "vector_retrieval": {},
            "relevant_chunks": [],
            "conflicting_facts": [
                {
                    "field_name": "heated_area_m2",
                    "summary_pl": "Powierzchnia 120 vs 160 m2.",
                    "evidence_refs": [{"source_type": "gmail_message", "source_id": "msg_rcu2"}],
                }
            ],
        }
        uo = _uo(business={"risks": [], "urgency": "normal"}, case_context_pack=pack)
        self.assertIn("contradiction_risk", _material_types(uo))
        contra = next(r for r in uo["risks"] if r["risk_type"] == "contradiction_risk")
        self.assertTrue(contra["grounding"]["evidence_refs"])

    def test_contradiction_without_evidence_not_materialized(self) -> None:
        pack = {
            "source_refs": [],
            "vector_retrieval": {},
            "relevant_chunks": [],
            "conflicting_facts": [{"field_name": "heated_area_m2", "summary_pl": "Konflikt bez evidence."}],
        }
        uo = _uo(business={"risks": [], "urgency": "normal"}, case_context_pack=pack)
        self.assertNotIn("contradiction_risk", _material_types(uo))


if __name__ == "__main__":
    unittest.main()
