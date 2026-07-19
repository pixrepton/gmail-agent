from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_identity import derive_canonical_case_id
from case_intelligence import _resolve_case_id
from mailbox_memory_runtime import derive_case_id


class CaseIdentityTests(unittest.TestCase):
    def test_thread_case_key_alias_matches_bare_thread_anchor(self) -> None:
        snapshot = {
            "source_message": {
                "message_id": "msg-thread-1",
                "thread_id": "thr-thread-1",
                "reference_tokens": {},
            }
        }
        intake_result = {"case_assessment": {"case_family": "unknown"}}

        from_thread = derive_case_id(
            snapshot=snapshot,
            intake_result=intake_result,
            case_link_result={"selected_case_key": ""},
        )
        from_thread_alias = derive_case_id(
            snapshot=snapshot,
            intake_result=intake_result,
            case_link_result={"selected_case_key": "thread:thr-thread-1"},
        )

        self.assertEqual(from_thread, from_thread_alias)

    def test_case_intelligence_uses_same_case_id_as_mailbox_memory_runtime(self) -> None:
        snapshot = {
            "source_message": {
                "message_id": "msg-thread-2",
                "thread_id": "thr-thread-2",
                "reference_tokens": {},
            }
        }
        intake_result = {
            "case_assessment": {"case_family": "supplier_commercial_review"},
            "thread": {"thread_id": "thr-thread-2"},
            "decision": {"action": "create_case"},
        }
        case_link_result = {"selected_case_key": "thread:thr-thread-2"}

        runtime_case_id = derive_case_id(
            snapshot=snapshot,
            intake_result=intake_result,
            case_link_result=case_link_result,
        )
        intelligence_case_id = _resolve_case_id(
            intake_result=intake_result,
            case_link_result=case_link_result,
        )

        self.assertEqual(runtime_case_id, intelligence_case_id)

    def test_projected_case_key_alias_is_canonicalized(self) -> None:
        case_id = derive_canonical_case_id(
            case_family="unknown",
            projected_case_key="thread:thr-thread-3",
            thread_id="thr-thread-3",
        )
        direct = derive_canonical_case_id(
            case_family="unknown",
            thread_id="thr-thread-3",
        )

        self.assertEqual(case_id, direct)


if __name__ == "__main__":
    unittest.main()
