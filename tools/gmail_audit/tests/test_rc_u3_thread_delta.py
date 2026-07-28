"""RC-U3 — semantic thread delta from case state (not intake-action labels).

Product invariant (independent of any eval suite): thread_delta must represent
what actually CHANGED in the case (changed/contradictory fact, new document,
resolved/added gap, new commitment), materialized from the available case state
and carrying provenance. The generic canned change string ("Pojawil sie nowy
temat operacyjny...") must be a true last resort, not the normal outcome, and the
delta must not be derived from the intake decision-action label when a real
change exists.
"""

from __future__ import annotations

import unittest

from case_intelligence import build_case_intelligence
from understanding_output import build_understanding_output, validate_understanding_invariants


def _snapshot(message_id: str = "msg_rcu3") -> dict:
    return {
        "source_message": {
            "message_id": message_id,
            "thread_id": "thread_rcu3",
            "subject": "Aktualizacja",
            "body": "Tresc.",
            "date": "2026-01-01T10:00:00Z",
        },
        "context_messages": [],
    }


def _uo(*, pack: dict, attachment_intelligence: dict | None = None, thread_memory: dict | None = None, message_id: str = "msg_rcu3") -> dict:
    snapshot = _snapshot(message_id)
    intake_result = {
        "decision": {"action": "create_case"},
        "business_area": "sales",
        "priority": "medium",
        "case_assessment": {"case_family": "lead_opportunity", "interpretation": "Lead."},
        "thread": {"thread_id": "thread_rcu3"},
    }
    business = {"business_interpretation": "Klient aktualizuje dane.", "customer_state_guess": "active_case", "recommended_next_action": "reply", "urgency": "normal"}
    ci = build_case_intelligence(
        snapshot=snapshot,
        intake_result=intake_result,
        case_link_result={"decision": "linked", "confidence": 0.6},
        business_result=business,
        reply_result={"draft_enabled": False, "drafts": []},
        action_plan_result={"primary_action": "update_case", "confidence": 0.6},
        attachment_intelligence=attachment_intelligence or {},
        thread_memory=thread_memory or {},
        case_context_pack=pack,
    )
    uo = build_understanding_output(
        snapshot=snapshot,
        intake_result=intake_result,
        case_link_result={"decision": "linked", "confidence": 0.6},
        intelligence=ci,
        attachment_intelligence=attachment_intelligence or {},
        thread_memory=thread_memory or {},
        case_context_pack=pack,
        business_result=business,
    )
    patched, _ = validate_understanding_invariants(uo)
    return patched


_CANNED = "nowy temat operacyjny"


def _area_conflict_pack() -> dict:
    return {
        "source_refs": [],
        "vector_retrieval": {},
        "relevant_chunks": [],
        "conflicting_facts": [
            {
                "field_name": "heated_area_m2",
                "summary_pl": "Powierzchnia domu zmienila sie: 120 vs 160 m2.",
                "evidence_refs": [{"source_type": "gmail_message", "source_id": "msg_rcu3"}],
            }
        ],
    }


class SemanticThreadDelta(unittest.TestCase):
    def test_changed_fact_conflict_materialized_in_changes(self) -> None:
        uo = _uo(pack=_area_conflict_pack())
        delta = uo["thread_delta"]
        types = {str(c.get("change_type") or "") for c in delta.get("changes") or []}
        self.assertIn("changed_or_conflicting_fact", types)

    def test_delta_summary_not_canned_when_change_exists(self) -> None:
        uo = _uo(pack=_area_conflict_pack())
        summary = uo["thread_delta"]["operator_visible_delta_summary"].lower()
        self.assertNotIn(_CANNED, summary)
        self.assertIn("120", summary)

    def test_new_conflicts_populated_from_conflict_summary(self) -> None:
        uo = _uo(pack=_area_conflict_pack())
        new_conflicts = [c for c in uo["thread_delta"].get("new_conflicts") or [] if str(c).strip()]
        self.assertTrue(new_conflicts)

    def test_changed_fact_change_carries_evidence(self) -> None:
        uo = _uo(pack=_area_conflict_pack())
        change = next(c for c in uo["thread_delta"]["changes"] if c.get("change_type") == "changed_or_conflicting_fact")
        self.assertTrue(change.get("evidence_refs"))

    def test_new_document_signal_in_delta(self) -> None:
        uo = _uo(
            pack={"source_refs": [], "vector_retrieval": {}, "relevant_chunks": []},
            attachment_intelligence={"combined_risk_flags": ["Nowy protokol serwisowy w zalaczniku"]},
        )
        types = {str(c.get("change_type") or "") for c in uo["thread_delta"].get("changes") or []}
        self.assertIn("new_document_signal", types)

    def test_no_meaningful_delta_falls_back_to_canned(self) -> None:
        uo = _uo(pack={"source_refs": [], "vector_retrieval": {}, "relevant_chunks": []})
        delta = uo["thread_delta"]
        self.assertEqual([], delta.get("changes") or [])
        self.assertIn(_CANNED, delta["operator_visible_delta_summary"].lower())


class TemporalDeltaBoundary(unittest.TestCase):
    """A conflict/change must be attributable to the CURRENT signal's own
    evidence -- not merely present anywhere in the case's accumulated state --
    otherwise the same pre-existing conflict resurfaces as "new" delta forever."""

    def test_same_signal_replay_is_idempotent(self) -> None:
        pack = _area_conflict_pack()
        first = _uo(pack=pack)["thread_delta"]
        second = _uo(pack=pack)["thread_delta"]
        self.assertEqual(first["changes"], second["changes"])
        self.assertEqual(len(first["changes"]), 1)

    def test_preexisting_conflict_not_resurfaced_by_unrelated_new_message(self) -> None:
        # Same pack (same persisted conflict, evidence still pointing at the
        # OLD message "msg_rcu3"), but the CURRENT signal is a different,
        # unrelated message ("msg_unrelated"). The old conflict must not be
        # reported as this turn's delta.
        pack = _area_conflict_pack()
        uo = _uo(pack=pack, message_id="msg_unrelated")
        delta = uo["thread_delta"]
        types = {str(c.get("change_type") or "") for c in delta.get("changes") or []}
        self.assertNotIn("changed_or_conflicting_fact", types)
        self.assertEqual([], delta.get("new_conflicts") or [])

    def test_current_signal_contradiction_is_the_one_materialized(self) -> None:
        # Two conflicts in the pack: one old (evidence -> a prior message), one
        # genuinely introduced by THIS signal (evidence -> the current message).
        # Only the current-signal one should appear in changes.
        pack = {
            "source_refs": [],
            "vector_retrieval": {},
            "relevant_chunks": [],
            "conflicting_facts": [
                {
                    "field_name": "old_field",
                    "summary_pl": "Stary konflikt sprzed kilku tur.",
                    "evidence_refs": [{"source_type": "gmail_message", "source_id": "msg_ancient"}],
                },
                {
                    "field_name": "heated_area_m2",
                    "summary_pl": "Powierzchnia domu zmienila sie: 120 vs 160 m2.",
                    "evidence_refs": [{"source_type": "gmail_message", "source_id": "msg_rcu3"}],
                },
            ],
        }
        uo = _uo(pack=pack, message_id="msg_rcu3")
        delta = uo["thread_delta"]
        summaries = [c["summary_pl"] for c in delta["changes"]]
        self.assertTrue(any("160" in s for s in summaries))
        self.assertFalse(any("Stary konflikt" in s for s in summaries))

    def test_conflict_without_current_signal_evidence_stays_out_of_changes(self) -> None:
        pack = {
            "source_refs": [],
            "vector_retrieval": {},
            "relevant_chunks": [],
            "conflicting_facts": [
                {
                    "field_name": "heated_area_m2",
                    "summary_pl": "Konflikt bez powiazania z biezacym sygnalem.",
                    "evidence_refs": [{"source_type": "gmail_message", "source_id": "msg_other"}],
                }
            ],
        }
        uo = _uo(pack=pack, message_id="msg_rcu3")
        types = {str(c.get("change_type") or "") for c in uo["thread_delta"].get("changes") or []}
        self.assertNotIn("changed_or_conflicting_fact", types)


if __name__ == "__main__":
    unittest.main()
