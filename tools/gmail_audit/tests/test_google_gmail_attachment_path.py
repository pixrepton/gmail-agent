from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from google_gmail_api import _gmail_attachment_resource_path  # noqa: E402


class GmailAttachmentPathTests(unittest.TestCase):
    def test_resource_path_percent_encodes_reserved_characters(self) -> None:
        path = _gmail_attachment_resource_path("msg+id/with/slash", "att+part")
        self.assertIn("%2B", path)
        self.assertIn("%2F", path)
        self.assertTrue(path.startswith("/messages/"))
        self.assertIn("/attachments/", path)


if __name__ == "__main__":
    unittest.main()
