"""Tests for evidence_ref normalization."""

from __future__ import annotations

import unittest

from evidence_ref import (
    FORBIDDEN_PROJECTION_KEYS,
    assert_no_forbidden_projection_keys,
    merge_evidence_refs,
    normalize_case_guidance_evidence_refs,
    normalize_evidence_ref,
    normalize_evidence_refs,
)


class EvidenceRefTests(unittest.TestCase):
    def test_normalize_defaults(self) -> None:
        r = normalize_evidence_ref({})
        self.assertEqual(r["source_type"], "unknown")
        self.assertEqual(r["evidence_role"], "supports")
        self.assertEqual(r["trust_level"], "unknown")
        self.assertEqual(r["freshness"], "unknown")
        self.assertFalse(r["can_answer_customer"])

    def test_normalize_strips_excerpt(self) -> None:
        r = normalize_evidence_ref({"source_type": "gmail_message", "source_id": "x", "excerpt": "do not leak"})
        self.assertNotIn("excerpt", r)
        self.assertEqual(r["source_id"], "x")

    def test_normalize_strips_dangerous_keys(self) -> None:
        r = normalize_evidence_ref({"source_type": "gmail_message", "source_id": "x", "snippet": "secret", "body": "b"})
        self.assertNotIn("snippet", r)
        self.assertNotIn("body", r)
        self.assertEqual(r["source_id"], "x")

    def test_used_for_survives(self) -> None:
        r = normalize_evidence_ref({"source_type": "drive_document", "source_id": "d1", "used_for": "policy_basis"})
        self.assertEqual(r.get("used_for"), "policy_basis")

    def test_precedent_implies_low_trust_when_unspecified(self) -> None:
        r = normalize_evidence_ref({"source_type": "mailbox_memory", "source_id": "sim-1", "evidence_role": "precedent"})
        self.assertEqual(r["trust_level"], "low")

    def test_merge_dedupes(self) -> None:
        a = {"source_type": "gmail_message", "message_id": "m1", "evidence_role": "supports"}
        b = {"source_type": "gmail_message", "message_id": "m1", "evidence_role": "supports"}
        m = merge_evidence_refs([a], [b])
        self.assertEqual(len(m), 1)

    def test_normalize_evidence_refs_list_matches_contract_import(self) -> None:
        from case_context_contract import normalize_evidence_refs as ncc

        self.assertIs(normalize_evidence_refs, ncc)

    def test_normalize_case_guidance_strips_excerpt_and_caps_llm_trust(self) -> None:
        rows = [{"source_id": "m1", "excerpt": "secret", "trust_level": "high", "can_answer_customer": True}]
        out = normalize_case_guidance_evidence_refs(rows, source_mode="llm_reasoned")
        self.assertEqual(len(out), 1)
        self.assertNotIn("excerpt", out[0])
        self.assertEqual(out[0]["trust_level"], "low")
        self.assertFalse(out[0]["can_answer_customer"])

    def test_forbidden_keys_detected(self) -> None:
        bad = {"operator_explanation": {"essence_pl": "x", "raw_prompt": "no"}}
        errs = assert_no_forbidden_projection_keys(bad)
        self.assertTrue(any("raw_prompt" in e for e in errs))

    def test_forbidden_constant_nonempty(self) -> None:
        self.assertIn("raw_prompt", FORBIDDEN_PROJECTION_KEYS)


if __name__ == "__main__":
    unittest.main()
