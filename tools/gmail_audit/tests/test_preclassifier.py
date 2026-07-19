from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from tests.fixture_helpers import run_fixture


class PreclassifierTests(unittest.TestCase):
    def test_skip_lane_fixture(self) -> None:
        result = run_fixture("obvious_noise")
        self.assertEqual(result["preclassification"]["lane"], "skip")

    def test_review_direct_lane_fixture(self) -> None:
        result = run_fixture("forwarded_review_chaos")
        self.assertEqual(result["preclassification"]["lane"], "review_direct")

    def test_reference_only_lane_fixture(self) -> None:
        result = run_fixture("reference_only_mail")
        self.assertEqual(result["preclassification"]["lane"], "reference_only")


if __name__ == "__main__":
    unittest.main()
