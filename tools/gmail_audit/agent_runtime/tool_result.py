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
    #: The canonical semantic identity this plan was built against (observed
    #: ``semantic_hash`` from the envelope offered to the planner). The
    #: reference monitor compares it with the current envelope's
    #: ``source_semantic_hash``; mismatch DENY (canonical_semantic_drift).
    semantic_hash: str = ""

    model_config = ConfigDict(extra="forbid")


class ToolResult(BaseModel):
    status: Literal["ok", "error", "budget_exceeded", "node_a_error"]
    snapshot_delta: dict[str, Any] = Field(default_factory=dict)
    turn_summary_pl: str = ""
    tokens_used: int = 0
    next_tool_hint: str | None = None
    # PLANNER-EXEC-FIDELITY-01: optional attribution (empty = unclassified legacy).
    failure_class: str = ""
    failure_owner: str = ""
    retryable: bool | None = None

    model_config = ConfigDict(extra="forbid")
