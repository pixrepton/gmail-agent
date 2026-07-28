"""Tool execution contract for agent graph (PR-B)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolCallPlan(BaseModel):
    """LLM (or mock planner) output: one tool invocation."""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    policy_decision_id: str = ""
    action_proposal_id: str = ""
    correlation_status: str = ""

    model_config = ConfigDict(extra="forbid")


class ToolResult(BaseModel):
    status: Literal["ok", "error", "budget_exceeded", "node_a_error"]
    snapshot_delta: dict[str, Any] = Field(default_factory=dict)
    turn_summary_pl: str = ""
    tokens_used: int = 0
    next_tool_hint: str | None = None

    model_config = ConfigDict(extra="forbid")
