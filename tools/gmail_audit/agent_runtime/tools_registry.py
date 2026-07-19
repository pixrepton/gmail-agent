
"""Tool registries: mock (PR-B) and production handlers (PR-C)."""

from __future__ import annotations

from log_config import get_logger
import time
from typing import Protocol

from agent_runtime.policy_guardrails import guard_tool_plan
from agent_runtime.tool_budgets import TOOL_BUDGET
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from agent_runtime.tools.handlers import HANDLERS

logger = get_logger(__name__)


class ToolRegistry(Protocol):
    def execute(self, plan: ToolCallPlan, *, context: ToolExecutionContext) -> ToolResult: ...


class AgentToolRegistry:
    """Production registry with per-tool budgets and handler dispatch."""

    def __init__(self, handlers: dict | None = None) -> None:
        self._handlers = handlers or HANDLERS

    def execute(self, plan: ToolCallPlan, *, context: ToolExecutionContext) -> ToolResult:
        blocked = guard_tool_plan(plan, constitution=context.constitution)
        if blocked is not None:
            return blocked
        name = str(plan.tool_name or "").strip()
        constitution_budget = getattr(getattr(context, "constitution", None), "tool_budget", None)
        limit = constitution_budget.get(name) if isinstance(constitution_budget, dict) else None
        if limit is None:
            limit = TOOL_BUDGET.get(name)
        if limit is not None:
            count = context.record_tool_use(name)
            if count > limit:
                return ToolResult(
                    status="budget_exceeded",
                    turn_summary_pl=f"Budżet narzędzia {name} wyczerpany ({limit}/run).",
                    next_tool_hint="request_operator_clarification",
                    snapshot_delta={
                        "operational_status": {"code": "pending_operator", "blocking": True},
                        "hitl_gate": {"required": True, "reason": f"tool_budget_exceeded:{name}"},
                        "agent_memory": {
                            "reasoning_trace": [{
                                "turn": count,
                                "summary_pl": f"Narzędzie {name} wyczerpało budżet — nie używaj go ponownie w tym runie.",
                            }],
                        },
                    },
                )
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(
                status="error",
                turn_summary_pl=f"Nieznane narzędzie: {name}",
            )
        # B3: logowanie czasu wykonania handlera
        _t0 = time.monotonic()
        result = handler(plan, context)
        _duration_ms = round((time.monotonic() - _t0) * 1000, 1)
        logger.info("TOOL_EXECUTED", extra={"x": {
            "tool": name, "status": result.status, "duration_ms": _duration_ms,
        }})
        return result


class MockToolRegistry:
    """PR-B deterministic mock — tests only."""

    def execute(
        self,
        plan: ToolCallPlan,
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        name = str(plan.tool_name or "").strip()
        handler = _MOCK_HANDLERS.get(name)
        if handler is None:
            return ToolResult(
                status="error",
                turn_summary_pl=f"Nieznane narzędzie: {name}",
            )
        return handler(plan)


def _mock_extract_facts(_plan: ToolCallPlan) -> ToolResult:
    # UWAGA: deterministyczny fixture WYŁĄCZNIE do testów ścieżki silnika (apply delta / pętla).
    # NIE reprezentuje jakości produkcyjnej ekstrakcji — realny handler (LLM) jest pokryty
    # testem integracyjnym tests/test_agent_extract_facts_llm.py.
    return ToolResult(
        status="ok",
        turn_summary_pl="Wyciągnięto metraż i miasto z treści zapytania.",
        snapshot_delta={
            "operational_status": {"code": "enriching"},
            "hvac_profile": {
                "heated_area_m2": 128,
                "location": {"city": "Radlin", "postal_code": "44-310"},
                "building_type": "single_family",
            },
        },
    )


def _mock_report_gaps(_plan: ToolCallPlan) -> ToolResult:
    return ToolResult(
        status="ok",
        turn_summary_pl="Zatrzymano — operator musi zatwierdzić dalsze kroki.",
        snapshot_delta={
            "operational_status": {"code": "pending_operator"},
            "hitl_gate": {"required": True, "reason": "agent_stopped"},
            "gaps": [
                {
                    "field": "thermal_demand_kw",
                    "severity": "blocking",
                    "ask_pl": "Podaj szacowaną stratę ciepła (OZC) budynku.",
                }
            ],
        },
    )


_MOCK_HANDLERS = {
    "extract_facts_from_text": _mock_extract_facts,
    "report_gaps_and_stop": _mock_report_gaps,
}
