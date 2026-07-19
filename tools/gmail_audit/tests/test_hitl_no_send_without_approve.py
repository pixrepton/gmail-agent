from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.policy_guardrails import guard_tool_plan
from agent_runtime.tool_result import ToolCallPlan
from agent_runtime.store import build_initial_snapshot
from agent_runtime.snapshot_delta import apply_snapshot_delta


def test_send_email_blocked_by_constitution() -> None:
    from agent_runtime.constitution import load_constitution

    constitution = load_constitution()
    plan = ToolCallPlan(tool_name="send_email", arguments={})
    blocked = guard_tool_plan(plan, constitution=constitution)
    assert blocked is not None
    assert blocked.status == "error"


def test_hitl_gate_blocks_execution_path() -> None:
    snap = build_initial_snapshot(case_id="c", engagement_id="e", trace_id="t")
    snap = apply_snapshot_delta(
        snap,
        {
            "hitl_gate": {"required": True, "reason": "draft_ready_for_approval"},
            "actions": [{"id": "draft_reply", "enabled": True, "payload_pl": "x"}],
        },
    )
    assert snap.hitl_gate.required is True
    assert snap.actions[0].enabled is True
