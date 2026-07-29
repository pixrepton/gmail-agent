"""RC-D2 — evidence/state-aware commitment gating for reply drafts.

Product invariant (independent of any eval suite): a drafted customer reply must
never assert a commitment the case genuinely does not support — an already-
arranged visit that was never scheduled, a guarantee/certainty the business
cannot make, a specific delivery/installation deadline nobody confirmed, or work
claimed as already completed when it was not. Sending such a message creates
real customer-trust and liability exposure regardless of any benchmark.

The gate is driven by CASE STATE (``case_state``), not by a bare phrase
blacklist: the same matched phrase is rewritten when unsupported and left intact
when the case state actually supports it (false-positive AND false-negative
behavior are both tested). Rewriting touches only the offending fragment, so
ordinary helpful wording elsewhere in the same draft is never touched.
"""

from __future__ import annotations

import unittest

from reply_drafter import _draft_case_state, gate_reply_draft_commitments


def _parsed(body: str) -> dict:
    return {
        "draft_enabled": True,
        "drafts": [
            {
                "variant": "short_operational",
                "subject_suggestion": "Re: Sprawa",
                "body": body,
                "goal": "respond_safely",
                "tone": "operational",
            }
        ],
        "do_not_send_reasons": [],
        "recommended_variant": "short_operational",
        "requires_manual_edit": False,
        "confidence": 0.6,
    }


class UnsupportedCommitmentsAreRewritten(unittest.TestCase):
    def test_unsupported_deadline_is_rewritten(self) -> None:
        result = gate_reply_draft_commitments(_parsed("Na pewno wyslemy oferte jutro."), case_state={})
        body = result["drafts"][0]["body"]
        self.assertNotIn("na pewno", body.lower())
        self.assertTrue(result["requires_manual_edit"])
        self.assertTrue(result["do_not_send_reasons"])

    def test_unsupported_completed_action_claim_is_rewritten(self) -> None:
        result = gate_reply_draft_commitments(
            _parsed("Dzien dobry, juz wyslalismy oferte na Panstwa adres."), case_state={}
        )
        body = result["drafts"][0]["body"].lower()
        self.assertNotIn("juz wyslalismy", body)

    def test_unsupported_scheduled_visit_claim_is_rewritten(self) -> None:
        result = gate_reply_draft_commitments(
            _parsed("Dzien dobry, wizyta jest juz umowiona na jutro."), case_state={"visit_confirmed": False}
        )
        body = result["drafts"][0]["body"].lower()
        self.assertNotIn("umowiona", body)

    def test_unsupported_guarantee_language_is_rewritten(self) -> None:
        result = gate_reply_draft_commitments(_parsed("Gwarantujemy najlepsza cene na rynku."), case_state={})
        body = result["drafts"][0]["body"].lower()
        self.assertNotIn("gwarantujemy", body)


class SupportedCommitmentsRemainIntact(unittest.TestCase):
    def test_supported_scheduled_visit_claim_survives(self) -> None:
        result = gate_reply_draft_commitments(
            _parsed("Dzien dobry, wizyta jest juz umowiona na jutro."), case_state={"visit_confirmed": True}
        )
        body = result["drafts"][0]["body"].lower()
        self.assertIn("umowiona", body)
        self.assertFalse(result["requires_manual_edit"])

    def test_supported_deadline_survives(self) -> None:
        result = gate_reply_draft_commitments(
            _parsed("Zamontujemy urzadzenie jutro zgodnie z ustalonym terminem."),
            case_state={"deadline_confirmed": True},
        )
        body = result["drafts"][0]["body"].lower()
        self.assertIn("jutro", body)

    def test_supported_completed_action_claim_survives(self) -> None:
        result = gate_reply_draft_commitments(
            _parsed("Dzien dobry, juz wyslalismy oferte na Panstwa adres."),
            case_state={"action_completed": True},
        )
        body = result["drafts"][0]["body"].lower()
        self.assertIn("juz wyslalismy", body)


class NormalWordingUnaffected(unittest.TestCase):
    def test_normal_helpful_wording_is_unaffected(self) -> None:
        text = "Dzien dobry, dziekujemy za wiadomosc. Postaramy sie pomoc i wrocimy z odpowiedzia. Pozdrawiamy."
        result = gate_reply_draft_commitments(_parsed(text), case_state={})
        self.assertEqual(result["drafts"][0]["body"], text)
        self.assertFalse(result["requires_manual_edit"])
        self.assertEqual(result["do_not_send_reasons"], [])

    def test_mixed_body_only_offending_fragment_rewritten(self) -> None:
        text = "Dzien dobry, dziekujemy za zapytanie. Gwarantujemy najlepsza cene. Pozdrawiamy."
        result = gate_reply_draft_commitments(_parsed(text), case_state={})
        body = result["drafts"][0]["body"]
        self.assertIn("Dzien dobry, dziekujemy za zapytanie.", body)
        self.assertIn("Pozdrawiamy.", body)
        self.assertNotIn("Gwarantujemy", body)


class QuotedCustomerTextIsNotTreatedAsOwnCommitment(unittest.TestCase):
    def test_quoted_customer_claim_is_not_rewritten(self) -> None:
        text = 'Dzien dobry, w Panstwa wiadomosci napisali Panstwo: "wizyta jest juz umowiona na jutro". Sprawdzimy to.'
        result = gate_reply_draft_commitments(_parsed(text), case_state={})
        body = result["drafts"][0]["body"]
        self.assertIn("wizyta jest juz umowiona na jutro", body)
        self.assertFalse(result["requires_manual_edit"])

    def test_unquoted_commitment_outside_quote_is_still_rewritten(self) -> None:
        text = 'Cytujac Panstwa: "sprawa jest pilna". Gwarantujemy najlepsza cene.'
        result = gate_reply_draft_commitments(_parsed(text), case_state={})
        body = result["drafts"][0]["body"]
        self.assertIn("sprawa jest pilna", body)
        self.assertNotIn("Gwarantujemy", body)

    def test_unattributed_quotes_do_not_shield_the_companys_own_unsupported_claim(self) -> None:
        """Quote characters alone are not customer attribution: an LLM can wrap
        its OWN guarantee in quotes (for emphasis or to evade the gate) without
        ever attributing it to the customer's message. Only a quoted span that
        is actually preceded by an attribution cue (``napisali Panstwo``,
        ``cytujac Panstwa`` etc.) may be exempted."""
        text = 'Dzien dobry. "Gwarantujemy najlepsza cene na rynku." Pozdrawiamy.'
        result = gate_reply_draft_commitments(_parsed(text), case_state={})
        body = result["drafts"][0]["body"].lower()
        self.assertNotIn("gwarantujemy", body)

    def test_unattributed_quoted_scheduled_visit_claim_is_still_rewritten(self) -> None:
        text = 'Potwierdzamy: "wizyta jest juz umowiona na jutro".'
        result = gate_reply_draft_commitments(_parsed(text), case_state={"visit_confirmed": False})
        body = result["drafts"][0]["body"].lower()
        self.assertNotIn("umowiona", body)


class RealAuthoritativeEvidenceIsConsulted(unittest.TestCase):
    """The gate must not treat "no dedicated boolean field" as proof no
    confirmation exists anywhere -- it must consult real, existing structured
    case evidence (CaseContextPack.active_facts, persisted by the real
    schedule_visit / add_deadline write executors) before defaulting to
    unsupported."""

    def test_scheduled_visit_fact_in_case_context_pack_is_recognized(self) -> None:
        context_bundle = {
            "case_context_pack": {
                "active_facts": [
                    {"fact_key": "scheduled_visit", "normalized_value": "Date: 2026-02-01, Address: X"}
                ]
            }
        }
        state = _draft_case_state({}, {}, context_bundle)
        self.assertTrue(state["visit_confirmed"])

    def test_case_deadline_fact_in_case_context_pack_is_recognized(self) -> None:
        context_bundle = {
            "case_context_pack": {
                "active_facts": [{"fact_key": "case_deadline", "normalized_value": "Deadline: 2026-02-01"}]
            }
        }
        state = _draft_case_state({}, {}, context_bundle)
        self.assertTrue(state["deadline_confirmed"])

    def test_absence_of_fact_and_absence_of_boolean_field_stays_unsupported(self) -> None:
        context_bundle = {"case_context_pack": {"active_facts": []}}
        state = _draft_case_state({}, {}, context_bundle)
        self.assertFalse(state["visit_confirmed"])
        self.assertFalse(state["deadline_confirmed"])

    def test_unrelated_facts_do_not_falsely_grant_visit_confirmation(self) -> None:
        context_bundle = {
            "case_context_pack": {"active_facts": [{"fact_key": "customer_email", "normalized_value": "a@b.pl"}]}
        }
        state = _draft_case_state({}, {}, context_bundle)
        self.assertFalse(state["visit_confirmed"])

    def test_real_scheduled_visit_evidence_flows_through_the_gate_end_to_end(self) -> None:
        context_bundle = {
            "case_context_pack": {
                "active_facts": [{"fact_key": "scheduled_visit", "normalized_value": "Date: 2026-02-01"}]
            }
        }
        state = _draft_case_state({}, {}, context_bundle)
        result = gate_reply_draft_commitments(
            _parsed("Dzien dobry, wizyta jest juz umowiona na jutro."), case_state=state
        )
        body = result["drafts"][0]["body"].lower()
        self.assertIn("umowiona", body)


if __name__ == "__main__":
    unittest.main()
