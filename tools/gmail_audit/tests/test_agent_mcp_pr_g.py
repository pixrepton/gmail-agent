from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.mcp_service import (
    MCP_TOOL_NAMES,
    AgentMcpService,
    build_agent_mcp_doctor_check,
    dispatch_mcp_tool,
)
from agent_runtime.planner import MockSequencePlanner
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.store import InMemoryOperatorEngagementStore, build_initial_snapshot
from agent_runtime.tools_registry import MockToolRegistry
from agent_runtime.graph import AgentGraphEngine
from agent_runtime.constitution import load_constitution
from agent_runtime.run import AgentRunResult
from llm_contracts.engagement_snapshot_v2 import ActionItem


def _settings(*, enabled: bool = True, mode: str = "prep") -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        enabled=enabled,
        mode=mode,
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=12,
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="",
        kalk_top_agent_key="",
        kalk_top_timeout_sec=4,
        kalk_top_max_retries=3,
    )


def _service_with_snapshot(
    *,
    engagement_id: str = "eng_mcp",
    case_id: str = "case_mcp",
    hitl: bool = False,
    actions: list[ActionItem] | None = None,
) -> AgentMcpService:
    store = InMemoryOperatorEngagementStore()
    snap = store.init_snapshot_from_signal(
        signal={"signal_id": "sig_mcp"},
        case_id=case_id,
        engagement_id=engagement_id,
    )
    if hitl or actions:
        from agent_runtime.snapshot_delta import apply_snapshot_delta

        delta: dict = {}
        if hitl:
            delta["hitl_gate"] = {"required": True, "reason": "draft_ready_for_approval"}
            delta["operational_status"] = {"code": "pending_operator"}
        if actions:
            delta["actions"] = [a.model_dump(mode="python") for a in actions]
        patched = apply_snapshot_delta(snap, delta)
        store.save_snapshot(patched, expected_version=1)
    return AgentMcpService(store=store, settings=_settings())


def test_mcp_tool_names_match_plan() -> None:
    assert set(MCP_TOOL_NAMES) == {
        "get_engagement_snapshot",
        "list_active_engagements",
        "trigger_agent_run",
        "approve_hitl_action",
        "get_agent_turns",
    }


def test_get_engagement_snapshot_by_case_id() -> None:
    svc = _service_with_snapshot()
    out = svc.get_engagement_snapshot(case_id="case_mcp")
    assert out["ok"] is True
    assert out["snapshot"]["engagement_id"] == "eng_mcp"


def test_list_active_engagements_filters_status() -> None:
    svc = _service_with_snapshot(hitl=True)
    pending = svc.list_active_engagements(status="pending_operator")
    assert pending["ok"] is True
    assert pending["count"] == 1
    raw = svc.list_active_engagements(status="ready_for_quote")
    assert raw["count"] == 0


def test_list_blocking_gaps_only() -> None:
    from agent_runtime.snapshot_delta import apply_snapshot_delta

    svc = _service_with_snapshot(hitl=True)
    snap = svc.store.load_snapshot("eng_mcp")
    assert snap is not None
    patched = apply_snapshot_delta(
        snap,
        {
            "gaps": [
                {"field": "thermal_demand_kw", "severity": "blocking", "ask_pl": "OZC?"},
            ],
            "operational_status": {"blocking": True},
        },
    )
    svc.store.save_snapshot(patched, expected_version=snap.version)
    out = svc.list_active_engagements(blocking_gaps_only=True)
    assert out["count"] == 1


def test_list_hitl_required_only() -> None:
    svc = _service_with_snapshot(hitl=True)
    out = svc.list_active_engagements(hitl_required_only=True)
    assert out["count"] == 1


def test_get_snapshot_include_full() -> None:
    svc = _service_with_snapshot()
    out = svc.get_engagement_snapshot(engagement_id="eng_mcp", include_full=True)
    assert out["ok"] is True
    assert "full" in out["snapshot"]
    assert out["snapshot"]["full"]["engagement_id"] == "eng_mcp"


def test_approve_hitl_clears_gate_when_action_already_enabled() -> None:
    svc = _service_with_snapshot(
        hitl=True,
        actions=[
            ActionItem(
                id="draft_reply",
                enabled=True,
                payload_pl="Draft body",
                disabled_reason_pl=None,
            )
        ],
    )
    out = svc.approve_hitl_action(engagement_id="eng_mcp", action_id="draft_reply", operator_id="op1")
    assert out["ok"] is True
    assert out["version"] >= 2
    assert out.get("new_status") == "ready_for_quote"
    assert out["adjudication"]["adjudication_kind"] == "hitl_action_approved"
    snap = svc.store.load_snapshot("eng_mcp")
    assert snap is not None
    assert snap.hitl_gate.required is False
    assert snap.actions[0].enabled is True


def test_approve_hitl_requires_enabled_action() -> None:
    svc = _service_with_snapshot(
        hitl=True,
        actions=[
            ActionItem(
                id="draft_reply",
                enabled=False,
                payload_pl="Draft body",
                disabled_reason_pl="pending approval",
            )
        ],
    )
    out = svc.approve_hitl_action(engagement_id="eng_mcp", action_id="draft_reply", operator_id="op1")
    assert out["ok"] is False
    assert "enabled=True" in out["error"]


def test_approve_hitl_allows_gap_only_staging_draft_reply() -> None:
    svc = _service_with_snapshot(hitl=True, actions=[])
    out = svc.approve_hitl_action(engagement_id="eng_mcp", action_id="draft_reply", operator_id="op1")
    assert out["ok"] is True
    assert out["action_id"] == "draft_reply"
    assert out["operator_id"] == "op1"
    snap = svc.store.load_snapshot("eng_mcp")
    assert snap is not None
    assert snap.hitl_gate.required is False
    assert str(snap.operational_status.code or "") == "ready_for_quote"


def test_approve_hitl_requires_active_gate() -> None:
    svc = _service_with_snapshot(
        actions=[ActionItem(id="draft_reply", enabled=False)],
    )
    out = svc.approve_hitl_action(engagement_id="eng_mcp", action_id="draft_reply")
    assert out["ok"] is False
    assert "hitl_gate" in out["error"]


def test_trigger_agent_run_with_injected_runner() -> None:
    store = InMemoryOperatorEngagementStore()
    snap = store.init_snapshot_from_signal(
        signal={"signal_id": "sig_trig"},
        case_id="case_trig",
        engagement_id="eng_trig",
    )

    def _fake_run(engagement_id: str, **kwargs: object) -> AgentRunResult:
        engine = AgentGraphEngine(
            planner=MockSequencePlanner(["extract_facts_from_text"]),
            constitution=load_constitution(),
            tool_registry=MockToolRegistry(),
        )
        graph = engine.run(snap)
        version = store.save_snapshot(graph.snapshot, expected_version=1)
        final = graph.snapshot.model_copy(update={"version": version})
        return AgentRunResult(snapshot=final, graph=graph, version=version)

    svc = AgentMcpService(store=store, settings=_settings(), run_agent=_fake_run)
    out = svc.trigger_agent_run(engagement_id="eng_trig")
    assert out["ok"] is True
    assert out["turns"] >= 1
    assert out["snapshot"]["hvac_profile"]["heated_area_m2"] == 128


def test_trigger_rejects_legacy_mode() -> None:
    svc = AgentMcpService(
        store=InMemoryOperatorEngagementStore(),
        settings=_settings(mode="legacy"),
    )
    out = svc.trigger_agent_run(engagement_id="eng_x")
    assert out["ok"] is False
    assert "legacy" in out["error"]


def test_get_agent_turns_from_memory_journal() -> None:
    from agent_runtime.turn_journal import InMemoryAgentTurnJournal
    from agent_runtime.tool_result import ToolCallPlan, ToolResult

    journal = InMemoryAgentTurnJournal()
    journal.append_turn(
        engagement_id="eng_mcp",
        snapshot_version=1,
        trace_id="t1",
        plan=ToolCallPlan(tool_name="extract_facts_from_text", arguments={}),
        result=ToolResult(status="ok", turn_summary_pl="Ekstrakcja faktów."),
    )
    svc = _service_with_snapshot()
    svc.turn_journal = journal
    out = svc.get_agent_turns(engagement_id="eng_mcp")
    assert out["ok"] is True
    assert out["count"] == 1
    assert "Ekstrakcja" in out["turns"][0]["turn_summary_pl"]


def test_dispatch_unknown_tool() -> None:
    svc = _service_with_snapshot()
    out = dispatch_mcp_tool(svc, "nope", {})
    assert out["ok"] is False


def test_mcp_doctor_check_reports_tools() -> None:
    check = build_agent_mcp_doctor_check()
    assert check["id"] == "agent_runtime_mcp"
    assert len(check["tools"]) == 5
    assert check["status"] in {"ok", "optional", "warn"}


def test_mcp_server_tool_schema_names() -> None:
    pytest.importorskip("mcp")
    from agent_runtime.mcp_server import _tool_schemas

    names = {t.name for t in _tool_schemas()}
    assert names == set(MCP_TOOL_NAMES)
