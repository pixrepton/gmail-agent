"""INTAKE-NOISE-01: 'reklamacja' must not be admitted as noise via substring match on 'reklama'.

Covers the two independently-maintained active admission gates that scan subject/body
keyword lists with plain substring containment before business reasoning runs:
  - preclassifier.is_obvious_noise (subject-only, sets lane="skip")
  - agent_runtime.agent_reconcile._evaluate_cost_gate (subject+body, sets cost_gate skip)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.agent_reconcile import _evaluate_cost_gate
from preclassifier import is_obvious_noise, preclassify_snapshot
from signal_contract import CanonicalSignal


def _snapshot(*, subject: str = "", body: str = "", sender: str = "klient@example.com") -> dict:
    return {
        "source_message": {
            "sender": sender,
            "subject": subject,
            "body": body,
            "snippet": body,
        },
        "thread_context": {"quality": "normal"},
        "routing_hints": {},
    }


def _gmail_signal(**overrides) -> CanonicalSignal:
    defaults = dict(
        signal_id="sig-intake-noise-01",
        schema_version="1",
        signal_kind="gmail_message_observed",
        source_kind="gmail_inbound",
        source_ref={"message_id": "msg-intake-noise-01"},
        observed_at="2026-07-15T12:00:00Z",
        effective_at=None,
        case_key_hint=None,
        thread_key_hint=None,
        business_lane=None,
        signal_summary_pl="Test signal",
        payload={},
        artifacts={},
        processing_state="pending",
        idempotency_key="idem-intake-noise-01",
        content_hash=None,
        replayable=True,
        created_by_runtime="test",
    )
    defaults.update(overrides)
    return CanonicalSignal(**defaults)


class PreclassifierReklamaSubstringTests(unittest.TestCase):
    """RED-1: subject-level preclassifier gate must not route a complaint to lane=skip."""

    def test_reklamacja_subject_is_not_obvious_noise(self) -> None:
        snapshot = _snapshot(
            subject="Reklamacja montazu",
            body="Prosze o kontakt w sprawie reklamacji montazu klimatyzacji, instalacja nie dziala poprawnie od tygodnia.",
        )
        self.assertFalse(
            is_obvious_noise(snapshot),
            "subject containing 'reklamacja' must not match noise keyword 'reklama' as a substring",
        )

    def test_reklamacja_subject_lane_is_not_skip(self) -> None:
        snapshot = _snapshot(
            subject="Reklamacja montazu",
            body="Prosze o kontakt w sprawie reklamacji montazu klimatyzacji, instalacja nie dziala poprawnie od tygodnia.",
        )
        result = preclassify_snapshot(snapshot)
        self.assertNotEqual(result["lane"], "skip", f"business complaint wrongly routed to skip lane: {result}")

    def test_zgloszenie_reklamacyjne_subject_is_not_obvious_noise(self) -> None:
        snapshot = _snapshot(subject="Zgloszenie reklamacyjne - klimatyzator Panasonic")
        self.assertFalse(is_obvious_noise(snapshot))

    def test_real_marketing_reklama_subject_is_still_obvious_noise(self) -> None:
        """Guardrail: fixing the false positive must not disable the true-positive noise case."""
        snapshot = _snapshot(subject="Wielka reklama wiosennej promocji!", body="Sprawdz nasza reklame i promocje sezonowa.")
        self.assertTrue(is_obvious_noise(snapshot), "standalone marketing 'reklama' must still be classified as noise")


class CostGateReklamaSubstringTests(unittest.TestCase):
    """RED-2: agent-runtime cost gate must not flag a complaint as spam_indicator:reklama."""

    def test_reklamacja_complaint_is_not_flagged_as_spam(self) -> None:
        signal = _gmail_signal()
        intake = {
            "message": {
                "subject": "Reklamacja montazu",
                "body_text": "Prosze o kontakt w sprawie reklamacji montazu klimatyzacji, instalacja nie dziala poprawnie.",
            }
        }
        result = _evaluate_cost_gate(signal, intake)
        self.assertFalse(result.get("skip"), f"business complaint wrongly cost-gated as spam: {result}")
        self.assertNotEqual(result.get("reason"), "spam_indicator:reklama")

    def test_real_marketing_reklama_is_still_flagged_as_spam(self) -> None:
        """Guardrail: fixing the false positive must not disable the true-positive spam case."""
        signal = _gmail_signal()
        intake = {
            "message": {
                "subject": "Wielka reklama wiosennej promocji!",
                "body_text": "Sprawdz nasza reklame i promocje sezonowa.",
            }
        }
        result = _evaluate_cost_gate(signal, intake)
        self.assertTrue(result.get("skip"))
        self.assertEqual(result.get("reason"), "spam_indicator:reklama")


# ── Small targeted regression corpus for this failure class only ──────
# Not EVAL-1, not a general spam benchmark: cases are limited to what the
# reklama/reklamacja substring-collision investigation actually surfaced.

BUSINESS_SIGNAL_CASES = [
    {
        "id": "reklamacja_montazu",
        "subject": "Reklamacja montazu",
        "body": "Prosze o kontakt w sprawie reklamacji montazu klimatyzacji.",
    },
    {
        "id": "zgloszenie_reklamacyjne",
        "subject": "Zgloszenie reklamacyjne",
        "body": "Zglaszam usterke pompy ciepla, prosze o rozpatrzenie reklamacji.",
    },
    {
        "id": "proces_reklamacyjny",
        "subject": "Proces reklamacyjny - zlecenie 4821",
        "body": "Chcielibysmy uruchomic proces reklamacyjny dla instalacji wykonanej w marcu.",
    },
    {
        "id": "reklamacja_body_only",
        "subject": "Pytanie w sprawie serwisu",
        "body": "Skladam reklamacje na prace montazowe z kwietnia, urzadzenie nie grzeje.",
    },
]

REAL_NOISE_CASES = [
    {
        "id": "marketing_reklama_standalone",
        "subject": "Wielka reklama wiosennej promocji!",
        "body": "Sprawdz nasza reklame i promocje sezonowa.",
        "expect_spam_indicator": "reklama",
    },
    {
        "id": "newsletter_classic",
        "subject": "Newsletter April updates",
        "body": "Newsletter April updates. Read our monthly newsletter and unsubscribe anytime.",
        "expect_spam_indicator": "unsubscribe",  # first list match: "unsubscribe" precedes "newsletter" in spam_indicators
    },
    {
        "id": "oferta_handlowa_marketing",
        "subject": "Oferta handlowa - nowosci w ofercie",
        "body": "Sprawdz nasza najnowsza oferte handlowa i promocje.",
        "expect_spam_indicator": None,  # caught by preclassifier subject pattern, not the cost-gate list
    },
]

BOUNDARY_CASES = [
    {"id": "standalone_lowercase", "subject": "reklama", "expect_noise": True},
    {"id": "standalone_uppercase", "subject": "REKLAMA", "expect_noise": True},
    {"id": "trailing_punctuation", "subject": "Zobacz nasza reklama!", "expect_noise": True},
    {"id": "leading_position", "subject": "Reklama - zobacz nowosci", "expect_noise": True},
    {"id": "prefix_of_longer_word_bare", "subject": "Reklamacja", "expect_noise": False},
    {"id": "prefix_of_longer_word_adjective", "subject": "Wniosek reklamacyjny", "expect_noise": False},
    {"id": "prefix_of_longer_word_mid_sentence", "subject": "Pilna reklamacja - prosze o odpowiedz", "expect_noise": False},
]


class RegressionCorpusBusinessSignalsTests(unittest.TestCase):
    """5A: valid business signals must clear both admission gates."""

    def test_business_signals_clear_preclassifier(self) -> None:
        for case in BUSINESS_SIGNAL_CASES:
            with self.subTest(case=case["id"]):
                snapshot = _snapshot(subject=case["subject"], body=case["body"])
                self.assertFalse(is_obvious_noise(snapshot), case)
                result = preclassify_snapshot(snapshot)
                self.assertNotEqual(result["lane"], "skip", (case, result))

    def test_business_signals_clear_cost_gate(self) -> None:
        for case in BUSINESS_SIGNAL_CASES:
            with self.subTest(case=case["id"]):
                signal = _gmail_signal()
                intake = {"message": {"subject": case["subject"], "body_text": case["body"]}}
                result = _evaluate_cost_gate(signal, intake)
                self.assertFalse(result.get("skip"), (case, result))


class RegressionCorpusRealNoiseTests(unittest.TestCase):
    """5B: real noise must still be rejected under the existing policy."""

    def test_real_noise_still_rejected_by_preclassifier(self) -> None:
        for case in REAL_NOISE_CASES:
            with self.subTest(case=case["id"]):
                snapshot = _snapshot(subject=case["subject"], body=case["body"])
                self.assertTrue(is_obvious_noise(snapshot), case)

    def test_real_noise_still_rejected_by_cost_gate_where_listed(self) -> None:
        for case in REAL_NOISE_CASES:
            if case["expect_spam_indicator"] is None:
                continue
            with self.subTest(case=case["id"]):
                signal = _gmail_signal()
                intake = {"message": {"subject": case["subject"], "body_text": case["body"]}}
                result = _evaluate_cost_gate(signal, intake)
                self.assertTrue(result.get("skip"), (case, result))
                self.assertEqual(result.get("reason"), f"spam_indicator:{case['expect_spam_indicator']}")


class RegressionCorpusBoundaryTests(unittest.TestCase):
    """5C: exact boundary contract of the subject-level noise matcher."""

    def test_boundary_cases(self) -> None:
        for case in BOUNDARY_CASES:
            with self.subTest(case=case["id"]):
                snapshot = _snapshot(subject=case["subject"])
                self.assertEqual(is_obvious_noise(snapshot), case["expect_noise"], case)


if __name__ == "__main__":
    unittest.main()
