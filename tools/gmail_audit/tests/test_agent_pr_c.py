from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.validate import build_agent_doctor_check
from agent_runtime.cp2025 import check_cp2025_eligibility
from agent_runtime.kalk_top_client import KalkTopUnreachableError, call_calculate_offer
from agent_runtime.openai_agent_client import OpenAIToolPlanner
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan
from agent_runtime.tools_registry import AgentToolRegistry
from agent_runtime.tools.handlers import extract_facts_from_text
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2, HvacProfile, OperationalStatus


def _snapshot(**kwargs: object) -> EngagementSnapshotV2:
    base = {
        "engagement_id": "eng_c",
        "case_id": "case_c",
        "version": 1,
        "trace_id": "sig_c",
        "operational_status": {"code": "enriching", "steps_remaining": 8},
        "hvac_profile": {"location": {}},
        "gaps": [],
        "agent_memory": {
            "reasoning_trace": [],
            "tool_calls": [],
            "constitution_sections_used": [],
        },
        "actions": [],
        "hitl_gate": {"required": False, "reason": ""},
    }
    base.update(kwargs)
    return EngagementSnapshotV2.model_validate(base)


def test_cp2025_eligible_for_radlin_profile() -> None:
    eligible, _ = check_cp2025_eligibility(
        HvacProfile(heated_area_m2=128, building_type="single_family")
    )
    assert eligible is True


def test_tool_budget_exceeded() -> None:
    registry = AgentToolRegistry()
    ctx = ToolExecutionContext.from_snapshot(_snapshot())
    plan = ToolCallPlan(tool_name="report_gaps_and_stop", arguments={})
    first = registry.execute(plan, context=ctx)
    assert first.status == "ok"
    second = registry.execute(plan, context=ctx)
    assert second.status == "budget_exceeded"


def test_extract_facts_from_signal_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_extraction(_ctx: ToolExecutionContext, _text: str) -> dict[str, object]:
        return {
            "parse_status": "ok",
            "heated_area_m2": 128,
            "hvac_intent": "heat_pump_quote",
            "location_city": "Radlin",
        }

    monkeypatch.setattr(
        "agent_runtime.tools.handlers._run_llm_extraction",
        _fake_extraction,
    )
    ctx = ToolExecutionContext.from_snapshot(
        _snapshot(case_id=""),
        signal_payload={
            "subject": "Zapytanie 128 m² Radlin — pompa ciepła",
            "body_text": "Dom 128 m2 w Radlinie, proszę o ofertę.",
        },
    )
    result = extract_facts_from_text(ToolCallPlan(tool_name="extract_facts_from_text"), ctx)
    assert result.status == "ok"
    assert result.snapshot_delta.get("hvac_profile", {}).get("heated_area_m2") == 128


def test_doctor_warns_when_kalk_top_missing() -> None:
    settings = AgentRuntimeSettings(
        enabled=True,
        mode="prep",
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
    check = build_agent_doctor_check(settings)
    assert any("KALK_TOP_BASE_URL is not configured" in w for w in check.get("warnings", []))


def test_kalk_top_unreachable_maps_node_a_error() -> None:
    settings = AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=12,
        openai_api_key="x",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="http://127.0.0.1:9",
        kalk_top_agent_key="bad",
        kalk_top_timeout_sec=1,
        kalk_top_max_retries=1,
    )

    class _FailClient:
        def post(self, *args, **kwargs):
            raise TimeoutError("connection refused")

    import httpx

    original = httpx.Client

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return _FailClient()

        def __exit__(self, *args):
            return False

    httpx.Client = _Client  # type: ignore[misc]
    try:
        with pytest.raises(KalkTopUnreachableError):
            call_calculate_offer(
                {"schemaVersion": "1.0", "lead": {}, "building": {"heated_area": 100}, "preferences": {"heating": {}, "dhw": {}}},
                settings=settings,
            )
    finally:
        httpx.Client = original  # type: ignore[misc]

    from agent_runtime.tools.handlers import call_kalk_top_quote

    ctx = ToolExecutionContext.from_snapshot(
        _snapshot(hvac_profile={"heated_area_m2": 100, "location": {"city": "Radlin"}}),
        settings=settings,
    )
    result = call_kalk_top_quote(ToolCallPlan(tool_name="call_kalk_top_quote"), ctx)
    assert result.status == "node_a_error"


def test_kalk_top_timeout_maps_node_a_error() -> None:
    settings = AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=12,
        openai_api_key="x",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="http://127.0.0.1:9",
        kalk_top_agent_key="bad",
        kalk_top_timeout_sec=1,
        kalk_top_max_retries=0,
    )

    class _TimeoutClient:
        def post(self, *args, **kwargs):
            raise TimeoutError("read timed out")

    import httpx

    original = httpx.Client

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return _TimeoutClient()

        def __exit__(self, *args):
            return False

    httpx.Client = _Client  # type: ignore[misc]
    try:
        with pytest.raises(KalkTopUnreachableError):
            call_calculate_offer(
                {
                    "schemaVersion": "1.0",
                    "lead": {},
                    "building": {"heated_area": 100},
                    "preferences": {"heating": {}, "dhw": {}},
                },
                settings=settings,
            )
    finally:
        httpx.Client = original  # type: ignore[misc]


def test_openai_planner_parses_tool_call() -> None:
    settings = AgentRuntimeSettings(
        enabled=True,
        mode="prep",
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
    mock_client = MagicMock()
    fn = MagicMock()
    fn.name = "extract_facts_from_text"
    fn.arguments = "{}"
    tool_call = MagicMock()
    tool_call.function = fn
    message = MagicMock()
    message.tool_calls = [tool_call]
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    mock_client.chat.completions.create.return_value = response

    from agent_runtime.constitution import load_constitution

    planner = OpenAIToolPlanner(settings=settings, client=mock_client)
    plan = planner.plan_next_tool(
        snapshot=_snapshot(),
        available_tools=load_constitution().tool_allowlist,
        constitution=load_constitution(),
    )
    assert plan.tool_name == "extract_facts_from_text"
    mock_client.chat.completions.create.assert_called_once()
