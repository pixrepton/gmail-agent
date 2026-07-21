"""RC-D1 — draft structured output contract normalization.

Product invariant (independent of any eval suite): the runtime's canonical draft
contract is top-level {draft_enabled, drafts[].{subject_suggestion,body,goal}}.
The checkpoint found the model reliably emitting semantically-equivalent
alternate serializations of the same content (nested "reply_draft_v1" envelope,
"subject" instead of "subject_suggestion") that were previously silently dropped
to an empty draft ("draft_enabled": false with 0 drafts) even though the raw text
contained a perfectly good, safe, on-topic reply. That is a real product defect:
a valid drafted reply becomes invisible to the operator.

This is fixed with a narrow, explicit normalization boundary at the entry point
(intake_schema._normalize_reply_draft_shape, used by validate_reply_draft_result)
— not by loosening the strict Pydantic contract the LLM call itself uses, and not
by inventing/fabricating any field content.

Genuinely malformed structures (no body anywhere, not a dict/list at all) must
still be rejected/produce an empty, safe draft — never silently fabricated.
"""

from __future__ import annotations

import unittest

from intake_schema import validate_reply_draft_result


class CanonicalShapeAccepted(unittest.TestCase):
    def test_canonical_valid_draft_parses(self) -> None:
        raw = {
            "draft_enabled": True,
            "drafts": [
                {
                    "variant": "short_operational",
                    "subject_suggestion": "Re: Zapytanie",
                    "body": "Dzien dobry, dziekujemy za wiadomosc.",
                    "goal": "acknowledge",
                }
            ],
            "do_not_send_reasons": [],
        }
        result = validate_reply_draft_result(raw)
        self.assertTrue(result["draft_enabled"])
        self.assertEqual(result["drafts"][0]["body"], "Dzien dobry, dziekujemy za wiadomosc.")
        self.assertEqual(result["drafts"][0]["subject_suggestion"], "Re: Zapytanie")


class KnownAlternateShapesNormalize(unittest.TestCase):
    def test_nested_reply_draft_v1_envelope_normalizes(self) -> None:
        raw = {
            "reply_draft_v1": {
                "draft_enabled": True,
                "drafts": [
                    {
                        "variant": "short_operational",
                        "subject_suggestion": "Re: Serwis",
                        "body": "Dzien dobry, przyjmujemy zgloszenie serwisowe.",
                        "goal": "acknowledge",
                    }
                ],
                "do_not_send_reasons": [],
            }
        }
        result = validate_reply_draft_result(raw)
        self.assertTrue(result["draft_enabled"])
        self.assertEqual(len(result["drafts"]), 1)
        self.assertIn("serwisowe", result["drafts"][0]["body"])

    def test_subject_key_maps_to_subject_suggestion(self) -> None:
        raw = {
            "draft_enabled": True,
            "drafts": [
                {
                    "variant": "customer_friendly",
                    "subject": "Re: Wycena",
                    "body": "Dzien dobry, przygotujemy wycene.",
                    "goal": "quote",
                }
            ],
            "do_not_send_reasons": [],
        }
        result = validate_reply_draft_result(raw)
        self.assertTrue(result["draft_enabled"])
        self.assertEqual(result["drafts"][0]["subject_suggestion"], "Re: Wycena")

    def test_no_silent_field_loss_for_normalized_shape(self) -> None:
        raw = {
            "reply_draft_v1": {
                "draft_enabled": True,
                "drafts": [
                    {
                        "variant": "short_operational",
                        "subject": "Re: Pytanie",
                        "body": "Tresc odpowiedzi klientowi.",
                        "goal": "answer_question",
                        "tone": "operational",
                    }
                ],
                "do_not_send_reasons": ["missing_confirmation"],
                "recommended_variant": "short_operational",
                "confidence": 0.6,
            }
        }
        result = validate_reply_draft_result(raw)
        draft = result["drafts"][0]
        self.assertEqual(draft["subject_suggestion"], "Re: Pytanie")
        self.assertEqual(draft["goal"], "answer_question")
        self.assertEqual(draft["tone"], "operational")
        self.assertEqual(result["do_not_send_reasons"], ["missing_confirmation"])
        self.assertEqual(result["recommended_variant"], "short_operational")
        self.assertGreater(result["confidence"], 0.0)


class MalformedStructuresRemainRejected(unittest.TestCase):
    def test_empty_object_produces_safe_empty_draft(self) -> None:
        result = validate_reply_draft_result({})
        self.assertFalse(result["draft_enabled"])
        self.assertEqual(result["drafts"], [])

    def test_non_dict_top_level_raises(self) -> None:
        with self.assertRaises(Exception):
            validate_reply_draft_result(None)

    def test_draft_item_without_any_body_is_dropped_not_fabricated(self) -> None:
        raw = {
            "draft_enabled": True,
            "drafts": [{"variant": "short_operational", "subject_suggestion": "Re: X", "goal": "ack"}],
            "do_not_send_reasons": [],
        }
        result = validate_reply_draft_result(raw)
        self.assertEqual(result["drafts"], [])
        self.assertFalse(result["draft_enabled"])

    def test_unrecognized_envelope_key_with_no_body_anywhere_is_empty(self) -> None:
        raw = {"some_other_wrapper": {"nonsense": True}}
        result = validate_reply_draft_result(raw)
        self.assertFalse(result["draft_enabled"])
        self.assertEqual(result["drafts"], [])


if __name__ == "__main__":
    unittest.main()
