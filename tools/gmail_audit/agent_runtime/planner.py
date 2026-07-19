"""Tool planner interface — logika planowania agenta.

ARCHITEKTURA PLANOWANIA (hybrydowa):
===============================

System stosuje hybrydowe podejście: LLM generuje intencję, kod waliduje i wykonuje.

1. WARSTWA DECYZYJNA (LLM, nie w tym pliku):
   - OpenAI/Anthropic planner (w `openai_agent_client.py`) otrzymuje snapshot sprawy
     i listę dostępnych narzędzi (allowlist z constitution).
   - LLM wybiera: które narzędzie wywołać i z jakimi argumentami.
   - LLM decyduje o intencji — kod nie ma `classify_intent()` z if/elif.

2. WARSTWA WALIDACYJNA (ten plik + graph.py):
   - `ToolPlanner` to interfejs (Protocol) — każdy planner musi zwrócić `ToolCallPlan`.
   - `graph.py` przyjmuje plan, sprawdza autoryzację (authz), wykonuje narzędzie,
     zapisuje do turn journal, sprawdza HITL gate.
   - `graph.py` nakłada follow-up guard: gdy case_id istnieje, blokuje
     extract_facts_from_text i report_gaps_and_stop — wymusza propose_mutation.

3. PLANOWANIE DETERMINISTYCZNE (testy):
   - `MockSequencePlanner` — deterministyczna kolejka narzędzi dla testów.
   - `HeuristicMockPlanner` — prosta ścieżka: extract facts → stop.
   - Te nie używają LLM — są do testów jednostkowych.

4. PRZEPŁYW:
   graph.py.run() → planner.plan_next_tool() → graph._execute_tool() →
   handlers.HANDLERS[tool_name]() → ToolResult → graph loop

   Pętla kończy się gdy:
   - steps_remaining == 0 (budżet wyczerpany) → pending_operator
   - HITL gate wymaga zatwierdzenia
   - status terminalny (node_a_error, pending_operator)

5. BUDŻETY:
   Każde narzędzie ma limit wywołań zdefiniowany w constitution_mail.py /
   constitution_chat.py (TOOL_BUDGET). Gdy limit osiągnięty, narzędzie zwraca
   status="budget_exceeded". Nie ma globalnego timeoutu na turę — timeout
   jest implementowany w graph.py jako AGENT_TURN_TIMEOUT_SECONDS.
"""

from __future__ import annotations

from typing import Protocol

from agent_runtime.constitution import AgentConstitution
from agent_runtime.tool_result import ToolCallPlan
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2


class ToolPlanner(Protocol):
    def plan_next_tool(
        self,
        *,
        snapshot: EngagementSnapshotV2,
        available_tools: tuple[str, ...],
        constitution: AgentConstitution,
    ) -> ToolCallPlan: ...


class MockSequencePlanner:
    """Deterministic planner for tests: pops tool names from a queue."""

    def __init__(self, sequence: list[str]) -> None:
        self._sequence = list(sequence)
        self._index = 0

    def plan_next_tool(
        self,
        *,
        snapshot: EngagementSnapshotV2,
        available_tools: tuple[str, ...],
        constitution: AgentConstitution,
    ) -> ToolCallPlan:
        if self._index >= len(self._sequence):
            return ToolCallPlan(tool_name="report_gaps_and_stop", arguments={})
        name = self._sequence[self._index]
        self._index += 1
        if name not in available_tools:
            raise ValueError(f"tool {name!r} not in allowlist")
        return ToolCallPlan(tool_name=name, arguments={})


class HeuristicMockPlanner:
    """Single-path planner: extract facts if no m2, else stop."""

    def plan_next_tool(
        self,
        *,
        snapshot: EngagementSnapshotV2,
        available_tools: tuple[str, ...],
        constitution: AgentConstitution,
    ) -> ToolCallPlan:
        if snapshot.hvac_profile.heated_area_m2 is None:
            return ToolCallPlan(tool_name="extract_facts_from_text", arguments={})
        return ToolCallPlan(tool_name="report_gaps_and_stop", arguments={})
