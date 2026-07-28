"""CLOSEOUT-01 Phase 3 — regression guard for the deterministic follow-up first-action
contract in OpenAIToolPlanner._build_messages. Asserts the ambiguous
"request_operator_clarification lub report_gaps_and_stop" OR was replaced by an ordered,
deterministic policy for follow-up (case_id set) mail cases. Does not call any LLM.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_runtime.constitution import load_constitution  # noqa: E402
from agent_runtime.openai_agent_client import OpenAIToolPlanner  # noqa: E402
from agent_runtime.settings import load_agent_runtime_settings  # noqa: E402
from agent_runtime.store import build_initial_snapshot  # noqa: E402

_FOLLOWUP_TOOLS = (
    "search_gmail_thread", "search_rag_knowledge", "extract_facts_from_text",
    "generate_draft_reply", "request_operator_clarification", "report_gaps_and_stop",
)


def _followup_system_message() -> str:
    planner = OpenAIToolPlanner(settings=load_agent_runtime_settings())
    constitution = load_constitution()
    snapshot = build_initial_snapshot(case_id="case_recovery_FU-TEST", engagement_id="eng_test", trace_id="trace_test")
    snapshot.case_kind = "zapytanie_klienta"  # a classified follow-up
    messages = planner._build_messages(snapshot=snapshot, constitution=constitution, available_tools=_FOLLOWUP_TOOLS)
    return messages[0]["content"]


def test_ambiguous_or_between_clarify_and_stop_is_removed():
    sysmsg = _followup_system_message()
    assert "request_operator_clarification lub report_gaps_and_stop" not in sysmsg


def test_followup_has_deterministic_ordered_first_action_policy():
    sysmsg = _followup_system_message()
    # ordered policy markers present
    assert "(1) generate_draft_reply" in sysmsg
    assert "(2) w przeciwnym razie request_operator_clarification" in sysmsg
    assert "(3) report_gaps_and_stop" in sysmsg


def test_followup_discourages_opening_with_research_without_gap():
    sysmsg = _followup_system_message()
    assert "Nie zaczynaj tury od narzędzi read/search" in sysmsg


# INTELLIGENCE-QUALITY-BASELINE-LIFT-01 — CTX-05 out-of-system-context operator policy.
# General (no case-id, no hardcoded phrase), asserts the prompt instructs
# request_operator_clarification as the first action for unverifiable out-of-system context.
def _new_lead_system_message() -> str:
    planner = OpenAIToolPlanner(settings=load_agent_runtime_settings())
    constitution = load_constitution()
    snapshot = build_initial_snapshot(case_id="", engagement_id="eng_test", trace_id="trace_test")
    snapshot.case_kind = "zapytanie_klienta"
    messages = planner._build_messages(
        snapshot=snapshot, constitution=constitution,
        available_tools=("search_gmail_thread", "search_rag_knowledge", "generate_draft_reply", "request_operator_clarification"),
    )
    return messages[0]["content"]


def test_out_of_system_context_policy_prefers_operator_clarification_first():
    sysmsg = _new_lead_system_message()
    assert "spoza tego systemu" in sysmsg
    assert "PIERWSZYM działaniem musi być request_operator_clarification" in sysmsg
    # must forbid opening with research for this class (context is not in the system)
    assert "NIE zaczynaj wtedy od search_*/read_*/list_*" in sysmsg
    # no hardcoded benchmark phrase / case id
    assert "CTX-05" not in sysmsg
    assert "telefon" not in sysmsg.lower().split("request_operator_clarification")[0][-400:]
