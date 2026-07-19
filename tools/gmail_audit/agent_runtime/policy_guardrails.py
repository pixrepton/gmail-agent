"""Policy guardrails — forbidden tools and allowlist enforcement (PR-C)."""

from __future__ import annotations

from agent_runtime.constitution import AgentConstitution
from agent_runtime.tool_result import ToolCallPlan, ToolResult

_FORBIDDEN_TOOL_NAMES = frozenset(
    {
        "send_email",
        "auto_send",
        "create_offerdto",
        "archive_gmail",
        "calendar_live_write",
    }
)


def filter_planner_allowlist(
    available_tools: tuple[str, ...],
    constitution: AgentConstitution,
) -> tuple[str, ...]:
    """Intersect LLM allowlist with constitution and global forbidden set."""
    allowed = set(constitution.tool_allowlist)
    return tuple(
        name
        for name in available_tools
        if name in allowed and name not in _FORBIDDEN_TOOL_NAMES
    )


def guard_tool_plan(
    plan: ToolCallPlan,
    *,
    constitution: AgentConstitution | None = None,
) -> ToolResult | None:
    """Return ToolResult when tool must not run; None when execution may proceed."""
    name = str(plan.tool_name or "").strip()
    if name in _FORBIDDEN_TOOL_NAMES:
        return ToolResult(
            status="error",
            turn_summary_pl=f"Narzędzie {name} jest zabronione (HITL).",
            snapshot_delta={
                "hitl_gate": {"required": True, "reason": f"forbidden_tool:{name}"},
            },
        )
    if constitution is not None and name not in constitution.tool_allowlist:
        return ToolResult(
            status="error",
            turn_summary_pl=f"Narzędzie {name} nie jest na allowliście konstytucji.",
        )
    return None
