from __future__ import annotations

import httpx
import pytest

from agent_runtime.kalk_top_client import KalkTopClientError, call_calculate_offer
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan
from agent_runtime.tools_registry import AgentToolRegistry


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=2,
        openai_api_key="test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="http://kalk-top.test",
        kalk_top_agent_key="test",
        kalk_top_timeout_sec=1,
        kalk_top_max_retries=1,
    )


class _NonJsonResponse:
    status_code = 200
    text = "upstream body must not escape the client boundary"

    def json(self) -> object:
        raise ValueError("Expecting value: line 1 column 1")


class _NonJsonClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> _NonJsonClient:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def post(self, *_args: object, **_kwargs: object) -> _NonJsonResponse:
        return _NonJsonResponse()


def test_non_json_success_is_a_typed_kalk_top_client_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "Client", _NonJsonClient)

    with pytest.raises(KalkTopClientError, match="non-JSON response"):
        call_calculate_offer({"schemaVersion": "1.0"}, settings=_settings())


def test_non_json_success_cannot_escape_the_planner_tool_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "Client", _NonJsonClient)
    snapshot = build_initial_snapshot(
        case_id="case-kalk-invalid-response",
        engagement_id="eng-kalk-invalid-response",
        trace_id="trace-kalk-invalid-response",
    )
    context = ToolExecutionContext.from_snapshot(snapshot, settings=_settings())

    result = AgentToolRegistry().execute(
        ToolCallPlan(tool_name="call_kalk_top_quote", arguments={}),
        context=context,
    )

    assert result.status == "error"
    assert result.failure_class == "DOWNSTREAM_RESULT_INVALID"
    assert result.failure_owner == "infra"
    assert result.retryable is False
    assert result.snapshot_delta["execution_attribution"]["safe_next_step"] == (
        "escalate_downstream_contract"
    )
