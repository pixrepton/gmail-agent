"""B1 regression corpus (CLAUDE.md B1 brief, section 9).

Eight follow-up scenarios on an EXISTING case (case_id already set), covering the
message-meaning categories the planner must be able to distinguish once it
receives the real current signal. None of them may be rewritten into a
fabricated SITE_VISIT/update_case_status decision, and a neutral/no-action
message must still be allowed to end honestly with report_gaps_and_stop
(the system may honestly propose nothing).

Not testing literal LLM wording — testing the deterministic runtime contract:
the real signal reaches the planner's snapshot, and whatever the planner
(stood in here by a fixed/sequenced test double) legitimately decides survives
unmodified through agent_runtime.graph.AgentGraphEngine.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution import load_constitution
from agent_runtime.graph import AgentGraphEngine
from agent_runtime.planner import MockSequencePlanner
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan
from agent_runtime.tools_registry import MockToolRegistry
from agent_runtime.turn_journal import InMemoryAgentTurnJournal


class _FixedPlanPlanner:
    def __init__(self, plan: ToolCallPlan) -> None:
        self._plan = plan

    def plan_next_tool(self, *, snapshot, available_tools, constitution):
        return self._plan


def _no_fabrication(rows) -> None:
    for row in rows:
        blob = str(row.get("tool_args_redacted") or "") + str(row.get("turn_summary_pl") or "")
        assert "SITE_VISIT" not in blob
        assert "klient odpowiedzial" not in blob.lower()


SEQUENCE_CASES = [
    {
        "id": "neutral_followup",
        "subject": "Re: Oferta na pompe ciepla",
        "snippet": "Dziekuje, sprawdze oferte i odezwe sie w przyszlym tygodniu.",
        "planner_tool": "report_gaps_and_stop",
    },
    {
        "id": "question_about_offer",
        "subject": "Pytanie do oferty",
        "snippet": "Czy w cenie oferty jest wliczony montaz jednostki zewnetrznej?",
        "planner_tool": "report_gaps_and_stop",
    },
    {
        "id": "acceptance_without_visit",
        "subject": "Akceptuje oferte",
        "snippet": "Akceptuje oferte, prosze o wystawienie faktury zaliczkowej.",
        "planner_tool": "extract_facts_from_text",
    },
    {
        "id": "complaint",
        "subject": "Reklamacja montazu",
        "snippet": "Zglaszam reklamacje - instalacja nie dziala poprawnie od tygodnia.",
        "planner_tool": "extract_facts_from_text",
    },
    {
        "id": "technical_failure",
        "subject": "Awaria pompy ciepla - pilne",
        "snippet": "Pompa ciepla nie grzeje od wczoraj, prosze o pilna interwencje serwisu.",
        "planner_tool": "extract_facts_from_text",
    },
    {
        "id": "document_attachment",
        "subject": "Faktura za materialy",
        "snippet": "W zalaczniku przesylam fakture za zakupione materialy.",
        "planner_tool": "report_gaps_and_stop",
    },
    {
        "id": "unrelated_followup",
        "subject": "Szkolenie BHP - zapisy",
        "snippet": "Przypominamy o terminie szkolenia BHP w przyszlym tygodniu.",
        "planner_tool": "report_gaps_and_stop",
    },
]


@pytest.mark.parametrize("case", SEQUENCE_CASES, ids=[c["id"] for c in SEQUENCE_CASES])
def test_regression_case_no_fabrication_and_real_grounding(case) -> None:
    constitution = load_constitution()
    journal = InMemoryAgentTurnJournal()
    engagement_id = f"eng_corpus_{case['id']}"
    case_id = f"case_corpus_{case['id']}"
    engine = AgentGraphEngine(
        planner=MockSequencePlanner([case["planner_tool"]]),
        constitution=constitution,
        tool_registry=MockToolRegistry(),
        turn_journal=journal,
    )
    snapshot = build_initial_snapshot(case_id=case_id, engagement_id=engagement_id, trace_id=f"sig_{case['id']}")
    ctx = ToolExecutionContext.from_snapshot(
        snapshot,
        signal_payload={
            "case_id": case_id,
            "subject": case["subject"],
            "snippet": case["snippet"],
        },
        constitution=constitution,
    )
    result = engine.run(snapshot, context=ctx)
    final = result.snapshot

    # Invariant: planner's real (test-double) choice is respected, not overwritten.
    assert result.turns[0].tool_name == case["planner_tool"]
    # Invariant: no unproven semantic fabrication anywhere in the durable turn history.
    _no_fabrication(journal.list_turns(engagement_id))
    # Invariant: the planner saw the real current signal (subject+snippet reached the
    # snapshot it reasoned from, via the same seam _compact_view surfaces as recent_steps).
    summaries = [item.summary_pl for item in final.agent_memory.reasoning_trace]
    assert any(case["subject"] in s for s in summaries), (case["id"], summaries)
    # Invariant: sufficient Case context (stable case_id) is present throughout.
    assert final.case_id == case_id


def test_explicit_visit_scheduling_is_recognized_and_preserved() -> None:
    """Case 4: a genuine visit-scheduling decision must still be recognizable and must
    survive unmodified — the fix must not swing to the opposite failure (blocking or
    rewriting a real scheduling decision) while removing the fabrication."""
    constitution = load_constitution()
    journal = InMemoryAgentTurnJournal()
    real_plan = ToolCallPlan(
        tool_name="propose_mutation",
        arguments={
            "operation": "schedule_visit",
            "target": "case_corpus_visit",
            "payload": {"date": "2026-07-21T10:00:00+02:00"},
            "reasoning_pl": "Klient potwierdzil termin wizji lokalnej we wtorek o 10:00.",
        },
    )
    engine = AgentGraphEngine(
        planner=_FixedPlanPlanner(real_plan),
        constitution=constitution,
        tool_registry=MockToolRegistry(),
        turn_journal=journal,
    )
    snapshot = build_initial_snapshot(
        case_id="case_corpus_visit", engagement_id="eng_corpus_visit", trace_id="sig_corpus_visit"
    )
    snapshot = snapshot.model_copy(
        update={"operational_status": snapshot.operational_status.model_copy(update={"steps_remaining": 1})}
    )
    ctx = ToolExecutionContext.from_snapshot(
        snapshot,
        signal_payload={
            "case_id": "case_corpus_visit",
            "subject": "Re: Oferta na pompe ciepla",
            "snippet": "Pasuje mi wtorek o 10:00, mozemy umowic wizje lokalna.",
        },
        constitution=constitution,
    )
    engine.run(snapshot, context=ctx)

    rows = journal.list_turns("eng_corpus_visit")
    assert rows[0]["tool_name"] == "propose_mutation"
    assert rows[0]["tool_args_redacted"]["operation"] == "schedule_visit"
    assert rows[0]["tool_args_redacted"]["reasoning_pl"] == real_plan.arguments["reasoning_pl"]
    _no_fabrication(rows)


if __name__ == "__main__":
    import unittest
    unittest.main()
