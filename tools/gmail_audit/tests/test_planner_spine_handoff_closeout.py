"""Tests for spine handoff + recommended_next_step sharpening."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.agent_reconcile import build_case_understanding_projection
from agent_runtime.graph import AgentGraphEngine
from agent_runtime.recommended_next_step_quality import sharpen_recommended_next_step
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan
from agent_runtime.tools_registry import MockToolRegistry
from eval_planner_spine_handoff import (
    apply_hvac_seed_to_snapshot,
    build_production_faithful_planner_signal,
)
from agent_runtime.constitution import load_constitution


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=3,
        openai_api_key="test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="",
        kalk_top_agent_key="",
        kalk_top_timeout_sec=4,
        kalk_top_max_retries=1,
    )


def test_spine_handoff_produces_current_envelope() -> None:
    intel = {
        "understanding_output": {
            "source_signal_id": "msg_int01",
            "operator_explanation": {
                "essence_pl": "Lead 150 m2 pod Wrocławiem",
                "why_pl": "Klient prosi o wycenę",
            },
            "missing_critical_fields": [],
            "next_best_action_recommendation": {
                "title_pl": "Wymagana ręczna ocena",
                "reason_pl": "escalate_internal",
            },
            "case_family": "wycena_oferta",
        }
    }
    handoff = build_production_faithful_planner_signal(
        case_id="case_INT-01",
        signal_id="sig_INT-01",
        message_id="msg_int01",
        subject="Wycena PC",
        body="Dom 150 m2 pod Wrocławiem, obecnie gaz.",
        case_intelligence_result=intel,
        case_kind="wycena_oferta",
        extraction={"heated_area_m2": 150, "city": "Wrocław"},
        policy_required=True,
        harness_mode=False,
    )
    signal = handoff["signal_payload"]
    presence = handoff["envelope_presence"]
    assert presence["status"] == "present_current"
    assert signal["policy_action_envelope"]["freshness"] == "current"
    assert signal["policy_action_envelope"]["policy_decision_id"]
    assert signal["policy_action_envelope"]["action_proposal_id"]
    assert signal["case_understanding_projection"]["essence_pl"]
    assert "ręczna ocena" not in signal["case_understanding_projection"][
        "recommended_next_step_pl"
    ].lower() or "Oferta" in signal["case_understanding_projection"][
        "recommended_next_step_pl"
    ]


def test_graph_run_with_envelope_correlates_plan() -> None:
    intel = {
        "understanding_output": {
            "source_signal_id": "msg_x",
            "operator_explanation": {"essence_pl": "Awaria pompy", "why_pl": "brak ciepła"},
            "missing_critical_fields": ["objaw"],
            "next_best_action_recommendation": {
                "title_pl": "escalate_internal",
            },
            "case_family": "awaria_naprawa",
        }
    }
    handoff = build_production_faithful_planner_signal(
        case_id="case_INT-04",
        signal_id="sig_INT-04",
        message_id="msg_x",
        subject="Awaria",
        body="Pompa nie grzeje od wczoraj",
        case_intelligence_result=intel,
        case_kind="awaria_naprawa",
        policy_required=True,
    )
    signal = handoff["signal_payload"]
    assert signal["policy_action_envelope"]["freshness"] == "current"

    class PlanDraft:
        def plan_next_tool(self, **_kwargs):
            return ToolCallPlan(
                tool_name="generate_draft_reply",
                arguments={"intent": "missing_info"},
            )

    snap = build_initial_snapshot(
        case_id="case_INT-04", engagement_id="e_int04", trace_id="t_int04"
    )
    snap = snap.model_copy(update={"case_kind": "awaria_naprawa"})
    snap = apply_hvac_seed_to_snapshot(snap, signal)
    engine = AgentGraphEngine(
        planner=PlanDraft(),
        constitution=load_constitution(),
        tool_registry=MockToolRegistry(),
    )
    # Use real handlers path via AgentToolRegistry for draft sanity
    from agent_runtime.tools_registry import AgentToolRegistry

    engine = AgentGraphEngine(
        planner=PlanDraft(),
        constitution=load_constitution(),
        tool_registry=AgentToolRegistry(),
    )
    ctx = ToolExecutionContext.from_snapshot(
        snap, settings=_settings(), signal_payload=signal
    )
    result = engine.run(snap, context=ctx)
    env = result.snapshot.policy_action_envelope
    assert env is not None
    assert env.freshness == "current"
    # Service missing_info draft must fail sanity — not enabled final sendable draft
    assert result.snapshot.hitl_gate.required is True


def test_sharpen_vague_sales_and_service() -> None:
    sales = sharpen_recommended_next_step(
        title_pl="Wymagana ręczna ocena",
        reason_pl="escalate_internal",
        case_kind="wycena_oferta",
        missing_critical_fields=[],
        essence_pl="Lead 150 m2",
    )
    assert "ręczna ocena" not in sales.lower()
    assert "Oferta" in sales or "ofert" in sales.lower() or "wycen" in sales.lower()

    service = sharpen_recommended_next_step(
        title_pl="escalate_internal",
        case_kind="awaria_naprawa",
        missing_critical_fields=["objaw"],
    )
    assert "Serwis" in service
    assert "metraż" not in service.lower() or "bez metrażu" in service.lower()


def test_projection_uses_sharpen() -> None:
    proj = build_case_understanding_projection(
        {
            "understanding_output": {
                "source_signal_id": "m1",
                "operator_explanation": {"essence_pl": "Lead"},
                "next_best_action_recommendation": {
                    "title_pl": "Wymagana ręczna ocena",
                },
                "case_family": "wycena_oferta",
            }
        },
        message_id="m1",
    )
    assert proj is not None
    assert "Oferta" in proj["recommended_next_step_pl"] or "wycen" in proj[
        "recommended_next_step_pl"
    ].lower()
