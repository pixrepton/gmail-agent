"""DeepSeek priority-1 agent-planner integration (DEEPSEEK-MIGRATION-1).

DeepSeek must be the first endpoint tried by OpenAIToolPlanner, ahead of Cerebras (previously
first), without changing the existing Cerebras → NVIDIA → Groq → OpenRouter → ... order or
semantics. See docs/core/LLM_PROVIDER_MAP.md and agent_runtime/settings.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution import load_constitution
from agent_runtime.openai_agent_client import OpenAIToolPlanner
from agent_runtime.settings import AgentRuntimeSettings, build_agent_planner_endpoints


def _settings(**overrides: object) -> AgentRuntimeSettings:
    base = dict(
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
    base.update(overrides)
    return AgentRuntimeSettings(**base)  # type: ignore[arg-type]


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


def _tool_call_response(tool_name: str = "extract_facts_from_text") -> MagicMock:
    fn = MagicMock()
    fn.name = tool_name
    fn.arguments = "{}"
    tool_call = MagicMock()
    tool_call.function = fn
    message = MagicMock()
    message.tool_calls = [tool_call]
    choice = MagicMock()
    choice.message = message
    choice.finish_reason = "tool_calls"
    response = MagicMock()
    response.choices = [choice]
    response.usage = None
    return response


def test_deepseek_absent_leaves_cerebras_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEYS", raising=False)
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk_test")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi_test")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")

    labels = [ep.label for ep in build_agent_planner_endpoints(_settings())]
    assert labels == ["cerebras", "nvidia", "groq", "openrouter"]


def test_deepseek_configured_becomes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds_test")
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk_test")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi_test")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")

    endpoints = build_agent_planner_endpoints(_settings())
    labels = [ep.label for ep in endpoints]
    assert labels == ["deepseek", "cerebras", "nvidia", "groq", "openrouter"]
    assert endpoints[0].model == "deepseek-v4-flash"
    assert endpoints[0].base_url == "https://api.deepseek.com"
    assert endpoints[0].thinking_enabled is True
    assert endpoints[0].reasoning_effort == "high"
    # non-DeepSeek endpoints are untouched — no thinking params leak elsewhere
    assert endpoints[1].thinking_enabled is False
    assert endpoints[1].reasoning_effort == ""


def test_deepseek_prepends_full_historical_planner_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds_test")
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk_test")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi_test")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or_test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("CURSOR_API_KEY", "cur_test")
    monkeypatch.delenv("AGENT_OPENAI_NATIVE_API_KEY", raising=False)

    labels = [ep.label for ep in build_agent_planner_endpoints(_settings(openai_api_key=""))]
    assert labels == ["deepseek", "cerebras", "nvidia", "groq", "openrouter", "openai", "cursor"]

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    labels_without_deepseek = [ep.label for ep in build_agent_planner_endpoints(_settings(openai_api_key=""))]
    assert labels_without_deepseek == ["cerebras", "nvidia", "groq", "openrouter", "openai", "cursor"]


def test_deepseek_model_and_base_url_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds_test")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("DEEPSEEK_REASONING_EFFORT", "high")

    endpoints = build_agent_planner_endpoints(_settings())
    assert endpoints[0].base_url == "https://api.deepseek.com/v1"
    assert endpoints[0].model == "deepseek-v4-flash"


def test_deepseek_thinking_disabled_clears_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds_test")
    monkeypatch.setenv("DEEPSEEK_THINKING_ENABLED", "0")

    endpoints = build_agent_planner_endpoints(_settings())
    assert endpoints[0].thinking_enabled is False
    assert endpoints[0].reasoning_effort == ""


def test_planner_calls_deepseek_first_and_sends_thinking_extra_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds_test")
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    deepseek_client = MagicMock()
    deepseek_client.chat.completions.create.return_value = _tool_call_response()

    planner = OpenAIToolPlanner(settings=_settings())
    monkeypatch.setattr(planner, "_build_client", lambda *, base_url, api_key: deepseek_client)

    plan = planner.plan_next_tool(
        snapshot=_minimal_snapshot(),
        available_tools=load_constitution().tool_allowlist,
        constitution=load_constitution(),
    )

    assert plan.tool_name == "extract_facts_from_text"
    assert not hasattr(plan, "reasoning_content")  # ToolCallPlan is extra="forbid" — structurally guaranteed
    deepseek_client.chat.completions.create.assert_called_once()
    call_kwargs = deepseek_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["extra_body"] == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    assert "tool_choice" not in call_kwargs
    assert "temperature" not in call_kwargs


def test_deepseek_retryable_failure_falls_through_to_cerebras(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retryable DeepSeek failures fall through to the pre-existing planner chain."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds_bad_key")
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk_test")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    class _ServerError(Exception):
        status_code = 500

        def __str__(self) -> str:
            return "500 temporary upstream error"

    deepseek_client = MagicMock()
    deepseek_client.chat.completions.create.side_effect = _ServerError()

    cerebras_client = MagicMock()
    cerebras_client.chat.completions.create.return_value = _tool_call_response()

    clients = [deepseek_client, cerebras_client]
    build_idx = {"n": 0}

    def _build_client(*, base_url: str, api_key: str) -> MagicMock:
        client = clients[build_idx["n"]]
        build_idx["n"] += 1
        return client

    planner = OpenAIToolPlanner(settings=_settings())
    monkeypatch.setattr(planner, "_build_client", _build_client)

    plan = planner.plan_next_tool(
        snapshot=_minimal_snapshot(),
        available_tools=load_constitution().tool_allowlist,
        constitution=load_constitution(),
    )

    assert plan.tool_name == "extract_facts_from_text"
    assert deepseek_client.chat.completions.create.call_count == 1
    assert cerebras_client.chat.completions.create.call_count == 1
    assert build_idx["n"] == 2


def test_deepseek_permanent_request_error_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad DeepSeek adapter payloads must not be silently masked by Cerebras."""
    from agent_runtime.openai_agent_client import OpenAIAgentPlannerError

    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds_bad_key")
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk_test")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    class _InvalidRequestError(Exception):
        status_code = 400

        def __str__(self) -> str:
            return "400 invalid_request_error: Thinking mode does not support this tool_choice"

    deepseek_client = MagicMock()
    deepseek_client.chat.completions.create.side_effect = _InvalidRequestError()
    cerebras_client = MagicMock()
    cerebras_client.chat.completions.create.return_value = _tool_call_response()

    clients = [deepseek_client, cerebras_client]
    build_idx = {"n": 0}

    def _build_client(*, base_url: str, api_key: str) -> MagicMock:
        client = clients[build_idx["n"]]
        build_idx["n"] += 1
        return client

    planner = OpenAIToolPlanner(settings=_settings())
    monkeypatch.setattr(planner, "_build_client", _build_client)

    with pytest.raises(OpenAIAgentPlannerError):
        planner.plan_next_tool(
            snapshot=_minimal_snapshot(),
            available_tools=load_constitution().tool_allowlist,
            constitution=load_constitution(),
        )

    assert deepseek_client.chat.completions.create.call_count == 1
    assert cerebras_client.chat.completions.create.call_count == 0


def test_deepseek_auth_config_failure_falls_back_to_cerebras(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepSeek auth/config failure is degraded primary-provider state, not an AI-OS outage."""

    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds_expired_key")
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk_test")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    class _AuthError(Exception):
        status_code = 401

        def __str__(self) -> str:
            return "401 invalid api key"

    deepseek_client = MagicMock()
    deepseek_client.chat.completions.create.side_effect = _AuthError()
    cerebras_client = MagicMock()
    cerebras_client.chat.completions.create.return_value = _tool_call_response()

    clients = [deepseek_client, cerebras_client]
    build_idx = {"n": 0}

    def _build_client(*, base_url: str, api_key: str) -> MagicMock:
        client = clients[build_idx["n"]]
        build_idx["n"] += 1
        return client

    planner = OpenAIToolPlanner(settings=_settings())
    monkeypatch.setattr(planner, "_build_client", _build_client)

    plan = planner.plan_next_tool(
        snapshot=_minimal_snapshot(),
        available_tools=load_constitution().tool_allowlist,
        constitution=load_constitution(),
    )

    assert plan.tool_name == "extract_facts_from_text"
    assert deepseek_client.chat.completions.create.call_count == 1
    assert cerebras_client.chat.completions.create.call_count == 1


def test_cerebras_permanent_failure_still_aborts_chain_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-DeepSeek positions keep their pre-existing 'non-retryable aborts the whole chain'
    behavior — this migration must not change that for any other provider."""
    from agent_runtime.openai_agent_client import OpenAIAgentPlannerError

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk_test")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi_test")

    class _AuthError(Exception):
        status_code = 401

        def __str__(self) -> str:
            return "401 invalid api key"

    cerebras_client = MagicMock()
    cerebras_client.chat.completions.create.side_effect = _AuthError()
    nvidia_client = MagicMock()
    nvidia_client.chat.completions.create.return_value = _tool_call_response()

    clients = [cerebras_client, nvidia_client]
    build_idx = {"n": 0}

    def _build_client(*, base_url: str, api_key: str) -> MagicMock:
        client = clients[build_idx["n"]]
        build_idx["n"] += 1
        return client

    planner = OpenAIToolPlanner(settings=_settings())
    monkeypatch.setattr(planner, "_build_client", _build_client)

    with pytest.raises(OpenAIAgentPlannerError):
        planner.plan_next_tool(
            snapshot=_minimal_snapshot(),
            available_tools=load_constitution().tool_allowlist,
            constitution=load_constitution(),
        )

    assert cerebras_client.chat.completions.create.call_count == 1
    assert nvidia_client.chat.completions.create.call_count == 0


def test_deepseek_stateless_replanning_steps_use_thinking_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Faithful-to-architecture proof: two successive plan_next_tool() calls as AgentGraphEngine._run
    performs across turns. Each call is a fresh DeepSeek request built from the current snapshot,
    not a raw Chat Completions continuation containing prior assistant/tool messages."""
    import agent_runtime.circuit_breaker as circuit_breaker

    circuit_breaker._breakers.clear()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds_test")
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    deepseek_client = MagicMock()
    deepseek_client.chat.completions.create.side_effect = [
        _tool_call_response("extract_facts_from_text"),
        _tool_call_response("report_gaps_and_stop"),
    ]

    planner = OpenAIToolPlanner(settings=_settings())
    monkeypatch.setattr(planner, "_build_client", lambda *, base_url, api_key: deepseek_client)

    constitution = load_constitution()
    turn1 = planner.plan_next_tool(
        snapshot=_minimal_snapshot(),
        available_tools=constitution.tool_allowlist,
        constitution=constitution,
    )
    assert turn1.tool_name == "extract_facts_from_text"

    # Runtime executes the tool and updates the snapshot (graph.py's real mechanism) — the
    # next planner call is a fresh, independent DeepSeek request, exactly as production does.
    turn2 = planner.plan_next_tool(
        snapshot=_minimal_snapshot(),
        available_tools=constitution.tool_allowlist,
        constitution=constitution,
    )
    assert turn2.tool_name == "report_gaps_and_stop"

    assert deepseek_client.chat.completions.create.call_count == 2
    for call in deepseek_client.chat.completions.create.call_args_list:
        assert call.kwargs["extra_body"] == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}


def test_second_turn_planner_request_does_not_show_completed_gmail_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds_test")
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    deepseek_client = MagicMock()
    deepseek_client.chat.completions.create.return_value = _tool_call_response("search_rag_knowledge")

    planner = OpenAIToolPlanner(settings=_settings())
    monkeypatch.setattr(planner, "_build_client", lambda *, base_url, api_key: deepseek_client)

    constitution = load_constitution()
    second_turn_available = tuple(
        tool for tool in constitution.tool_allowlist if tool != "search_gmail_thread"
    )

    plan = planner.plan_next_tool(
        snapshot=_minimal_snapshot(),
        available_tools=second_turn_available,
        constitution=constitution,
    )

    assert plan.tool_name == "search_rag_knowledge"
    call_kwargs = deepseek_client.chat.completions.create.call_args.kwargs
    tool_names = [tool["function"]["name"] for tool in call_kwargs["tools"]]
    prompt_text = "\n".join(str(message.get("content") or "") for message in call_kwargs["messages"])

    assert "search_gmail_thread" not in tool_names
    assert "search_gmail_thread" not in prompt_text
    assert "search_rag_knowledge" in tool_names
