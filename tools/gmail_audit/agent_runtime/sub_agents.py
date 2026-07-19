"""Specialist sub-agent routing (MAX-STACK W5 handoff labels).

Registry-based: dodanie nowego narzędzia = rejestracja w TOOL_SCOPE_MAP.
Żadne if/elif na tool_name. Generic Hands compliant.
"""

from __future__ import annotations

from typing import Any, Literal

from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2

SubAgentKind = Literal["document", "policy", "draft", "general"]

# ── Registry: tool_name → sub-agent scope ────────────────────────────────
# Dodanie nowego narzędzia = wpis w tym słowniku. Zero if/elif.
TOOL_SCOPE_MAP: dict[str, SubAgentKind] = {
    # Document tools
    "read_google_drive_file": "document",
    "list_drive_folder": "document",
    "retry_hard_parse": "document",
    # Policy tools
    "check_cp2025_eligibility": "policy",
    "search_rag_knowledge": "policy",
    "query_anything": "policy",
    # Draft tools
    "generate_draft_reply": "draft",
}

# Tools dostępne w każdym scope (oprócz general, który ma wszystko)
_POLICY_EXTRA_TOOLS = frozenset({
    "propose_case_link", "propose_artifact",
    "propose_plan", "propose_mutation",
})
_DRAFT_EXTRA_TOOLS = frozenset({"request_operator_clarification", "propose_plan", "propose_mutation"})


def select_sub_agent(*, tool_name: str, snapshot: EngagementSnapshotV2) -> SubAgentKind:
    name = str(tool_name or "").strip()
    # Registry lookup — zero if/elif
    scope = TOOL_SCOPE_MAP.get(name)
    if scope is not None:
        return scope
    # Materialize proposals → policy context
    if snapshot.agent_memory.materialize_proposals:
        return "policy"
    return "general"


def tools_for_sub_agent(kind: SubAgentKind, allowlist: frozenset[str] | set[str]) -> list[str]:
    """Restrict planner allowlist per specialist handoff (W5).

    Używa TOOL_SCOPE_MAP zamiast hardcoded frozenset — Generic Hands compliant.
    """
    pool = frozenset(allowlist)
    if kind == "document":
        return sorted(pool & frozenset(
            name for name, scope in TOOL_SCOPE_MAP.items() if scope == "document"
        ))
    if kind == "policy":
        policy_tools = frozenset(
            name for name, scope in TOOL_SCOPE_MAP.items() if scope == "policy"
        )
        return sorted(pool & (policy_tools | _POLICY_EXTRA_TOOLS))
    if kind == "draft":
        draft_tools = frozenset(
            name for name, scope in TOOL_SCOPE_MAP.items() if scope == "draft"
        )
        return sorted(pool & (draft_tools | _DRAFT_EXTRA_TOOLS))
    return sorted(pool)


def sub_agent_handoff_note(kind: SubAgentKind, tool_name: str) -> str:
    return f"handoff:{kind}:{tool_name}"


def register_tool_scope(tool_name: str, kind: SubAgentKind) -> None:
    """Dynamiczna rejestracja scope'u dla narzędzia — Generic Hands."""
    TOOL_SCOPE_MAP[tool_name] = kind


def sub_agent_scopes() -> dict[str, SubAgentKind]:
    """Zwraca kopię aktualnego rejestru scope'ów."""
    return dict(TOOL_SCOPE_MAP)


__all__ = [
    "SubAgentKind",
    "select_sub_agent",
    "sub_agent_handoff_note",
    "tools_for_sub_agent",
    "register_tool_scope",
    "sub_agent_scopes",
]
