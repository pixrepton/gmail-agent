"""Per-tool invocation budgets for one AgentRun (PR-C).

Tylko narzędzia w HANDLERS — stare, skonsolidowane narzędzia usunięte.
query_anything jest bez limitu (generyczne czytanie).
"""

from __future__ import annotations

from agent_runtime.constitution_chat import CHAT_AGENT_TOOL_BUDGET
from agent_runtime.constitution_mail import MAIL_AGENT_TOOL_BUDGET

TOOL_BUDGET: dict[str, int] = dict(MAIL_AGENT_TOOL_BUDGET)
for _tool, _limit in CHAT_AGENT_TOOL_BUDGET.items():
    TOOL_BUDGET.setdefault(_tool, _limit)
