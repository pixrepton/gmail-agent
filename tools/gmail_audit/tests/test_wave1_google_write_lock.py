from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.authz import WRITE_OPERATION_PERMISSIONS
from agent_runtime.constitution import load_constitution
from agent_runtime.constitution_chat import CHAT_AGENT_TOOL_ALLOWLIST
from agent_runtime.openai_agent_client import OpenAIToolPlanner
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.tool_schemas import openai_tool_definitions
from agent_runtime.tools.handlers import HANDLERS
from agent_runtime.tools.write_executors import WRITE_EXECUTORS
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
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


def _snapshot() -> EngagementSnapshotV2:
    return EngagementSnapshotV2.model_validate(
        {
            "engagement_id": "eng_wave1_lock",
            "case_id": "case_wave1_lock",
            "version": 1,
            "trace_id": "sig_wave1_lock",
            "operational_status": {"code": "enriching", "steps_remaining": 6},
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
    )


def _propose_mutation_operation_enum() -> list[str]:
    tools = openai_tool_definitions(CHAT_AGENT_TOOL_ALLOWLIST)
    propose_mutation = next(tool for tool in tools if tool["function"]["name"] == "propose_mutation")
    operation = propose_mutation["function"]["parameters"]["properties"]["operation"]
    return list(operation["enum"])


def test_write_executors_exclude_google_write_operations() -> None:
    assert "send_email" not in WRITE_EXECUTORS
    assert "schedule_visit" not in WRITE_EXECUTORS


def test_propose_mutation_schema_excludes_google_write_operations() -> None:
    operations = _propose_mutation_operation_enum()
    assert "send_email" not in operations
    assert "schedule_visit" not in operations


def test_chat_planner_prompt_no_longer_suggests_google_write_operations() -> None:
    planner = OpenAIToolPlanner(settings=_settings())
    constitution = replace(load_constitution(), tool_allowlist=CHAT_AGENT_TOOL_ALLOWLIST)
    messages = planner._build_messages(
        snapshot=_snapshot(),
        constitution=constitution,
        available_tools=CHAT_AGENT_TOOL_ALLOWLIST,
    )
    system_text = messages[0]["content"]
    assert "propose_mutation(operation=generate_draft)" in system_text
    assert "schedule_visit" not in system_text
    assert "propose_mutation(operation=send_email)" not in system_text


def test_authz_permissions_exclude_removed_google_write_operations() -> None:
    assert "send_email" not in WRITE_OPERATION_PERMISSIONS
    assert "schedule_visit" not in WRITE_OPERATION_PERMISSIONS


def test_request_human_handoff_removed_from_runtime_handlers() -> None:
    assert "request_human_handoff" not in HANDLERS
