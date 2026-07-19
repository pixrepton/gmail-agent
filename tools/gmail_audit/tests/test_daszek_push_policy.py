"""Tests for Daszek v1 live push vs v2 operator projection policy."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from daszek_push_policy import evaluate_live_push_policy, evaluate_operator_projection_policy


def _ignore_safe_plan() -> dict:
    return {
        "primary_action": "ignore",
        "safe_for_live_push": True,
        "safe_for_operator_projection": False,
    }


def _create_task_unsafe_plan() -> dict:
    return {
        "primary_action": "create_task",
        "safe_for_live_push": False,
        "safe_for_operator_projection": True,
    }


def _hold_operator_visible_plan() -> dict:
    return {
        "primary_action": "hold",
        "safe_for_live_push": False,
        "safe_for_operator_projection": True,
    }


class DaszekPushPolicyTests(unittest.TestCase):
    def test_v1_blocked_when_push_not_requested(self) -> None:
        r = evaluate_live_push_policy(
            surface="v1_preview_tasks",
            manifest={"daszek_push_requested": False},
            action_plan_result=_ignore_safe_plan(),
            intake_result_final={},
        )
        self.assertFalse(r.allowed)
        self.assertEqual(r.push_policy_reason, "skipped_v1_not_requested")

    def test_v1_blocked_review_required(self) -> None:
        r = evaluate_live_push_policy(
            surface="v1_preview_tasks",
            manifest={"daszek_push_requested": True},
            action_plan_result=_ignore_safe_plan(),
            intake_result_final={"review_required": True},
        )
        self.assertFalse(r.allowed)
        self.assertEqual(r.push_policy_reason, "blocked_review_required")

    def test_v1_blocked_not_safe_for_live(self) -> None:
        r = evaluate_live_push_policy(
            surface="v1_preview_tasks",
            manifest={"daszek_push_requested": True},
            action_plan_result=_create_task_unsafe_plan(),
            intake_result_final={},
        )
        self.assertFalse(r.allowed)
        self.assertEqual(r.push_policy_reason, "blocked_not_safe_for_live_push")

    def test_v1_allowed_ignore_safe(self) -> None:
        r = evaluate_live_push_policy(
            surface="v1_preview_tasks",
            manifest={"daszek_push_requested": True},
            action_plan_result=_ignore_safe_plan(),
            intake_result_final={},
        )
        self.assertTrue(r.allowed)
        self.assertEqual(r.push_policy_reason, "allowed_safe_for_live_push")

    def test_v2_operator_projection_blocked_when_disabled_in_manifest(self) -> None:
        r = evaluate_operator_projection_policy(
            manifest={"daszek_v2_push_enabled": False},
            action_plan_result=_hold_operator_visible_plan(),
            intake_result_final={},
        )
        self.assertFalse(r.allowed)
        self.assertEqual(r.push_policy_reason, "skipped_v2_disabled")

    def test_v2_operator_projection_allows_non_ignore_when_enabled(self) -> None:
        r = evaluate_operator_projection_policy(
            manifest={"daszek_v2_push_enabled": True},
            action_plan_result=_create_task_unsafe_plan(),
            intake_result_final={},
        )
        self.assertTrue(r.allowed)
        self.assertEqual(r.push_policy_reason, "allowed_operator_projection")

    def test_v2_operator_projection_blocks_when_not_safe_for_operator_projection(self) -> None:
        r = evaluate_operator_projection_policy(
            manifest={"daszek_v2_push_enabled": True},
            action_plan_result=_ignore_safe_plan(),
            intake_result_final={},
        )
        self.assertFalse(r.allowed)
        self.assertEqual(r.push_policy_reason, "blocked_not_safe_for_operator_projection")

    def test_v2_operator_projection_blocked_when_policy_engine_rejected(self) -> None:
        r = evaluate_operator_projection_policy(
            manifest={"daszek_v2_push_enabled": True},
            action_plan_result=_hold_operator_visible_plan(),
            intake_result_final={},
            policy_report={"status": "REJECTED", "effective_risk_class": "high"},
        )
        self.assertFalse(r.allowed)
        self.assertEqual(r.push_policy_reason, "blocked_policy_engine_rejected")

    def test_v2_operator_projection_allows_needs_human(self) -> None:
        r = evaluate_operator_projection_policy(
            manifest={"daszek_v2_push_enabled": True},
            action_plan_result=_hold_operator_visible_plan(),
            intake_result_final={},
            policy_report={"status": "NEEDS_HUMAN", "effective_risk_class": "medium"},
        )
        self.assertTrue(r.allowed)
        self.assertEqual(r.push_policy_reason, "allowed_operator_projection")

    def test_v2_operator_projection_allows_approved(self) -> None:
        r = evaluate_operator_projection_policy(
            manifest={"daszek_v2_push_enabled": True},
            action_plan_result=_hold_operator_visible_plan(),
            intake_result_final={},
            policy_report={"status": "APPROVED", "effective_risk_class": "low"},
        )
        self.assertTrue(r.allowed)
        self.assertEqual(r.push_policy_reason, "allowed_operator_projection")

    def test_v2_operator_projection_blocks_unknown_policy_status(self) -> None:
        r = evaluate_operator_projection_policy(
            manifest={"daszek_v2_push_enabled": True},
            action_plan_result=_hold_operator_visible_plan(),
            intake_result_final={},
            policy_report={"status": "WEIRD", "effective_risk_class": "low"},
        )
        self.assertFalse(r.allowed)
        self.assertEqual(r.push_policy_reason, "blocked_policy_engine_status")

    def test_v2_operator_projection_relax_rejected_allows(self) -> None:
        r = evaluate_operator_projection_policy(
            manifest={
                "daszek_v2_push_enabled": True,
                "daszek_v2_desk_relax_rejected": True,
            },
            action_plan_result=_hold_operator_visible_plan(),
            intake_result_final={},
            policy_report={"status": "REJECTED", "effective_risk_class": "high"},
        )
        self.assertTrue(r.allowed)
        self.assertEqual(r.push_policy_reason, "allowed_operator_projection_desk_relax_rejected")

    def test_v2_operator_projection_include_ignore_allows(self) -> None:
        r = evaluate_operator_projection_policy(
            manifest={
                "daszek_v2_push_enabled": True,
                "daszek_v2_desk_include_ignore": True,
            },
            action_plan_result=_ignore_safe_plan(),
            intake_result_final={"decision": {"action": "ignore"}},
        )
        self.assertTrue(r.allowed)
        self.assertEqual(r.push_policy_reason, "allowed_operator_projection_desk_include_ignore")


if __name__ == "__main__":
    unittest.main()
