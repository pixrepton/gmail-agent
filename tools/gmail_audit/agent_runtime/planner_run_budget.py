"""Explicit planner run budget contract (PLANNER-EXEC-FIDELITY-01).

Extends existing max_rounds / per-tool constitution budgets with a single
run-scoped contract that is computed before the loop, updated after turns,
and enforced deterministically without an LLM.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PlannerRunBudget:
    max_turns: int = 12
    max_total_tool_calls: int = 24
    max_research_calls: int = 5
    max_repeated_objective_calls: int = 1
    max_tokens: int = 0  # 0 = unlimited / not enforced yet
    max_tool_failures: int = 4
    deadline_seconds: int = 0  # 0 = no wall clock deadline

    turns_used: int = 0
    tool_calls_used: int = 0
    research_calls_used: int = 0
    tool_failures_used: int = 0
    tokens_used: int = 0
    exhausted_reason: str = ""

    research_tools: tuple[str, ...] = field(
        default_factory=lambda: ("search_rag_knowledge", "search_gmail_thread")
    )

    def remaining(self) -> dict[str, int]:
        return {
            "turns": max(0, self.max_turns - self.turns_used),
            "tool_calls": max(0, self.max_total_tool_calls - self.tool_calls_used),
            "research_calls": max(
                0, self.max_research_calls - self.research_calls_used
            ),
            "tool_failures": max(
                0, self.max_tool_failures - self.tool_failures_used
            ),
            "tokens": (
                max(0, self.max_tokens - self.tokens_used) if self.max_tokens else -1
            ),
        }

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["remaining"] = self.remaining()
        return data

    def check_before_turn(self) -> str | None:
        """Return exhausted reason code or None if another turn is allowed."""
        if self.exhausted_reason:
            return self.exhausted_reason
        if self.turns_used >= self.max_turns:
            self.exhausted_reason = "PLANNER_BUDGET_EXCEEDED:max_turns"
            return self.exhausted_reason
        if self.tool_calls_used >= self.max_total_tool_calls:
            self.exhausted_reason = "PLANNER_BUDGET_EXCEEDED:max_total_tool_calls"
            return self.exhausted_reason
        if self.tool_failures_used >= self.max_tool_failures:
            self.exhausted_reason = "PLANNER_BUDGET_EXCEEDED:max_tool_failures"
            return self.exhausted_reason
        if self.max_tokens and self.tokens_used >= self.max_tokens:
            self.exhausted_reason = "PLANNER_BUDGET_EXCEEDED:max_tokens"
            return self.exhausted_reason
        return None

    def record_turn(
        self,
        *,
        tool_name: str,
        status: str,
        tokens: int = 0,
    ) -> str | None:
        self.turns_used += 1
        self.tool_calls_used += 1
        self.tokens_used += max(0, int(tokens or 0))
        if tool_name in self.research_tools:
            self.research_calls_used += 1
            if self.research_calls_used > self.max_research_calls:
                self.exhausted_reason = "PLANNER_BUDGET_EXCEEDED:max_research_calls"
        if status not in {"ok"}:
            self.tool_failures_used += 1
        return self.check_before_turn()


def build_planner_run_budget(
    *,
    max_rounds: int,
    constitution_tool_budget: dict[str, int] | None = None,
) -> PlannerRunBudget:
    """Derive a run budget from existing settings + constitution limits."""
    rounds = max(1, int(max_rounds or 12))
    per_tool = constitution_tool_budget or {}
    research_cap = int(per_tool.get("search_rag_knowledge") or 5)
    # Total tool calls soft-cap: sum of per-tool caps bounded, else 2x rounds.
    if per_tool:
        total_cap = min(48, max(rounds * 2, sum(int(v) for v in per_tool.values())))
    else:
        total_cap = rounds * 2
    return PlannerRunBudget(
        max_turns=rounds,
        max_total_tool_calls=total_cap,
        max_research_calls=research_cap,
        max_repeated_objective_calls=1,
        max_tokens=0,
        max_tool_failures=max(2, min(8, rounds // 2 or 2)),
        deadline_seconds=0,
    )


__all__ = ["PlannerRunBudget", "build_planner_run_budget"]
