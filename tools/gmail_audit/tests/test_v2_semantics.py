from __future__ import annotations

import sys
import unittest
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from v2_semantics import command_from_lifecycle_intent, decision_type_from_command, is_case_only_transition


class V2SemanticsTests(unittest.TestCase):
    def test_move_to_case_only_keeps_distinct_trace_semantics(self) -> None:
        command = command_from_lifecycle_intent("move_to_case_only", target_zone="case_only")
        decision_type = decision_type_from_command(command, lifecycle_intent="move_to_case_only", target_zone="case_only")

        self.assertEqual(command, "deescalate_presence")
        self.assertEqual(decision_type, "move_to_case_only")
        self.assertTrue(is_case_only_transition("move_to_case_only", "case_only"))

    def test_deescalate_presence_maps_to_persistence_and_trace(self) -> None:
        command = command_from_lifecycle_intent("deescalate_presence")
        decision_type = decision_type_from_command(command, lifecycle_intent="deescalate_presence")

        self.assertEqual(command, "deescalate_presence")
        self.assertEqual(decision_type, "deescalate_presence_note")
        self.assertFalse(is_case_only_transition("deescalate_presence", "desk"))

    def test_resolve_maps_to_resolve_note(self) -> None:
        self.assertEqual(command_from_lifecycle_intent("resolve"), "resolve")
        self.assertEqual(decision_type_from_command("resolve", lifecycle_intent="resolve"), "resolve_note")

    def test_withdraw_maps_to_withdraw_note(self) -> None:
        self.assertEqual(command_from_lifecycle_intent("withdraw"), "withdraw")
        self.assertEqual(decision_type_from_command("withdraw", lifecycle_intent="withdraw"), "withdraw_note")


if __name__ == "__main__":
    unittest.main()
