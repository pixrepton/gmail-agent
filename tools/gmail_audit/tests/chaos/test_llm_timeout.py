"""Chaos: LLM timeout — agent przelacza na fallback providera."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

TOOL_DIR = Path(__file__).resolve().parent.parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.openai_agent_client import OpenAIToolPlanner
from agent_runtime.settings import PlannerLLMEndpoint
from exceptions import LLMTimeoutError


def _make_test_snapshot() -> object:
    """Stworz snapshot o minimalnej strukturze dla _compact_view."""
    operational_status = SimpleNamespace()
    operational_status.model_dump = lambda: {"code": "raw_inquiry"}

    hvac_profile = SimpleNamespace()
    hvac_profile.model_dump = lambda exclude_none: {}

    gap = SimpleNamespace()
    gap.model_dump = lambda: {"label": "test"}
    action = SimpleNamespace()
    action.model_dump = lambda: {"tool": "noop"}

    reasoning_step = SimpleNamespace()
    reasoning_step.summary_pl = "test step"

    agent_memory = SimpleNamespace()
    agent_memory.reasoning_trace = [reasoning_step]

    snap = SimpleNamespace()
    snap.case_id = ""
    snap.case_kind = "lead_opportunity"
    snap.user_instruction = ""
    snap.operational_status = operational_status
    snap.hvac_profile = hvac_profile
    snap.gaps = [gap]
    snap.actions = [action]
    snap.agent_memory = agent_memory
    return snap


def test_llm_timeout_falls_back() -> None:
    """Gdy pierwszy provider timeoutuje, planner przelacza na nastepny endpoint z listy."""
    settings = MagicMock(spec=[])
    settings.agent_timeout_seconds = 45
    settings.agent_planner_personality_yaml_path = ""

    planner = OpenAIToolPlanner(settings=settings)

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.tool_calls = [MagicMock()]
    mock_response.choices[0].message.tool_calls[0].function.name = "noop"
    mock_response.choices[0].message.tool_calls[0].function.arguments = "{}"
    mock_response.choices[0].message.content = "ok"
    mock_response.usage = MagicMock()
    mock_response.usage.completion_tokens = 10
    mock_response.usage.prompt_tokens = 50

    groq_endpoint = PlannerLLMEndpoint(
        label="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key="gsk_test",
        model="groq/llama",
    )
    cerebras_endpoint = PlannerLLMEndpoint(
        label="cerebras",
        base_url="https://api.cerebras.ai/v1",
        api_key="cai_test",
        model="cerebras/llama",
    )

    with patch(
        "agent_runtime.openai_agent_client.build_agent_planner_endpoints",
        return_value=[groq_endpoint, cerebras_endpoint],
    ), patch(
        "agent_runtime.openai_agent_client._call_llm_with_timeout",
        side_effect=[
            LLMTimeoutError("Groq timeout"),
            mock_response,
        ],
    ):
        from agent_runtime.constitution import AgentConstitution
        constitution = AgentConstitution(
            hvac_rules="",
            company_context="",
            forbidden_actions=(),
            tool_allowlist=("noop",),
        )

        result = planner.plan_next_tool(
            snapshot=_make_test_snapshot(),
            available_tools=("noop",),
            constitution=constitution,
        )

    assert result is not None  # nie crash — fallback zadzialal
    assert planner.last_tokens_used > 0
