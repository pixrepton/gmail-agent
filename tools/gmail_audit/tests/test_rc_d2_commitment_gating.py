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

from reply_drafter import gate_reply_draft_commitments


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


if __name__ == "__main__":
    unittest.main()
