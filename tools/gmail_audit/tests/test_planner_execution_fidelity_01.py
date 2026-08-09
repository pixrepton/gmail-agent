"""PLANNER-EXEC-FIDELITY-01 — effective tools, envelope, budget, known-fact, draft gate."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution import load_constitution
from agent_runtime.draft_sanity import evaluate_draft_sanity
from agent_runtime.effective_tools import compute_effective_available_tools
from agent_runtime.envelope_presence import (
    classify_envelope_presence,
    policy_path_requires_envelope,
)
from agent_runtime.failure_taxonomy import classify_tool_handler_error
from agent_runtime.graph import AgentGraphEngine, _policy_enforcement_block
from agent_runtime.known_fact_guard import (
    guard_known_fact_reask,
    known_facts_from_snapshot,
)
from agent_runtime.planner_run_budget import (
    PlannerRunBudget,
    build_planner_run_budget,
)
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from agent_runtime.tools.handlers import generate_draft_reply, request_operator_clarification
from agent_runtime.tools_registry import MockToolRegistry
from llm_contracts.engagement_snapshot_v2 import (
    CaseUnderstandingProjection,
    PolicyActionEnvelopeV1,
)


def _settings(*, kalk_url: str = "") -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=4,
        openai_api_key="test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url=kalk_url,
        kalk_top_agent_key="",
        kalk_top_timeout_sec=4,
        kalk_top_max_retries=1,
    )


def test_kalk_filtered_when_url_missing() -> None:
    constitution = load_constitution()
    effective = compute_effective_available_tools(
        constitution.tool_allowlist,
        constitution=constitution,
        settings=_settings(kalk_url=""),
    )
    assert "call_kalk_top_quote" not in effective.offered
    assert any("KALK_TOP_BASE_URL" in n for n in effective.unavailable_notes)


def test_kalk_offered_when_url_configured() -> None:
    constitution = load_constitution()
    effective = compute_effective_available_tools(
        constitution.tool_allowlist,
        constitution=constitution,
        settings=_settings(kalk_url="http://host.docker.internal:8091"),
    )
    assert "call_kalk_top_quote" in effective.offered


def test_envelope_expected_absence_vs_wiring_failure() -> None:
    absent = classify_envelope_presence(
        None,
        case_understanding_present=False,
        policy_required=False,
        harness_mode=True,
    )
    assert absent["status"] == "expected_absence"
    assert absent["expected"] is True

    wiring = classify_envelope_presence(
        None,
        case_understanding_present=True,
        policy_required=True,
        harness_mode=False,
    )
    assert wiring["status"] == "wiring_failure"
    assert wiring["wiring_ok"] is False

    current = PolicyActionEnvelopeV1(
        freshness="current",
        policy_decision_id="pd1",
        action_proposal_id="ap1",
        decision_candidate_id="dc1",
        source_signal_id="sig1",
        allowed_by_policy=True,
    )
    present = classify_envelope_presence(current, case_understanding_present=True)
    assert present["status"] == "present_current"
    assert present["policy_decision_id"] == "pd1"


def test_policy_path_requires_envelope_with_understanding() -> None:
    snap = build_initial_snapshot(
        case_id="c1",
        engagement_id="e1",
        trace_id="t1",
    )
    snap = snap.model_copy(
        update={
            "case_understanding": CaseUnderstandingProjection(
                essence_pl="lead",
                recommended_next_step_pl="draft",
            )
        }
    )
    assert policy_path_requires_envelope(snap, {}) is True


def test_policy_enforcement_fail_closed_on_wiring_failure() -> None:
    snap = build_initial_snapshot(
        case_id="c1",
        engagement_id="e1",
        trace_id="t1",
    )
    snap = snap.model_copy(
        update={
            "case_understanding": CaseUnderstandingProjection(essence_pl="x"),
            "policy_action_envelope": None,
        }
    )
    blocked = _policy_enforcement_block(
        snap,
        ToolCallPlan(tool_name="generate_draft_reply", arguments={"intent": "quote"}),
        signal_payload={"policy_required": True},
    )
    assert blocked is not None
    assert blocked.failure_class == "POLICY_ENVELOPE_MISSING"
    assert blocked.status == "error"


def test_budget_exhaustion_deterministic() -> None:
    budget = build_planner_run_budget(max_rounds=2, constitution_tool_budget={"search_rag_knowledge": 1})
    assert budget.check_before_turn() is None
    budget.record_turn(tool_name="search_rag_knowledge", status="ok")
    budget.record_turn(tool_name="search_rag_knowledge", status="ok")
    hit = budget.check_before_turn()
    assert hit is not None
    assert "PLANNER_BUDGET_EXCEEDED" in hit


def test_known_fact_reask_blocks_area_and_city() -> None:
    snap = build_initial_snapshot(
        case_id="c1",
        engagement_id="e1",
        trace_id="t1",
    )
    snap = snap.model_copy(
        update={
            "hvac_profile": {
                "heated_area_m2": 150,
                "location": {"city": "Wrocław", "postal_code": None},
            }
        }
    )
    # model_copy with dict may need validate — use apply via model_validate path
    from llm_contracts.engagement_snapshot_v2 import HvacLocation, HvacProfile

    snap = snap.model_copy(
        update={
            "hvac_profile": HvacProfile(
                heated_area_m2=150,
                location=HvacLocation(city="Wrocław"),
            )
        }
    )
    known = known_facts_from_snapshot(snap)
    assert "heated_area_m2" in known
    assert "raw_geographic_signal" in known

    area = guard_known_fact_reask(
        tool_name="request_operator_clarification",
        arguments={"ask_pl": "Proszę o metraż budynku"},
        snapshot=snap,
    )
    assert area is not None
    assert "heated_area_m2" in area["fact_keys"]

    city = guard_known_fact_reask(
        tool_name="request_operator_clarification",
        arguments={"ask_pl": "Jakie jest miasto / lokalizacja?"},
        snapshot=snap,
    )
    assert city is not None
    assert "raw_geographic_signal" in city["fact_keys"]


def test_handler_blocks_known_fact_clarification() -> None:
    from llm_contracts.engagement_snapshot_v2 import HvacLocation, HvacProfile

    snap = build_initial_snapshot(case_id="c1", engagement_id="e1", trace_id="t1")
    snap = snap.model_copy(
        update={
            "hvac_profile": HvacProfile(
                heated_area_m2=150,
                location=HvacLocation(city="Wrocław"),
            )
        }
    )
    ctx = ToolExecutionContext.from_snapshot(snap, settings=_settings())
    result = request_operator_clarification(
        ToolCallPlan(
            tool_name="request_operator_clarification",
            arguments={"ask_pl": "Podaj proszę metraż"},
        ),
        ctx,
    )
    assert result.status == "error"
    assert result.failure_class == "PLANNER_KNOWN_FACT_REASK"


def test_draft_sanity_blocks_service_metraz_ozc() -> None:
    verdict = evaluate_draft_sanity(
        body=(
            "Dzień dobry,\n\nprosimy o uzupełnienie danych technicznych (metraż, OZC) "
            "dla instalacji.\n\nZespół TOP-INSTAL"
        ),
        case_kind="awaria_naprawa",
        intent="missing_info",
    )
    assert verdict["ok"] is False
    assert "service_draft_asks_sales_fields" in verdict["reason_codes"]


def test_draft_sanity_allows_sales_quote() -> None:
    from llm_contracts.engagement_snapshot_v2 import HvacLocation, HvacProfile

    snap = build_initial_snapshot(case_id="c1", engagement_id="e1", trace_id="t1")
    snap = snap.model_copy(
        update={
            "case_kind": "wycena_oferta",
            "hvac_profile": HvacProfile(
                heated_area_m2=150,
                location=HvacLocation(city="Wrocław"),
            ),
        }
    )
    ctx = ToolExecutionContext.from_snapshot(snap, settings=_settings())
    result = generate_draft_reply(
        ToolCallPlan(tool_name="generate_draft_reply", arguments={"intent": "quote"}),
        ctx,
    )
    assert result.status == "ok"
    assert result.snapshot_delta.get("actions", [{}])[0].get("enabled") is True


def test_draft_sanity_allows_bounded_service_handler() -> None:
    snap = build_initial_snapshot(case_id="c1", engagement_id="e1", trace_id="t1")
    snap = snap.model_copy(update={"case_kind": "awaria_naprawa"})
    ctx = ToolExecutionContext.from_snapshot(snap, settings=_settings())
    result = generate_draft_reply(
        ToolCallPlan(
            tool_name="generate_draft_reply",
            arguments={"intent": "missing_info"},
        ),
        ctx,
    )
    assert result.status == "ok"
    action = result.snapshot_delta.get("actions", [{}])[0]
    body = str(action.get("payload_pl") or "").lower()
    assert action.get("enabled") is True
    assert "model" in body
    assert "objaw" in body or "kod" in body
    assert "technik przyjedzie" not in body
    assert "umowimy" not in body
    assert "ozc" not in body


def test_config_missing_not_planner_failure() -> None:
    attr = classify_tool_handler_error(
        tool_name="call_kalk_top_quote",
        summary="KALK_TOP_BASE_URL is not configured",
        status="error",
    )
    assert attr["failure_class"] == "TOOL_CONFIGURATION_MISSING"
    assert attr["owner"] == "infra"


def test_graph_budget_stops_loop() -> None:
    from agent_runtime.tools_registry import AgentToolRegistry

    class AlwaysClarify:
        def plan_next_tool(self, **_kwargs):
            return ToolCallPlan(
                tool_name="request_operator_clarification",
                arguments={"ask_pl": "Operatorze, decyzja biznesowa?"},
            )

    constitution = load_constitution()
    snap = build_initial_snapshot(
        case_id="c_budget",
        engagement_id="e_budget",
        trace_id="t_budget",
    )
    engine = AgentGraphEngine(
        planner=AlwaysClarify(),
        constitution=constitution,
        tool_registry=AgentToolRegistry(),
    )
    ctx = ToolExecutionContext.from_snapshot(
        snap,
        settings=AgentRuntimeSettings(
            enabled=True,
            mode="prep",
            model="gpt-4o-mini",
            model_fallback="",
            max_rounds=1,
            openai_api_key="test",
            openai_base_url="https://api.openai.com/v1",
            kalk_top_base_url="",
            kalk_top_agent_key="",
            kalk_top_timeout_sec=4,
            kalk_top_max_retries=1,
        ),
    )
    result = engine.run(snap, context=ctx)
    assert result.planner_run_budget
    assert result.planner_run_budget.get("turns_used", 0) >= 1
    assert result.snapshot.hitl_gate.required is True


def test_graph_does_not_offer_kalk_without_url() -> None:
    chosen: list[str] = []

    class CapturePlanner:
        def plan_next_tool(self, *, available_tools, **_kwargs):
            chosen.extend(available_tools)
            return ToolCallPlan(
                tool_name="report_gaps_and_stop",
                arguments={},
            )

    constitution = load_constitution()
    snap = build_initial_snapshot(
        case_id="c_kalk",
        engagement_id="e_kalk",
        trace_id="t_kalk",
    )
    engine = AgentGraphEngine(
        planner=CapturePlanner(),
        constitution=constitution,
        tool_registry=MockToolRegistry(),
    )
    ctx = ToolExecutionContext.from_snapshot(snap, settings=_settings(kalk_url=""))
    engine.run(snap, context=ctx)
    assert chosen
    assert "call_kalk_top_quote" not in chosen


def test_correlation_ids_on_correlated_plan() -> None:
    from agent_runtime.policy_action_spine import correlate_tool_plan

    envelope = PolicyActionEnvelopeV1(
        freshness="current",
        policy_decision_id="pd_x",
        action_proposal_id="ap_x",
        decision_candidate_id="dc_x",
        source_signal_id="sig_x",
        allowed_by_policy=True,
    )
    plan = correlate_tool_plan(
        ToolCallPlan(tool_name="generate_draft_reply", arguments={"intent": "quote"}),
        envelope,
    )
    assert plan.policy_decision_id == "pd_x"
    assert plan.action_proposal_id == "ap_x"
    assert plan.correlation_status == "correlated"
