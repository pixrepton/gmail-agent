"""B1: planner must decide from the real current signal, not a fabricated one.

Confirmed bug (as-built): agent_runtime.graph.AgentGraphEngine._run() used to
intercept ANY planner choice of extract_facts_from_text/report_gaps_and_stop on
a follow-up (case_id already set) and unconditionally replace it with a
hardcoded propose_mutation(operation=update_case_status, target="SITE_VISIT dla
{case_id}", reasoning_pl="Automatyczne przejscie do SITE_VISIT - klient
odpowiedzial na oferte.") — regardless of what the current signal actually said.

Root cause (deeper): even before that override, the planner never received the
real current-turn message content at all (EngagementSnapshotV2 carries no
current-signal field, and the ToolPlanner protocol only receives `snapshot`,
never `ctx.signal_payload`), so a follow-up turn's LLM planner had nothing
concrete to reason from.
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution import load_constitution
from agent_runtime.graph import AgentGraphEngine, _ground_current_signal
from agent_runtime.planner import MockSequencePlanner
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan
from agent_runtime.tools_registry import MockToolRegistry
from agent_runtime.turn_journal import InMemoryAgentTurnJournal


class _FixedPlanPlanner:
    """Test-only planner stub standing in for the LLM: always returns the same
    fixed ToolCallPlan, and records the snapshot it was shown so tests can
    assert on what the planner actually saw before deciding."""

    def __init__(self, plan: ToolCallPlan) -> None:
        self._plan = plan
        self.last_seen_snapshot = None

    def plan_next_tool(self, *, snapshot, available_tools, constitution):
        self.last_seen_snapshot = snapshot
        return self._plan


def _assert_no_fabrication(rows) -> None:
    for row in rows:
        blob = str(row.get("tool_args_redacted") or "") + str(row.get("turn_summary_pl") or "")
        assert "SITE_VISIT" not in blob, f"fabricated SITE_VISIT leaked into turn: {row}"
        assert "klient odpowiedzial" not in blob.lower(), f"fabricated reasoning leaked into turn: {row}"


def test_neutral_followup_is_not_rewritten_to_site_visit() -> None:
    """RED-A: a neutral, no-action follow-up must not become update_case_status/SITE_VISIT."""
    constitution = load_constitution()
    journal = InMemoryAgentTurnJournal()
    engine = AgentGraphEngine(
        planner=MockSequencePlanner(["report_gaps_and_stop"]),
        constitution=constitution,
        tool_registry=MockToolRegistry(),
        turn_journal=journal,
    )
    snapshot = build_initial_snapshot(case_id="case_neutral", engagement_id="eng_neutral", trace_id="sig_neutral")
    ctx = ToolExecutionContext.from_snapshot(
        snapshot,
        signal_payload={
            "case_id": "case_neutral",
            "subject": "Re: Oferta na pompe ciepla",
            "snippet": "Dziekuje, sprawdze oferte i odezwe sie w przyszlym tygodniu.",
        },
        constitution=constitution,
    )
    result = engine.run(snapshot, context=ctx)
    final = result.snapshot

    assert result.turns[0].tool_name == "report_gaps_and_stop"
    assert final.hitl_gate.reason == "agent_stopped"
    _assert_no_fabrication(journal.list_turns("eng_neutral"))


def test_other_signal_type_is_not_rewritten_to_site_visit() -> None:
    """RED-B: a complaint/technical-issue follow-up must not become SITE_VISIT either."""
    constitution = load_constitution()
    journal = InMemoryAgentTurnJournal()
    engine = AgentGraphEngine(
        planner=MockSequencePlanner(["extract_facts_from_text"]),
        constitution=constitution,
        tool_registry=MockToolRegistry(),
        turn_journal=journal,
    )
    snapshot = build_initial_snapshot(case_id="case_complaint", engagement_id="eng_complaint", trace_id="sig_complaint")
    ctx = ToolExecutionContext.from_snapshot(
        snapshot,
        signal_payload={
            "case_id": "case_complaint",
            "subject": "Awaria pompy ciepla - pilne",
            "snippet": "Pompa ciepla nie grzeje od wczoraj, prosze o pilna interwencje serwisu.",
        },
        constitution=constitution,
    )
    result = engine.run(snapshot, context=ctx)

    assert result.turns[0].tool_name == "extract_facts_from_text"
    _assert_no_fabrication(journal.list_turns("eng_complaint"))


def test_real_visit_intent_reaches_execution_unmodified() -> None:
    """RED-C: a genuine visit-scheduling decision by the planner must survive unmodified
    (not be replaced by a *different* fabricated decision either)."""
    constitution = load_constitution()
    journal = InMemoryAgentTurnJournal()
    real_plan = ToolCallPlan(
        tool_name="propose_mutation",
        arguments={
            "operation": "schedule_visit",
            "target": "case_visit_1",
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
    snapshot = build_initial_snapshot(case_id="case_visit_1", engagement_id="eng_visit_1", trace_id="sig_visit_1")
    snapshot = snapshot.model_copy(
        update={"operational_status": snapshot.operational_status.model_copy(update={"steps_remaining": 1})}
    )
    ctx = ToolExecutionContext.from_snapshot(
        snapshot,
        signal_payload={
            "case_id": "case_visit_1",
            "subject": "Re: Oferta na pompe ciepla",
            "snippet": "Pasuje mi wtorek o 10:00, mozemy umowic wizje lokalna.",
        },
        constitution=constitution,
    )
    engine.run(snapshot, context=ctx)

    rows = journal.list_turns("eng_visit_1")
    assert rows[0]["tool_name"] == "propose_mutation"
    assert rows[0]["tool_args_redacted"]["operation"] == "schedule_visit"
    assert rows[0]["tool_args_redacted"]["reasoning_pl"] == real_plan.arguments["reasoning_pl"]
    assert "SITE_VISIT" not in str(rows[0]["tool_args_redacted"])


def test_ground_current_signal_appends_reasoning_trace_entry() -> None:
    """Contract: the real current signal (and any already-computed case understanding)
    must be foldable into the existing reasoning_trace seam without a schema change."""
    snapshot = build_initial_snapshot(case_id="case_ground", engagement_id="eng_ground", trace_id="sig_ground")
    grounded = _ground_current_signal(
        snapshot,
        {
            "subject": "Re: Oferta",
            "snippet": "Dziekuje, sprawdze i wroce za tydzien.",
            "understanding_brief_pl": "Klient nie podjal jeszcze decyzji o ofercie.",
        },
    )
    summaries = [item.summary_pl for item in grounded.agent_memory.reasoning_trace]
    assert any("Oferta" in s and "Dziekuje" in s for s in summaries)
    assert any("Klient nie podjal" in s for s in summaries)


def test_planner_sees_grounding_before_first_tool_call() -> None:
    """Contract: the planner's FIRST call in a turn must already see the real current
    signal — not just stale case-level state — via the snapshot it is handed."""
    constitution = load_constitution()
    fixed = _FixedPlanPlanner(ToolCallPlan(tool_name="report_gaps_and_stop", arguments={}))
    engine = AgentGraphEngine(
        planner=fixed,
        constitution=constitution,
        tool_registry=MockToolRegistry(),
    )
    snapshot = build_initial_snapshot(case_id="case_visible", engagement_id="eng_visible", trace_id="sig_visible")
    ctx = ToolExecutionContext.from_snapshot(
        snapshot,
        signal_payload={
            "case_id": "case_visible",
            "subject": "Awaria",
            "snippet": "Piec nie dziala od rana.",
        },
        constitution=constitution,
    )
    engine.run(snapshot, context=ctx)

    assert fixed.last_seen_snapshot is not None
    summaries = [item.summary_pl for item in fixed.last_seen_snapshot.agent_memory.reasoning_trace]
    assert any("Awaria" in s and "Piec nie dziala" in s for s in summaries)


if __name__ == "__main__":
    import unittest
    unittest.main()
