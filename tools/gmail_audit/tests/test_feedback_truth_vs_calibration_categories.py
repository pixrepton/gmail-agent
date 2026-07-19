"""Calibration categories must not overlap adjudication truth kinds (rejected_fact_claim invariant)."""

from __future__ import annotations

import unittest

import feedback_event_contract as fec


class FeedbackTruthVsCalibrationTests(unittest.TestCase):
    def test_rejected_fact_claim_is_calibration_not_adjudication(self) -> None:
        cal = set(fec._calibration_values())
        adj = set(fec._adjudication_values())
        self.assertIn("rejected_fact_claim", cal)
        self.assertNotIn("rejected_fact_claim", adj)
        self.assertIn("invalidate_fact", adj)
        self.assertNotIn("invalidate_fact", cal)


if __name__ == "__main__":
    unittest.main()
