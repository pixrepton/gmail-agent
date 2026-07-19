from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution import load_constitution
from agent_runtime.openai_agent_client import OpenAIToolPlanner
from agent_runtime.planner import MockSequencePlanner
from agent_runtime.policy_guardrails import filter_planner_allowlist, guard_tool_plan
from agent_runtime.run import execute_agent_run
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.store import InMemoryOperatorEngagementStore
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from agent_runtime.tools_registry import AgentToolRegistry
from agent_runtime.turn_journal import InMemoryAgentTurnJournal
from agent_runtime.validate import (
    AgentRuntimeConfigError,
    assert_agent_run_ready,
    build_agent_doctor_check,
    validate_agent_runtime_settings,
)
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2


def _settings(**overrides: object) -> AgentRuntimeSettings:
    base = {
        "enabled": True,
        "mode": "prep",
        "model": "gpt-4o-mini",
        "model_fallback": "",
        "max_rounds": 12,
        "openai_api_key": "sk-test",
        "openai_base_url": "https://api.openai.com/v1",
        "kalk_top_base_url": "",
        "kalk_top_agent_key": "",
        "kalk_top_timeout_sec": 4,
        "kalk_top_max_retries": 3,
    }
    base.update(overrides)
    return AgentRuntimeSettings(**base)


def test_validate_agent_requires_openai_when_enabled() -> None:
    issues = validate_agent_runtime_settings(_settings(enabled=True, openai_api_key=""))
    assert any("OPENAI" in item for item in issues)


def test_legacy_enabled_is_inconsistent() -> None:
    issues = validate_agent_runtime_settings(_settings(enabled=True, mode="legacy"))
    assert issues


def test_guard_forbidden_tool() -> None:
    constitution = load_constitution()
    blocked = guard_tool_plan(ToolCallPlan(tool_name="send_email"), constitution=constitution)
    assert blocked is not None
    assert blocked.status == "error"


def test_filter_planner_allowlist_excludes_forbidden() -> None:
    constitution = load_constitution()
    filtered = filter_planner_allowlist(
        ("extract_facts_from_text", "send_email"),
        constitution,
    )
    assert "extract_facts_from_text" in filtered
    assert "send_email" not in filtered


def test_openai_planner_records_token_usage() -> None:
    settings = _settings()
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
    usage = MagicMock()
    usage.total_tokens = 150
    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    mock_client.chat.completions.create.return_value = response

    planner = OpenAIToolPlanner(settings=settings, client=mock_client)
    planner.plan_next_tool(
        snapshot=EngagementSnapshotV2.model_validate(
            {
                "engagement_id": "e",
                "case_id": "c",
                "version": 1,
                "operational_status": {"code": "enriching", "steps_remaining": 5},
            }
        ),
        available_tools=load_constitution().tool_allowlist,
        constitution=load_constitution(),
    )
    assert planner.last_tokens_used == 150


def test_openai_planner_finish_reason_stop_maps_report_gaps() -> None:
    settings = _settings()
    mock_client = MagicMock()
    message = MagicMock()
    message.tool_calls = []
    choice = MagicMock()
    choice.message = message
    choice.finish_reason = "stop"
    response = MagicMock()
    response.choices = [choice]
    mock_client.chat.completions.create.return_value = response
    planner = OpenAIToolPlanner(settings=settings, client=mock_client)
    plan = planner.plan_next_tool(
        snapshot=EngagementSnapshotV2.model_validate(
            {
                "engagement_id": "e",
                "case_id": "c",
                "version": 1,
                "trace_id": "t",
                "operational_status": {"code": "enriching", "steps_remaining": 5},
                "hvac_profile": {"location": {}},
                "gaps": [],
                "agent_memory": {"reasoning_trace": [], "tool_calls": [], "constitution_sections_used": []},
                "actions": [],
                "hitl_gate": {"required": False, "reason": ""},
            }
        ),
        available_tools=load_constitution().tool_allowlist,
        constitution=load_constitution(),
    )
    assert plan.tool_name == "report_gaps_and_stop"


def test_openai_planner_fallback_on_retryable_error() -> None:
    settings = _settings(model="primary-model", model_fallback="fallback-model")
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

    class _RateErr(Exception):
        status_code = 429

    mock_client.chat.completions.create.side_effect = [_RateErr("rate"), response]
    planner = OpenAIToolPlanner(settings=settings, client=mock_client)
    plan = planner.plan_next_tool(
        snapshot=EngagementSnapshotV2.model_validate(
            {
                "engagement_id": "e",
                "case_id": "c",
                "version": 1,
                "trace_id": "t",
                "operational_status": {"code": "enriching", "steps_remaining": 5},
                "hvac_profile": {"location": {}},
                "gaps": [],
                "agent_memory": {"reasoning_trace": [], "tool_calls": [], "constitution_sections_used": []},
                "actions": [],
                "hitl_gate": {"required": False, "reason": ""},
            }
        ),
        available_tools=load_constitution().tool_allowlist,
        constitution=load_constitution(),
    )
    assert plan.tool_name == "extract_facts_from_text"
    assert mock_client.chat.completions.create.call_args_list[1].kwargs["model"] == "fallback-model"


def test_execute_agent_run_with_mock_planner_and_journal() -> None:
    store = InMemoryOperatorEngagementStore()
    journal = InMemoryAgentTurnJournal()
    store.init_snapshot_from_signal(
        signal={"signal_id": "sig_exec"},
        case_id="case_exec",
        engagement_id="eng_exec",
    )
    settings = _settings(enabled=False)
    result = execute_agent_run(
        "eng_exec",
        store=store,
        planner=MockSequencePlanner(["extract_facts_from_text", "report_gaps_and_stop"]),
        settings=settings,
        turn_journal=journal,
        require_enabled=False,
    )
    assert result.version == 2
    assert result.snapshot.operational_status.code == "pending_operator"
    assert len(journal.list_turns("eng_exec")) == 2


def test_execute_agent_run_rejects_legacy_mode() -> None:
    store = InMemoryOperatorEngagementStore()
    store.init_snapshot_from_signal(
        signal={"signal_id": "sig_legacy"},
        case_id="case_legacy",
        engagement_id="eng_legacy",
    )
    with pytest.raises(AgentRuntimeConfigError):
        execute_agent_run(
            "eng_legacy",
            store=store,
            settings=_settings(enabled=True, mode="legacy"),
            planner=MockSequencePlanner(["report_gaps_and_stop"]),
            require_enabled=False,
        )


def test_registry_blocks_forbidden_with_constitution_in_context() -> None:
    registry = AgentToolRegistry()
    snap = EngagementSnapshotV2.model_validate(
        {
            "engagement_id": "e",
            "case_id": "c",
            "version": 1,
            "operational_status": {"code": "enriching", "steps_remaining": 5},
        }
    )
    ctx = ToolExecutionContext.from_snapshot(snap, constitution=load_constitution())
    result = registry.execute(ToolCallPlan(tool_name="create_offerdto"), context=ctx)
    assert result.status == "error"


def test_read_drive_file_uses_parser_chain() -> None:
    from agent_runtime.tools.handlers import read_google_drive_file

    parsed = {
        "file_id": "file_1",
        "file_name": "ozc.pdf",
        "extracted_text": "Budynek 128 m2 w Radlinie",
        "parser_name": "docling",
        "extraction_status": "ok",
    }
    snap = EngagementSnapshotV2.model_validate(
        {
            "engagement_id": "e",
            "case_id": "c",
            "version": 1,
            "operational_status": {"code": "enriching", "steps_remaining": 5},
        }
    )
    ctx = ToolExecutionContext.from_snapshot(snap, settings=_settings(enabled=False))
    with patch(
        "agent_runtime.drive_file_reader.download_and_parse_drive_file",
        return_value=parsed,
    ):
        result = read_google_drive_file(
            ToolCallPlan(tool_name="read_google_drive_file", arguments={"file_id": "file_1"}),
            ctx,
        )
    assert result.status == "ok"
    assert result.snapshot_delta["hvac_profile"]["heated_area_m2"] == 128


def test_build_agent_doctor_check_skipped_when_disabled() -> None:
    check = build_agent_doctor_check(_settings(enabled=False))
    assert check["status"] == "skipped"
