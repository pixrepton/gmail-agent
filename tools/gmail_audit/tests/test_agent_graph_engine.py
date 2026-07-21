from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution import load_constitution
from agent_runtime.graph import AgentGraphEngine
from agent_runtime.openai_agent_client import OpenAIAgentPlannerError, _compact_view
from agent_runtime.planner import HeuristicMockPlanner, MockSequencePlanner
from agent_runtime.store import InMemoryOperatorEngagementStore, build_initial_snapshot
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from agent_runtime.tools_registry import AgentToolRegistry, MockToolRegistry


class _RaisingPlanner:
    """A planner whose plan_next_tool always raises — simulates a ghost-tool 400,
    a network error, or any other real-world planner-call failure not caused by
    schema mismatch specifically."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def plan_next_tool(self, *, snapshot, available_tools, constitution):  # noqa: D401
        raise self._exc


class _ReadOncePlanner:
    def __init__(self) -> None:
        self.available_by_turn: list[tuple[str, ...]] = []

    def plan_next_tool(self, *, snapshot, available_tools, constitution):  # noqa: D401
        offered = tuple(available_tools)
        self.available_by_turn.append(offered)
        if "search_gmail_thread" in offered:
            return ToolCallPlan(tool_name="search_gmail_thread", arguments={})
        return ToolCallPlan(tool_name="report_gaps_and_stop", arguments={})


class _TwoRagQueriesPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan_next_tool(self, *, snapshot, available_tools, constitution):  # noqa: D401
        self.calls += 1
        if self.calls == 1:
            return ToolCallPlan(
                tool_name="search_rag_knowledge",
                arguments={"query": "warunki dotacji"},
            )
        if self.calls == 2:
            return ToolCallPlan(
                tool_name="search_rag_knowledge",
                arguments={"query": "polityka gwarancyjna"},
            )
        return ToolCallPlan(tool_name="report_gaps_and_stop", arguments={})


class _IgnoresAvailableToolsPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan_next_tool(self, *, snapshot, available_tools, constitution):  # noqa: D401
        self.calls += 1
        return ToolCallPlan(tool_name="search_gmail_thread", arguments={"thread_id": snapshot.case_id})


class _CaptureResearchStatePlanner:
    def __init__(self) -> None:
        self.calls = 0
        self.compact_by_turn: list[dict] = []

    def plan_next_tool(self, *, snapshot, available_tools, constitution):  # noqa: D401
        self.calls += 1
        self.compact_by_turn.append(_compact_view(snapshot))
        if self.calls == 1:
            return ToolCallPlan(
                tool_name="search_rag_knowledge",
                arguments={"query": "case_recovery_DEC-02 oferta cena negocjacja"},
            )
        if self.calls == 2:
            return ToolCallPlan(
                tool_name="search_rag_knowledge",
                arguments={"query": "case_recovery_DEC-02 szczegoly oferty klient"},
            )
        return ToolCallPlan(tool_name="request_operator_clarification", arguments={"ask_pl": "decyzja"})


class _DuplicateRagObjectivePlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan_next_tool(self, *, snapshot, available_tools, constitution):  # noqa: D401
        self.calls += 1
        if self.calls == 1:
            return ToolCallPlan(
                tool_name="search_rag_knowledge",
                arguments={"query": "case_recovery_DEC-02 oferta rabat negocjacja ceny"},
            )
        return ToolCallPlan(
            tool_name="search_rag_knowledge",
            arguments={"query": "case_recovery_DEC-02 oferta cena konkurencji rabat"},
        )


class _DuplicateMi02OldHeaterPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan_next_tool(self, *, snapshot, available_tools, constitution):  # noqa: D401
        self.calls += 1
        if self.calls == 1:
            return ToolCallPlan(
                tool_name="search_rag_knowledge",
                arguments={"query": "oferta wywoz starego pieca wliczony w cene warunki oferty"},
            )
        return ToolCallPlan(
            tool_name="search_rag_knowledge",
            arguments={"query": "case_recovery_MI-02 oferta warunki cena wywoz pieca stary koszt"},
        )


class _DuplicateInt06ContextPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan_next_tool(self, *, snapshot, available_tools, constitution):  # noqa: D401
        self.calls += 1
        if self.calls == 1:
            return ToolCallPlan(
                tool_name="search_rag_knowledge",
                arguments={"query": "case_recovery_INT-06 sprawa kontekst"},
            )
        return ToolCallPlan(
            tool_name="search_rag_knowledge",
            arguments={"query": "case_recovery_INT-06 kontekst sprawy klient"},
        )


class _DuplicateInt06MessageContentPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan_next_tool(self, *, snapshot, available_tools, constitution):  # noqa: D401
        self.calls += 1
        if self.calls == 1:
            return ToolCallPlan(
                tool_name="search_rag_knowledge",
                arguments={"query": "case_recovery_INT-06 tresc wiadomosci klienta"},
            )
        return ToolCallPlan(
            tool_name="search_rag_knowledge",
            arguments={"query": "case_recovery_INT-06 oryginalna wiadomosc klienta tresc"},
        )


def test_load_constitution_has_allowlist() -> None:
    constitution = load_constitution()
    assert "extract_facts_from_text" in constitution.tool_allowlist
    assert "send_email" in constitution.forbidden_actions
    assert constitution.section_headers()


def test_graph_extract_then_stop() -> None:
    constitution = load_constitution()
    engine = AgentGraphEngine(
        planner=MockSequencePlanner(
            ["extract_facts_from_text", "report_gaps_and_stop"],
        ),
        constitution=constitution,
        tool_registry=MockToolRegistry(),
    )
    snapshot = build_initial_snapshot(
        case_id="case_b1",
        engagement_id="eng_b1",
        trace_id="sig_b1",
    )
    result = engine.run(snapshot)
    final = result.snapshot
    assert final.hvac_profile.heated_area_m2 == 128
    assert final.hvac_profile.location.city == "Radlin"
    assert final.operational_status.code == "pending_operator"
    assert final.hitl_gate.required is True
    assert len(result.turns) == 2
    assert result.turns[0].tool_name == "extract_facts_from_text"


def test_planner_exception_converges_to_safe_escalation_not_silent_death() -> None:
    """DELIVERY-1 failure convergence (brief §9): an uncaught planner exception — a
    ghost-tool 400, a network error, any bug — must not silently unwind the whole
    run with zero recorded turn and zero hitl_gate change. It must converge to the
    SAME safe, escalated terminal state report_gaps_and_stop already produces,
    using the run's own existing safe-stop mechanism rather than a new subsystem.
    This is independent of RC-2's specific missing-schema cause: it must hold for
    ANY planner exception, not just the one EVAL-1 happened to observe.
    """
    constitution = load_constitution()
    engine = AgentGraphEngine(
        planner=_RaisingPlanner(OpenAIAgentPlannerError("simulated ghost-tool 400")),
        constitution=constitution,
        tool_registry=MockToolRegistry(),
    )
    snapshot = build_initial_snapshot(
        case_id="case_fc1",
        engagement_id="eng_fc1",
        trace_id="sig_fc1",
    )

    result = engine.run(snapshot)  # must not raise

    final = result.snapshot
    assert final.hitl_gate.required is True
    assert final.operational_status.code == "pending_operator"
    assert len(result.turns) == 1
    assert result.turns[0].tool_status == "error"


def test_heuristic_planner_single_extract() -> None:
    constitution = load_constitution()
    engine = AgentGraphEngine(
        planner=HeuristicMockPlanner(),
        constitution=constitution,
        tool_registry=MockToolRegistry(),
    )
    snapshot = build_initial_snapshot(
        case_id="case_b2",
        engagement_id="eng_b2",
        trace_id="sig_b2",
    )
    result = engine.run(snapshot)
    assert result.snapshot.hvac_profile.heated_area_m2 == 128
    assert result.snapshot.operational_status.code == "pending_operator"


def test_store_init_then_graph_round_trip() -> None:
    store = InMemoryOperatorEngagementStore()
    snapshot = store.init_snapshot_from_signal(
        signal={"signal_id": "sig_store_graph"},
        case_id="case_sg",
        engagement_id="eng_sg",
    )
    constitution = load_constitution()
    engine = AgentGraphEngine(
        planner=MockSequencePlanner(["extract_facts_from_text", "report_gaps_and_stop"]),
        constitution=constitution,
        tool_registry=MockToolRegistry(),
    )
    run_result = engine.run(snapshot)
    new_version = store.save_snapshot(run_result.snapshot, expected_version=1)
    assert new_version == 2
    loaded = store.load_snapshot("eng_sg")
    assert loaded is not None
    assert loaded.hvac_profile.heated_area_m2 == 128
    assert loaded.operational_status.code == "pending_operator"


def test_budget_exhaustion_sets_hitl() -> None:
    from agent_runtime.store import build_initial_snapshot

    constitution = load_constitution()
    engine = AgentGraphEngine(
        planner=MockSequencePlanner(["search_gmail_thread"] * 20),
        constitution=constitution,
        tool_registry=MockToolRegistry(),
    )
    snapshot = build_initial_snapshot(
        case_id="case_budget",
        engagement_id="eng_budget",
        trace_id="sig_budget",
    )
    snapshot = snapshot.model_copy(
        update={
            "operational_status": snapshot.operational_status.model_copy(
                update={"steps_remaining": 1}
            )
        }
    )
    result = engine.run(snapshot)
    final = result.snapshot
    assert final.hitl_gate.required is True
    assert final.hitl_gate.reason == "budget_exhausted"
    assert final.operational_status.code == "pending_operator"


def test_graph_stops_on_constitution_per_tool_budget() -> None:
    constitution = load_constitution()
    engine = AgentGraphEngine(
        planner=MockSequencePlanner(["search_gmail_thread"] * 20),
        constitution=constitution,
        tool_registry=AgentToolRegistry(
            handlers={
                "search_gmail_thread": lambda _plan, _ctx: ToolResult(
                    status="error",
                    turn_summary_pl="backend unavailable",
                )
            }
        ),
    )
    snapshot = build_initial_snapshot(
        case_id="case_tool_budget",
        engagement_id="eng_tool_budget",
        trace_id="sig_tool_budget",
    )

    result = engine.run(snapshot)

    final = result.snapshot
    assert len(result.turns) == 4
    assert result.turns[-1].tool_status == "budget_exceeded"
    assert final.hitl_gate.required is True
    assert final.hitl_gate.reason == "tool_budget_exceeded:search_gmail_thread"
    assert final.operational_status.code == "pending_operator"


def test_completed_search_gmail_thread_is_not_offered_again() -> None:
    constitution = load_constitution()
    planner = _ReadOncePlanner()
    engine = AgentGraphEngine(
        planner=planner,
        constitution=constitution,
        tool_registry=AgentToolRegistry(
            handlers={
                "search_gmail_thread": lambda _plan, _ctx: ToolResult(
                    status="ok",
                    turn_summary_pl="gmail read ok",
                    snapshot_delta={
                        "agent_memory": {
                            "reasoning_trace": [{"turn": 0, "summary_pl": "gmail fixture"}],
                        },
                    },
                ),
                "report_gaps_and_stop": lambda _plan, _ctx: ToolResult(
                    status="ok",
                    turn_summary_pl="stop",
                    snapshot_delta={
                        "operational_status": {"code": "pending_operator", "blocking": True},
                        "hitl_gate": {"required": True, "reason": "agent_stopped"},
                    },
                ),
            }
        ),
    )
    snapshot = build_initial_snapshot(
        case_id="case_read_once",
        engagement_id="eng_read_once",
        trace_id="sig_read_once",
    )

    result = engine.run(snapshot)

    assert result.turns[0].tool_name == "search_gmail_thread"
    assert result.turns[1].tool_name == "report_gaps_and_stop"
    assert "search_gmail_thread" in planner.available_by_turn[0]
    assert "search_gmail_thread" not in planner.available_by_turn[1]


def test_successful_distinct_rag_queries_remain_allowed() -> None:
    constitution = load_constitution()
    planner = _TwoRagQueriesPlanner()
    executed_queries: list[str] = []
    engine = AgentGraphEngine(
        planner=planner,
        constitution=constitution,
        tool_registry=AgentToolRegistry(
            handlers={
                "search_rag_knowledge": lambda plan, _ctx: (
                    executed_queries.append(str(plan.arguments["query"]))
                    or ToolResult(
                        status="ok",
                        turn_summary_pl="rag read ok",
                        snapshot_delta={
                            "agent_memory": {
                                "reasoning_trace": [
                                    {"turn": 0, "summary_pl": f"rag {plan.arguments['query']}"},
                                ],
                            },
                        },
                    )
                ),
                "report_gaps_and_stop": lambda _plan, _ctx: ToolResult(
                    status="ok",
                    turn_summary_pl="stop",
                    snapshot_delta={
                        "operational_status": {"code": "pending_operator", "blocking": True},
                        "hitl_gate": {"required": True, "reason": "agent_stopped"},
                    },
                ),
            }
        ),
    )
    snapshot = build_initial_snapshot(
        case_id="case_rag_multi",
        engagement_id="eng_rag_multi",
        trace_id="sig_rag_multi",
    )

    result = engine.run(snapshot)

    assert [turn.tool_name for turn in result.turns[:2]] == [
        "search_rag_knowledge",
        "search_rag_knowledge",
    ]
    assert executed_queries == ["warunki dotacji", "polityka gwarancyjna"]


def test_successful_rag_research_accumulates_for_next_planner_turn() -> None:
    constitution = load_constitution()
    planner = _CaptureResearchStatePlanner()

    def _rag(plan, _ctx):
        query = str(plan.arguments["query"])
        return ToolResult(
            status="ok",
            turn_summary_pl="RAG: 1 fragmentow dla zapytania.",
            snapshot_delta={
                "agent_memory": {
                    "reasoning_trace": [
                        {
                            "turn": 0,
                            "summary_pl": (
                                f"RAG query={query}; hits=1; "
                                f"evidence=chunk-{len(query)}; top=fixture result"
                            ),
                        }
                    ],
                },
            },
        )

    engine = AgentGraphEngine(
        planner=planner,
        constitution=constitution,
        tool_registry=AgentToolRegistry(
            handlers={
                "search_rag_knowledge": _rag,
                "request_operator_clarification": lambda _plan, _ctx: ToolResult(
                    status="ok",
                    turn_summary_pl="stop",
                    snapshot_delta={
                        "operational_status": {"code": "pending_operator", "blocking": True},
                        "hitl_gate": {"required": True, "reason": "agent_stopped"},
                    },
                ),
            }
        ),
    )
    snapshot = build_initial_snapshot(
        case_id="case_research_stop",
        engagement_id="eng_research_stop",
        trace_id="sig_research_stop",
    )

    engine.run(snapshot)

    third_turn_view = planner.compact_by_turn[2]
    completed = "\n".join(third_turn_view.get("completed_rag_research", []))
    assert "case_recovery_DEC-02 oferta cena negocjacja" in completed
    assert "case_recovery_DEC-02 szczegoly oferty klient" in completed


def test_semantic_duplicate_rag_objective_is_blocked_after_success() -> None:
    constitution = load_constitution()
    planner = _DuplicateRagObjectivePlanner()
    executed_queries: list[str] = []

    def _rag(plan, _ctx):
        query = str(plan.arguments["query"])
        executed_queries.append(query)
        return ToolResult(
            status="ok",
            turn_summary_pl="RAG: 1 fragmentow dla zapytania.",
            snapshot_delta={
                "agent_memory": {
                    "reasoning_trace": [
                        {
                            "turn": 0,
                            "summary_pl": (
                                f"RAG query={query}; hits=1; "
                                "evidence=case_recovery_DEC-02_chunk_0; top=rabat fixture"
                            ),
                        }
                    ],
                },
            },
        )

    engine = AgentGraphEngine(
        planner=planner,
        constitution=constitution,
        tool_registry=AgentToolRegistry(handlers={"search_rag_knowledge": _rag}),
    )
    snapshot = build_initial_snapshot(
        case_id="case_duplicate_rag",
        engagement_id="eng_duplicate_rag",
        trace_id="sig_duplicate_rag",
    )

    result = engine.run(snapshot)

    assert executed_queries == ["case_recovery_DEC-02 oferta rabat negocjacja ceny"]
    assert [turn.tool_name for turn in result.turns] == [
        "search_rag_knowledge",
        "search_rag_knowledge",
    ]
    assert result.turns[1].tool_status == "error"
    assert result.snapshot.hitl_gate.reason == "duplicate_rag_research:price_negotiation"


def test_mi02_old_heater_removal_research_objective_is_blocked_after_success() -> None:
    constitution = load_constitution()
    planner = _DuplicateMi02OldHeaterPlanner()
    executed_queries: list[str] = []

    def _rag(plan, _ctx):
        query = str(plan.arguments["query"])
        executed_queries.append(query)
        return ToolResult(
            status="ok",
            turn_summary_pl="RAG: 1 fragmentow dla zapytania.",
            snapshot_delta={
                "agent_memory": {
                    "reasoning_trace": [
                        {
                            "turn": 0,
                            "summary_pl": (
                                f"RAG query={query}; hits=1; "
                                "evidence=case_recovery_MI-02_chunk_0; top=wywoz pieca fixture"
                            ),
                        }
                    ],
                },
            },
        )

    engine = AgentGraphEngine(
        planner=planner,
        constitution=constitution,
        tool_registry=AgentToolRegistry(handlers={"search_rag_knowledge": _rag}),
    )
    snapshot = build_initial_snapshot(
        case_id="case_duplicate_mi02",
        engagement_id="eng_duplicate_mi02",
        trace_id="sig_duplicate_mi02",
    )

    result = engine.run(snapshot)

    assert executed_queries == ["oferta wywoz starego pieca wliczony w cene warunki oferty"]
    assert result.turns[1].tool_status == "error"
    assert result.snapshot.hitl_gate.reason == "duplicate_rag_research:old_heater_removal_scope"


def test_int06_generic_context_research_objective_is_blocked_after_success() -> None:
    constitution = load_constitution()
    planner = _DuplicateInt06ContextPlanner()
    executed_queries: list[str] = []

    def _rag(plan, _ctx):
        query = str(plan.arguments["query"])
        executed_queries.append(query)
        return ToolResult(
            status="ok",
            turn_summary_pl="RAG: 1 fragmentow dla zapytania.",
            snapshot_delta={
                "agent_memory": {
                    "reasoning_trace": [
                        {
                            "turn": 0,
                            "summary_pl": (
                                f"RAG query={query}; hits=1; "
                                "evidence=case_recovery_INT-06_chunk_0; top=context fixture"
                            ),
                        }
                    ],
                },
            },
        )

    engine = AgentGraphEngine(
        planner=planner,
        constitution=constitution,
        tool_registry=AgentToolRegistry(handlers={"search_rag_knowledge": _rag}),
    )
    snapshot = build_initial_snapshot(
        case_id="case_duplicate_int06",
        engagement_id="eng_duplicate_int06",
        trace_id="sig_duplicate_int06",
    )

    result = engine.run(snapshot)

    assert executed_queries == ["case_recovery_INT-06 sprawa kontekst"]
    assert result.turns[1].tool_status == "error"
    assert result.snapshot.hitl_gate.reason == "duplicate_rag_research:case_context"


def test_int06_source_message_content_research_objective_is_blocked_after_success() -> None:
    constitution = load_constitution()
    planner = _DuplicateInt06MessageContentPlanner()
    executed_queries: list[str] = []

    def _rag(plan, _ctx):
        query = str(plan.arguments["query"])
        executed_queries.append(query)
        return ToolResult(
            status="ok",
            turn_summary_pl="RAG: 1 fragmentow dla zapytania.",
            snapshot_delta={
                "agent_memory": {
                    "reasoning_trace": [
                        {
                            "turn": 0,
                            "summary_pl": (
                                f"RAG query={query}; hits=1; "
                                "evidence=case_recovery_INT-06_chunk_0; top=message content fixture"
                            ),
                        }
                    ],
                },
            },
        )

    engine = AgentGraphEngine(
        planner=planner,
        constitution=constitution,
        tool_registry=AgentToolRegistry(handlers={"search_rag_knowledge": _rag}),
    )
    snapshot = build_initial_snapshot(
        case_id="case_duplicate_int06_msg",
        engagement_id="eng_duplicate_int06_msg",
        trace_id="sig_duplicate_int06_msg",
    )

    result = engine.run(snapshot)

    assert executed_queries == ["case_recovery_INT-06 tresc wiadomosci klienta"]
    assert result.turns[1].tool_status == "error"
    assert result.snapshot.hitl_gate.reason == "duplicate_rag_research:source_message_content"


def test_graph_rejects_tool_not_offered_this_turn_after_read_once() -> None:
    constitution = load_constitution()
    planner = _IgnoresAvailableToolsPlanner()
    executed = 0

    def _gmail(_plan, _ctx):
        nonlocal executed
        executed += 1
        return ToolResult(status="ok", turn_summary_pl="gmail read ok")

    engine = AgentGraphEngine(
        planner=planner,
        constitution=constitution,
        tool_registry=AgentToolRegistry(handlers={"search_gmail_thread": _gmail}),
    )
    snapshot = build_initial_snapshot(
        case_id="case_read_once_guard",
        engagement_id="eng_read_once_guard",
        trace_id="sig_read_once_guard",
    )

    result = engine.run(snapshot)

    assert executed == 1
    assert [turn.tool_name for turn in result.turns] == [
        "search_gmail_thread",
        "search_gmail_thread",
    ]
    assert result.turns[1].tool_status == "error"
    assert result.snapshot.hitl_gate.reason == "tool_not_offered:search_gmail_thread"


def test_illegal_delta_raises_validation_error() -> None:
    from pydantic import ValidationError

    from agent_runtime.snapshot_delta import apply_snapshot_delta
    from agent_runtime.store import build_initial_snapshot

    snapshot = build_initial_snapshot(
        case_id="case_bad",
        engagement_id="eng_bad",
        trace_id="sig_bad",
    )
    with pytest.raises(ValidationError):
        apply_snapshot_delta(snapshot, {"operational_status": {"steps_remaining": "not-a-number"}})


def test_journal_logs_tool_name() -> None:
    from agent_runtime.turn_journal import InMemoryAgentTurnJournal
    from agent_runtime.tool_result import ToolCallPlan, ToolResult

    journal = InMemoryAgentTurnJournal()
    constitution = load_constitution()
    engine = AgentGraphEngine(
        planner=MockSequencePlanner(["extract_facts_from_text"]),
        constitution=constitution,
        tool_registry=MockToolRegistry(),
        turn_journal=journal,
    )
    snapshot = build_initial_snapshot(
        case_id="case_j",
        engagement_id="eng_j",
        trace_id="sig_j",
    )
    engine.run(snapshot)
    rows = journal.list_turns("eng_j", limit=5)
    assert len(rows) >= 1
    assert rows[0].get("tool_name") == "extract_facts_from_text"
