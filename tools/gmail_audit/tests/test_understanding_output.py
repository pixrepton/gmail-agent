"""Tests for UnderstandingOutput v1."""

from __future__ import annotations

import unittest

from case_intelligence import build_case_intelligence
from understanding_output import UNDERSTANDING_SCHEMA_VERSION, build_understanding_output, validate_understanding_invariants


def _minimal_snapshot() -> dict:
    return {
        "source_message": {
            "message_id": "msg_test_1",
            "thread_id": "thread_1",
            "subject": "Awaria pompy",
            "body": "Czy możecie podjechać?",
            "date": "2026-01-01T10:00:00Z",
        },
        "context_messages": [],
    }


class UnderstandingOutputTests(unittest.TestCase):
    def test_build_and_validate(self) -> None:
        snapshot = _minimal_snapshot()
        intake_result = {
            "decision": {"action": "create_case"},
            "business_area": "service",
            "case_assessment": {"case_family": "lead_opportunity", "interpretation": "Klient zgłasza problem techniczny."},
            "extracted_data": {"city": "Warszawa"},
            "thread": {"thread_id": "thread_1"},
        }
        case_link = {"decision": "unlinked", "confidence": 0.3, "selected_case_key": ""}
        business = {"business_interpretation": "Prośba o serwis", "risks": [], "urgency": "high"}
        reply = {"draft_enabled": False, "drafts": []}
        action_plan = {"primary_action": "hold", "confidence": 0.5}
        ci = build_case_intelligence(
            snapshot=snapshot,
            intake_result=intake_result,
            case_link_result=case_link,
            business_result=business,
            reply_result=reply,
            action_plan_result=action_plan,
            thread_memory={
                "thread_id": "thread_1",
                "unresolved_questions": ["Czy możecie podjechać?"],
                "commitments_made": [],
                "key_facts_so_far": [],
                "has_unanswered_question": True,
                "has_open_commitment": False,
                "thread_state": "active",
                "updated_at": "2026-01-01T10:00:00Z",
                "last_operator_action": "",
                "open_tasks_from_thread": [],
            },
            case_context_pack={"source_refs": [], "vector_retrieval": {}, "relevant_chunks": []},
        )
        uo = build_understanding_output(
            snapshot=snapshot,
            intake_result=intake_result,
            case_link_result=case_link,
            intelligence=ci,
            thread_memory=ci.get("thread_memory") or {},
            attachment_intelligence={},
            case_context_pack={"source_refs": [], "vector_retrieval": {}, "relevant_chunks": []},
            business_result=business,
        )
        self.assertEqual(uo["schema_version"], UNDERSTANDING_SCHEMA_VERSION)
        self.assertEqual(uo["source_signal_id"], "msg_test_1")
        self.assertIn("next_best_action_recommendation", uo)
        patched, errs = validate_understanding_invariants(uo)
        self.assertEqual(patched["schema_version"], UNDERSTANDING_SCHEMA_VERSION)
        self.assertIsInstance(errs, list)

    def test_situation_only_rejects_execution_semantics(self) -> None:
        from understanding_output import validate_understanding_invariants, validate_understanding_situation_only

        polluted = {
            "schema_version": UNDERSTANDING_SCHEMA_VERSION,
            "source_signal_id": "x",
            "situation_summary_pl": "s",
            "customer_intent_pl": "i",
            "policy_decision_id": "pdec_bad",
            "next_best_action_recommendation": {"action_type": "answer_customer", "title_pl": "X", "kind": "execute"},
        }
        diag = validate_understanding_situation_only(polluted)
        self.assertTrue(any("situation_only" in e for e in diag))
        self.assertIn("policy_decision_id", polluted)
        sample = dict(polluted)
        patched, errs = validate_understanding_invariants(sample)
        self.assertNotIn("policy_decision_id", patched)
        self.assertTrue(any("invalid_kind" in e for e in errs))

    def test_risk_without_message_gets_unsupported(self) -> None:
        snapshot = {"source_message": {}}
        uo = {
            "schema_version": UNDERSTANDING_SCHEMA_VERSION,
            "risks": [{"risk_type": "aging_risk", "severity": "medium", "reason_pl": "x", "confidence": 0.5}],
        }
        patched, errs = validate_understanding_invariants(uo)
        self.assertTrue(patched["risks"][0].get("unsupported"))


if __name__ == "__main__":
    unittest.main()
