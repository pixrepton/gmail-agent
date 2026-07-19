from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.openai_agent_client import OpenAIToolPlanner
from agent_runtime.settings import AgentRuntimeSettings, build_agent_planner_endpoints


def test_build_agent_planner_endpoints_cerebras_first_before_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk_test")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi_test")
    settings = AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="deepseek/deepseek-chat:free",
        max_rounds=12,
        openai_api_key="or_test",
        openai_base_url="https://openrouter.ai/api/v1",
        kalk_top_base_url="",
        kalk_top_agent_key="",
        kalk_top_timeout_sec=4,
        kalk_top_max_retries=3,
    )
    labels = [ep.label for ep in build_agent_planner_endpoints(settings)]
    assert labels[0] == "cerebras"
    assert labels.index("nvidia") < labels.index("openrouter")
    assert labels.index("groq") < labels.index("openrouter")
    assert labels == ["cerebras", "nvidia", "groq", "openrouter", "openrouter_fallback"]


def test_openai_planner_cerebras_transient_falls_back_to_nvidia(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from agent_runtime.constitution import load_constitution

    monkeypatch.setenv("CEREBRAS_API_KEY", "csk_test")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi_test")

    settings = AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=12,
        openai_api_key="or_test",
        openai_base_url="https://openrouter.ai/api/v1",
        kalk_top_base_url="",
        kalk_top_agent_key="",
        kalk_top_timeout_sec=4,
        kalk_top_max_retries=3,
    )

    class _RateLimitError(Exception):
        status_code = 429

        def __str__(self) -> str:
            return "429 rate limit"

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

    cerebras_client = MagicMock()
    cerebras_client.chat.completions.create.side_effect = _RateLimitError()

    nvidia_client = MagicMock()
    nvidia_client.chat.completions.create.return_value = response

    clients = [cerebras_client, nvidia_client]
    build_idx = {"n": 0}

    def _build_client(*, base_url: str, api_key: str) -> MagicMock:
        client = clients[build_idx["n"]]
        build_idx["n"] += 1
        return client

    planner = OpenAIToolPlanner(settings=settings)
    monkeypatch.setattr(planner, "_build_client", _build_client)

    plan = planner.plan_next_tool(
        snapshot=_minimal_snapshot(),
        available_tools=load_constitution().tool_allowlist,
        constitution=load_constitution(),
    )
    assert plan.tool_name == "extract_facts_from_text"
    assert cerebras_client.chat.completions.create.call_count == 1
    assert nvidia_client.chat.completions.create.call_count == 1
    assert build_idx["n"] == 2


def _minimal_snapshot():
    from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2

    return EngagementSnapshotV2.model_validate(
        {
            "engagement_id": "eng_x",
            "case_id": "case_x",
            "version": 1,
            "trace_id": "sig_x",
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
    )
