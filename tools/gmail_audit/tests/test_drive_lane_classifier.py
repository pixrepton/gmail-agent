from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from drive_lane_classifier import classify_candidate


class DriveLaneClassifierTests(unittest.TestCase):
    def test_contract_file_classifies_to_case_specific_contract(self) -> None:
        result = classify_candidate(
            title="Umowa ZAM-3.pdf",
            mime_type="application/pdf",
            folder_path="Skany/Umowy/Zator",
            is_folder=False,
        )

        self.assertEqual(result["lane"], "formal_contracts")
        self.assertEqual(result["document_kind"], "contract")
        self.assertEqual(result["scope"], "case_specific")
        self.assertTrue(result["classification_confidence"] >= 0.76)

    def test_pricing_workbook_classifies_to_company_reference(self) -> None:
        result = classify_candidate(
            title="date-base33 koszty Panasonic.xlsx",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            folder_path="Cenniki/Panasonic",
            is_folder=False,
        )

        self.assertEqual(result["lane"], "commercial_pricing")
        self.assertEqual(result["document_kind"], "pricing_workbook")
        self.assertEqual(result["scope"], "company_reference")

    def test_case_media_folder_becomes_media_bundle(self) -> None:
        result = classify_candidate(
            title="Zdjecia montazu",
            mime_type="application/vnd.google-apps.folder",
            folder_path="Realizacje/Siedlec",
            is_folder=True,
        )

        self.assertEqual(result["lane"], "case_folder")
        self.assertEqual(result["document_kind"], "media_bundle")
        self.assertEqual(result["scope"], "case_specific")


if __name__ == "__main__":
    unittest.main()
