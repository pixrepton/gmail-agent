"""RC-12 / RP-22: sub-agent scope reachability and terminal-tool retention.

Two defects are covered here:

1. The pre-plan call site in `AgentGraph._run` used to call
   `select_sub_agent(tool_name="", ...)`. An empty tool name can never match
   `TOOL_SCOPE_MAP`, so the `document` and `draft` scopes were structurally
   unreachable there while the signature still claimed all four were possible.
   Pre-plan selection is now its own function with an honest narrower return type.

2. `tools_for_sub_agent` dropped the terminal/escalation tools from every
   narrowed scope. With `materialize_proposals` set, the mail agent was offered
   only `check_cp2025_eligibility` and `search_rag_knowledge` — no way to stop or
   escalate — so the turn loop could only burn budget until the per-turn cap.
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution_mail import MAIL_AGENT_TOOL_ALLOWLIST
from agent_runtime.sub_agents import (
    TOOL_SCOPE_MAP,
    select_preplan_sub_agent,
    select_sub_agent,
    sub_agent_scopes,
    tools_for_sub_agent,
)
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2

_TERMINAL_TOOLS = ("request_operator_clarification", "report_gaps_and_stop")


def _snapshot(*, materialize: bool = False) -> EngagementSnapshotV2:
    proposals = (
        [{"proposal_id": "prop_1", "proposal_type": "create_case", "payload_json": {}}]
        if materialize
        else []
    )
    return EngagementSnapshotV2.model_validate(
        {
            "engagement_id": "eng_scope",
            "case_id": "case_scope",
            "version": 1,
            "trace_id": "sig_scope",
            "operational_status": {"code": "enriching", "steps_remaining": 8},
            "hvac_profile": {"location": {}},
            "gaps": [],
            "agent_memory": {
                "reasoning_trace": [],
                "tool_calls": [],
                "constitution_sections_used": [],
                "materialize_proposals": proposals,
            },
            "actions": [],
            "hitl_gate": {"required": False, "reason": ""},
        }
    )


def test_preplan_selection_only_returns_scopes_it_can_actually_resolve() -> None:
    # Pre-plan has no tool name, so the tool-driven scopes must not be claimed.
    assert select_preplan_sub_agent(snapshot=_snapshot()) == "general"
    assert select_preplan_sub_agent(snapshot=_snapshot(materialize=True)) == "policy"


def test_document_and_draft_scopes_are_reachable_after_the_plan() -> None:
    # The post-plan site passes the real tool name; all four scopes resolve there.
    reached = {
        select_sub_agent(tool_name=name, snapshot=_snapshot())
        for name in TOOL_SCOPE_MAP
    }
    assert {"document", "policy", "draft"} <= reached
    assert select_sub_agent(tool_name="search_gmail_thread", snapshot=_snapshot()) == "general"


def test_every_narrowed_scope_keeps_a_way_to_stop_or_escalate() -> None:
    pool = frozenset(MAIL_AGENT_TOOL_ALLOWLIST)
    for kind in ("document", "policy", "draft"):
        offered = tools_for_sub_agent(kind, pool)
        assert offered, f"{kind} scope offered no tools at all"
        for terminal in _TERMINAL_TOOLS:
            assert terminal in offered, f"{kind} scope cannot reach {terminal}"


def test_materialize_scope_is_not_starved_of_terminal_tools() -> None:
    # Regression for the live path: materialize_proposals -> policy -> offered set.
    kind = select_preplan_sub_agent(snapshot=_snapshot(materialize=True))
    offered = tools_for_sub_agent(kind, frozenset(MAIL_AGENT_TOOL_ALLOWLIST))
    assert "report_gaps_and_stop" in offered
    assert "check_cp2025_eligibility" in offered


def test_general_scope_still_offers_the_whole_allowlist() -> None:
    pool = frozenset(MAIL_AGENT_TOOL_ALLOWLIST)
    assert tools_for_sub_agent("general", pool) == sorted(pool)


def test_registry_covers_all_tool_driven_scopes() -> None:
    scopes = set(sub_agent_scopes().values())
    assert scopes == {"document", "policy", "draft"}
