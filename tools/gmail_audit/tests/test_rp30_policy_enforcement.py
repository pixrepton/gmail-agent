"""RP-30: semantic policy divergence enforcement in agent graph."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.graph import _policy_enforcement_block
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from llm_contracts.engagement_snapshot_v2 import (
    EngagementSnapshotV2,
    SemanticPolicyPlanConsistencyV1,
)


def _snapshot_with_consistency(status: str, reason_codes: list[str]) -> EngagementSnapshotV2:
    snap = EngagementSnapshotV2.model_validate(
        {
            "engagement_id": "eng_rp30",
            "case_id": "case_rp30",
            "trace_id": "trace_rp30",
            "version": 1,
            "operational_status": {"code": "enriching", "steps_remaining": 3},
            "semantic_policy_plan_consistency": {
                "status": status,
                "reason_codes": reason_codes,
                "policy_decision_id": "pol1",
                "action_proposal_id": "act1",
                "tool_name": "generate_draft_reply",
                "mapping_classification": "NO_SAFE_MAPPING_EXISTS",
            },
        }
    )
    return snap


def test_policy_block_on_actionable_tool_conflict() -> None:
    snap = _snapshot_with_consistency("conflicting", ["policy_blocks_actionable_tool"])
    plan = ToolCallPlan(tool_name="generate_draft_reply", arguments={})
    block = _policy_enforcement_block(snap, plan)
    assert isinstance(block, ToolResult)
    assert block.status == "error"
    assert block.snapshot_delta["hitl_gate"]["required"] is True


def test_policy_does_not_block_when_consistent() -> None:
    snap = _snapshot_with_consistency("not_evaluable", ["no_formal_action_intent_tool_mapping"])
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={})
    assert _policy_enforcement_block(snap, plan) is None
