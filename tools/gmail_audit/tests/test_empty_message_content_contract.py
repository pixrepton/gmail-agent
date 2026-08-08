"""Empty `message.content` must be retryable DELIVERY, never silent CAPABILITY.

Structured/text paths treat None / "" / whitespace-only content as invalid provider
output, fall through the existing provider chain, and only surface a typed failure
after the chain is exhausted. Reasoning content is never a business-output substitute.
Planner tool-call responses may legally have empty content when tool_calls are present.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.openai_agent_client import OpenAIToolPlanner
from central_llm_stage import run_central_structured_stage
from config import load_settings
from context_assembler import AssembledContext
from groq_client import (
    GroqClientError,
    _extract_openai_chat_message_text,
    deepseek_error_allows_fallback,
    request_structured_output,
)
from llm_contracts.signal_extraction import SignalExtractionResult
from llm_provider_router import LLMProvider, LLMRouter, LLMRouterError, classify_provider_error
from test_deepseek_structured_stage import _minimal_settings


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = ""
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, object]:
        return self._payload


_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "LLM_PRIMARY_PROVIDER": "groq",
        "LLM_FALLBACK_PROVIDERS": "cerebras",
        "LLM_STRUCTURED_PROVIDER_ALTERNATION": "0",
        "GROQ_API_KEY": "gsk_test",
        "GROQ_MODEL": "openai/gpt-oss-120b",
        "CEREBRAS_API_KEY": "csk_test",
        "CEREBRAS_MODEL": "gpt-oss-120b",
        "CEREBRAS_BASE_URL": "https://api.cerebras.ai/v1",
        "HTTP_TIMEOUT": "5",
        "HTTP_MAX_RETRIES": "1",
        "HTTP_RETRY_BASE_DELAY": "2",
    }
    env.update(overrides)
    return env


def _settings(**env_overrides: str):
    with patch("config._load_env_file", return_value=None):
        with patch.dict(os.environ, _env(**env_overrides), clear=True):
            return load_settings(require_groq=True, require_google=False)


def _chat_payload(*, content: object, finish_reason: str = "stop", **message_extra: object) -> dict[str, object]:
    message: dict[str, object] = {"content": content, **message_extra}
    return {"choices": [{"finish_reason": finish_reason, "message": message}]}


def _empty_chat(*, content: object = "", **extra: object) -> _FakeResponse:
    return _FakeResponse(200, _chat_payload(content=content, **extra))


def _valid_chat(payload: str = '{"ok": true}') -> _FakeResponse:
    return _FakeResponse(200, _chat_payload(content=payload))


@pytest.mark.parametrize(
    "exc",
    [
        GroqClientError("OpenAI-compatible response has empty `message.content`."),
        GroqClientError("OpenAI-compatible response has empty `message.content`.", details={"error_class": "empty_content"}),
        GroqClientError("provider returned empty content"),
        RuntimeError("message.content is None"),
    ],
)
def test_classify_empty_message_content_is_retryable(exc: Exception) -> None:
    info = classify_provider_error(exc)
    assert info.error_class == "empty_content"
    assert info.retryable is True


@pytest.mark.parametrize("content", [None, "", "   ", "\n\t"])
def test_extract_rejects_empty_or_whitespace_content(content: object) -> None:
    with pytest.raises(GroqClientError) as exc:
        _extract_openai_chat_message_text(_chat_payload(content=content, reasoning_content="THINK HARD"))
    assert "empty `message.content`" in str(exc.value)
    assert exc.value.details.get("error_class") == "empty_content"
    assert exc.value.details.get("has_reasoning_content") is True
    assert "THINK HARD" not in str(exc.value.details.get("content") or "")


def test_extract_never_promotes_reasoning_content_to_business_output() -> None:
    with pytest.raises(GroqClientError) as exc:
        _extract_openai_chat_message_text(
            _chat_payload(content="", reasoning_content='{"ok": true}', finish_reason="stop")
        )
    assert exc.value.details.get("has_reasoning_content") is True
    with pytest.raises(GroqClientError):
        # re-assert no silent success path exists
        text = _extract_openai_chat_message_text(
            _chat_payload(content=None, reasoning_content='{"ok": true}')
        )
        assert text  # pragma: no cover — must not reach


def test_extract_allows_empty_content_when_tool_calls_present_on_tool_path() -> None:
    text = _extract_openai_chat_message_text(
        _chat_payload(
            content=None,
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
        ),
        require_text_content=False,
    )
    assert text == ""


def test_router_falls_back_when_first_provider_returns_empty_content() -> None:
    calls: list[str] = []

    def empty_call() -> tuple[dict, dict]:
        calls.append("primary")
        raise GroqClientError(
            "OpenAI-compatible response has empty `message.content`.",
            details={"error_class": "empty_content"},
        )

    def ok_call() -> tuple[dict, dict]:
        calls.append("fallback")
        return {"ok": True}, {"model": "fallback-model"}

    response, meta = LLMRouter(
        [
            LLMProvider(provider="deepseek", backend="openai_compatible", model="ds", call=empty_call),
            LLMProvider(provider="cerebras", backend="openai_compatible", model="cb", call=ok_call),
        ]
    ).run()

    assert calls == ["primary", "fallback"]
    assert response == {"ok": True}
    assert meta["llm_selected_provider"] == "cerebras"
    assert meta["llm_fallback_used"] is True
    assert meta["llm_provider_attempts"][0]["error_class"] == "empty_content"
    assert meta["llm_provider_attempts"][0]["retryable"] is True


def test_router_exhausts_chain_on_all_empty_content() -> None:
    def empty_call() -> tuple[dict, dict]:
        raise GroqClientError("OpenAI-compatible response has empty `message.content`.")

    with pytest.raises(LLMRouterError) as exc:
        LLMRouter(
            [
                LLMProvider(provider="a", backend="x", model="m1", call=empty_call),
                LLMProvider(provider="b", backend="x", model="m2", call=empty_call),
            ]
        ).run()

    attempts = exc.value.details["llm_provider_attempts"]
    assert len(attempts) == 2
    assert all(a["error_class"] == "empty_content" for a in attempts)
    assert all(a["retryable"] is True for a in attempts)


def test_structured_http_empty_then_valid_uses_fallback_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings()
    calls: list[str] = []

    def fake_post(url: str, **_kwargs: object) -> _FakeResponse:
        calls.append(url)
        if "groq.com" in url:
            # Groq structured path uses Responses API shape.
            return _FakeResponse(
                200,
                {
                    "output_text": "",
                    "output": [],
                },
            )
        return _valid_chat('{"ok": true}')

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    result = request_structured_output(
        settings,
        "Return JSON.",
        '{"input": true}',
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
    )

    assert result.text == '{"ok": true}'
    assert any("groq.com" in u for u in calls)
    assert any("cerebras.ai" in u for u in calls)
    assert result.request_meta["llm_selected_provider"] == "cerebras"
    assert result.request_meta["llm_fallback_used"] is True
    assert result.request_meta["llm_provider_attempts"][0]["error_class"] == "empty_content"


def test_structured_all_providers_empty_raises_typed_delivery_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings()

    def fake_post(url: str, **_kwargs: object) -> _FakeResponse:
        if "groq.com" in url:
            return _FakeResponse(200, {"output_text": "", "output": []})
        return _empty_chat(content=None, reasoning_content="x")

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    with pytest.raises(GroqClientError) as exc:
        request_structured_output(
            settings,
            "Return JSON.",
            '{"input": true}',
            json_schema=_SCHEMA,
            schema_name="unit_test_schema",
        )

    attempts = exc.value.details.get("llm_provider_attempts") or []
    assert len(attempts) >= 2
    assert all(a.get("error_class") == "empty_content" for a in attempts if a.get("status") == "failed")
    assert "empty" in str(exc.value).lower() or "assistant text" in str(exc.value).lower()


def test_deepseek_empty_content_allows_central_chain_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _minimal_settings(
        deepseek_api_key="ds_test",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-v4-flash",
        llm_structured_provider_alternation=False,
        llm_primary_provider="groq",
        llm_fallback_providers=(),
        http_timeout=5,
        http_max_retries=1,
        http_retry_base_delay=2,
    )
    assert deepseek_error_allows_fallback(
        GroqClientError("OpenAI-compatible response has empty `message.content`.")
    ) is True

    payload = {"hvac_intent": "install", "raw_geographic_signal": "Katowice"}
    calls: list[str] = []

    def fake_post(url: str, **_kwargs: object) -> _FakeResponse:
        calls.append(url)
        if "deepseek.com" in url:
            return _empty_chat(content="", reasoning_content="thinking…")
        return _FakeResponse(
            200,
            {
                "output_text": json.dumps(payload),
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(payload)}],
                    }
                ],
            },
        )

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    with patch("central_llm_stage.build_context_assembler") as mock_asm:
        mock_asm.return_value.assemble.return_value = AssembledContext(
            company_context="ctx", assembled_at="2026-07-18T00:00:00+00:00"
        )
        out = run_central_structured_stage(
            settings,
            stage_name="signal_extraction",
            task_instructions="extract",
            prompt_input={"message": "test"},
            query_text="pompa",
            json_schema=SignalExtractionResult.model_json_schema(),
            schema_name="signal_extraction_v1",
            output_model=SignalExtractionResult,
        )

    assert any("deepseek.com" in u for u in calls)
    assert any("groq.com" in u for u in calls)
    assert out is not None
    assert out["central_llm_provider"] == "groq"
    assert out["response_json"]["hvac_intent"] == "install"


def test_malformed_json_success_is_not_classified_as_empty_content(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings()
    calls: list[str] = []

    def fake_post(url: str, **_kwargs: object) -> _FakeResponse:
        calls.append(url)
        return _FakeResponse(
            200,
            {
                "output_text": "not-json",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "not-json"}],
                    }
                ],
            },
        )

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    result = request_structured_output(
        settings,
        "Return JSON.",
        '{"input": true}',
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
    )
    assert result.text == "not-json"
    assert len(calls) == 1
    assert result.request_meta["llm_selected_provider"] == "groq"
    assert result.request_meta["llm_fallback_used"] is False


def test_rate_limit_still_retryable_distinct_from_empty_content() -> None:
    class _HttpExc(Exception):
        def __init__(self) -> None:
            super().__init__("429 too many requests")
            self.details = {"status_code": 429}

    info = classify_provider_error(_HttpExc())
    assert info.error_class == "rate_limit"
    assert info.retryable is True


def test_planner_empty_content_with_tool_calls_is_not_empty_content_failure() -> None:
    response = MagicMock()
    choice = MagicMock()
    choice.finish_reason = "tool_calls"
    message = MagicMock()
    message.content = None
    fn = MagicMock()
    fn.name = "report_gaps_and_stop"
    fn.arguments = "{}"
    call = MagicMock()
    call.function = fn
    message.tool_calls = [call]
    choice.message = message
    response.choices = [choice]

    plan = OpenAIToolPlanner._parse_tool_call(OpenAIToolPlanner.__new__(OpenAIToolPlanner), response)
    assert plan.tool_name == "report_gaps_and_stop"
