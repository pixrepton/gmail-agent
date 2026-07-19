from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from intake_policy import CHECK_STATUS_SKIPPED  # noqa: E402
from mailbox_memory_health import check_mailbox_memory_database  # noqa: E402


class MailboxMemoryHealthTests(unittest.TestCase):
    def test_check_skips_when_url_empty(self) -> None:
        result = check_mailbox_memory_database("")
        self.assertEqual(result.get("status"), CHECK_STATUS_SKIPPED)


if __name__ == "__main__":
    unittest.main()
