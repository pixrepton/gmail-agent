"""The legal-escalation rule must catch legal threats, not everyone who knows a lawyer.

`REVIEW_DIRECT_RISK_PATTERNS` used to contain the bare tokens "prawnik"/"prawnika", matched by
plain substring. Any incidental mention of a lawyer therefore routed a message straight to
`review_direct`, skipping the reasoning lane entirely -- including ordinary sales leads. The
frozen SUT manifest flagged this as a possible over-broad product judgment; these negatives
proved it, so the rule now requires a legal actor to appear together with an adversarial cue.

This is a general semantic contract. Nothing here is tied to a specific corpus case.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from preclassifier import preclassify_snapshot  # noqa: E402


def _snapshot(subject: str, body: str) -> dict:
    return {
        "source_message": {
            "subject": subject,
            "body": body,
            "sender": "klient@example.com",
        },
        "thread_context": {"quality": "strong"},
        "routing_hints": {},
    }


class LegalEscalationScope(unittest.TestCase):
    def assert_escalates(self, subject: str, body: str) -> None:
        result = preclassify_snapshot(_snapshot(subject, body))
        self.assertEqual(result["lane"], "review_direct", f"expected escalation for: {body!r}")
        self.assertIn("legal_or_contract_escalation_signal", result["reasons"])

    def assert_does_not_escalate(self, subject: str, body: str) -> None:
        result = preclassify_snapshot(_snapshot(subject, body))
        self.assertNotIn(
            "legal_or_contract_escalation_signal",
            result["reasons"],
            f"benign lawyer mention must not be treated as a legal threat: {body!r}",
        )

    # ── negatives: a lawyer is mentioned, but nothing adversarial is happening ──────────

    def test_lawyer_as_referral_source_is_a_normal_lead(self) -> None:
        self.assert_does_not_escalate(
            "Zapytanie o pompe ciepla",
            "Dzien dobry, moj sasiad jest prawnikiem i polecil Panstwa firme. "
            "Prosze o wycene pompy ciepla dla domu 150 m2.",
        )

    def test_sender_being_a_lawyer_is_a_normal_lead(self) -> None:
        self.assert_does_not_escalate(
            "Wycena pompy ciepla",
            "Dzien dobry, jestem prawnikiem, potrzebuje wyceny pompy ciepla do domu 180 m2 w Krakowie.",
        )

    def test_incidental_bookkeeping_mention_is_not_a_threat(self) -> None:
        self.assert_does_not_escalate(
            "Faktura",
            "Prosze o przeslanie faktury na kancelarie. Nasz prawnik prowadzi ksiegowosc.",
        )

    def test_adwokat_referral_is_not_a_threat(self) -> None:
        self.assert_does_not_escalate(
            "Polecenie",
            "Adwokat z sasiedztwa polecil Panstwa uslugi, prosze o oferte na rekuperacje.",
        )

    # ── positives: the escalation signal must survive the tightening ───────────────────

    def test_lawyer_plus_court_escalates(self) -> None:
        self.assert_escalates("Sprawa", "Moj prawnik skieruje sprawe do sadu jesli nie bedzie reakcji.")

    def test_lawyer_plus_damages_claim_escalates(self) -> None:
        self.assert_escalates("Roszczenie", "Przekazalem sprawe prawnikowi, zglaszam roszczenie o odszkodowanie.")

    def test_lawyer_plus_legal_steps_escalates(self) -> None:
        self.assert_escalates("Kroki", "Jesli nie otrzymam odpowiedzi, moj adwokat podejmie kroki prawne.")

    def test_turning_to_a_lawyer_still_escalates_on_its_own(self) -> None:
        self.assert_escalates(
            "Wezwanie",
            "W zwiazku z brakiem realizacji rozwazam zwrocenie sie do prawnika.",
        )

    def test_lawsuit_escalates_without_any_lawyer_mention(self) -> None:
        self.assert_escalates("Pozew", "Skladam pozew w tej sprawie.")

    def test_pre_court_demand_escalates(self) -> None:
        self.assert_escalates("Wezwanie", "Otrzymaja Panstwo wezwanie przedsadowe.")

    def test_contract_termination_escalates(self) -> None:
        self.assert_escalates("Umowa", "Chce zerwanie umowy natychmiast.")

    def test_contract_withdrawal_escalates(self) -> None:
        self.assert_escalates("Umowa", "Skladam odstapienie od umowy.")


if __name__ == "__main__":
    unittest.main()
