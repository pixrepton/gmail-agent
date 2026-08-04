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
    assert proj.get("planner_action_hint")


def test_apply_nba_quality_at_understanding_source() -> None:
    from agent_runtime.recommended_next_step_quality import apply_nba_quality_to_understanding

    out = apply_nba_quality_to_understanding(
        {
            "situation_summary": {"case_family": "wycena_oferta", "business_area": "sales"},
            "missing_critical_fields": ["OZC"],
            "operator_explanation": {"essence_pl": "Lead 120 m2"},
            "next_best_action_recommendation": {
                "title_pl": "Wymagana ręczna ocena",
                "reason_pl": "escalate_internal",
                "kind": "recommendation",
            },
        },
        case_kind="wycena_oferta",
    )
    nba = out["next_best_action_recommendation"]
    assert nba["title_pl_raw"] == "Wymagana ręczna ocena"
    assert "ręczna ocena" not in nba["title_pl"].lower()
    assert nba["quality"]["sharpened"] is True
    assert nba["quality"]["planner_action_hint"]


def test_sharpen_followup_change_and_admin() -> None:
    follow = sharpen_recommended_next_step(
        title_pl="escalate_internal",
        case_kind="awaria_naprawa",
        what_changed_pl="Klient dodał: cieknie z zaworu",
    )
    assert "Follow-up" in follow or "cieknie" in follow.lower()
    assert "escalate_internal" not in follow.lower()

    admin = sharpen_recommended_next_step(
        title_pl="Wymagana ręczna ocena",
        case_kind="faktura_zakup",
    )
    assert "Administracja" in admin or "ksieg" in admin.lower() or "faktur" in admin.lower()
    assert "HVAC" in admin or "hvac" in admin.lower() or "request_operator" in admin


def test_empty_nba_stays_empty() -> None:
    assert (
        sharpen_recommended_next_step(
            title_pl="",
            reason_pl="",
            case_kind="wycena_oferta",
            essence_pl="Lead",
        )
        == ""
    )


def test_compact_view_surfaces_preferred_next_step() -> None:
    from agent_runtime.openai_agent_client import _compact_view
    from llm_contracts.engagement_snapshot_v2 import CaseUnderstandingProjection

    snap = build_initial_snapshot(case_id="c1", engagement_id="e1", trace_id="t1")
    snap = snap.model_copy(
        update={
            "case_kind": "wycena_oferta",
            "case_understanding": CaseUnderstandingProjection(
                source_signal_id="m1",
                essence_pl="Lead 150 m2",
                recommended_next_step_pl="Oferta: policz wycenę i draft quote",
                planner_action_hint="generate_draft_reply",
            ),
        }
    )
    view = _compact_view(snap)
    assert view["preferred_operator_next_step_pl"].startswith("Oferta")
    assert view["preferred_tool_class"] == "generate_draft_reply"
    assert "brain1_context" in view


def test_planner_prompt_binds_preferred_next_step() -> None:
    from agent_runtime.openai_agent_client import OpenAIToolPlanner
    from agent_runtime.constitution import load_constitution
    from llm_contracts.engagement_snapshot_v2 import CaseUnderstandingProjection

    snap = build_initial_snapshot(case_id="c1", engagement_id="e1", trace_id="t1")
    snap = snap.model_copy(
        update={
            "case_kind": "wycena_oferta",
            "case_understanding": CaseUnderstandingProjection(
                source_signal_id="m1",
                essence_pl="Lead",
                recommended_next_step_pl="Oferta: draft quote",
                planner_action_hint="generate_draft_reply",
            ),
        }
    )
    planner = OpenAIToolPlanner(settings=_settings())
    messages = planner._build_messages(
        snapshot=snap,
        constitution=load_constitution(),
        available_tools=("generate_draft_reply", "request_operator_clarification"),
        unavailable_notes={},
    )
    system = messages[0]["content"]
    assert "preferred_operator_next_step_pl" in system
    assert "ZAKAZ" in system
    assert "ręczna ocena" in system.lower()
    user = messages[1]["content"]
    assert "preferred_operator_next_step_pl" in user
    assert "Oferta: draft quote" in user
