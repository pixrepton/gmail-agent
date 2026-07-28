"""Tests for bounded LLM provider fallback on structured stages."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import ConfigError, load_settings
from groq_client import (
    GroqClientError,
    request_structured_output,
    reset_structured_alternation_counter_for_tests,
    run_structured_stage,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object], *, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, object]:
        return self._payload


_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}


def _groq_success() -> _FakeResponse:
    return _FakeResponse(
        200,
        {
            "output_text": '{"ok": true}',
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"ok": true}'}],
                }
            ],
        },
    )


def _groq_invalid_json_text() -> _FakeResponse:
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


def _cerebras_success() -> _FakeResponse:
    return _FakeResponse(
        200,
        {
            "choices": [
                {
                    "message": {
                        "content": '{"ok": true}',
                    }
                }
            ],
        },
    )


def _nvidia_success() -> _FakeResponse:
    return _cerebras_success()


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "LLM_PRIMARY_PROVIDER": "groq",
        "LLM_FALLBACK_PROVIDERS": "cerebras",
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


def _call(settings, *, correlation_id: str | None = None):
    return request_structured_output(
        settings,
        "Return JSON.",
        '{"input": true}',
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
        correlation_id=correlation_id,
    )


def test_structured_alternation_defaults_on_without_explicit_env() -> None:
    settings = _settings()
    assert settings.llm_structured_provider_alternation is True


def test_structured_alternation_explicit_off() -> None:
    settings = _settings(LLM_STRUCTURED_PROVIDER_ALTERNATION="0")
    assert settings.llm_structured_provider_alternation is False


def test_structured_alternation_groq_429_uses_fallback_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_structured_alternation_counter_for_tests()
    settings = _settings()
    calls: list[str] = []

    def fake_post(url: str, **_kwargs: object) -> _FakeResponse:
        calls.append(url)
        if "groq.com" in url:
            return _FakeResponse(429, {"error": {"message": "rate limited"}})
        return _cerebras_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    result = request_structured_output(
        settings,
        "Return JSON.",
        '{"input": true}',
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
        stage_name="signal_extraction",
        correlation_id="msg-2",
    )

    assert result.text == '{"ok": true}'
    assert any("groq.com" in u for u in calls)
    assert any("cerebras.ai" in u for u in calls)
    assert result.request_meta.get("llm_structured_provider_alternation") is True
    assert result.request_meta["llm_selected_provider"] == "cerebras"
    assert result.request_meta["llm_fallback_used"] is True


def test_structured_alternation_reuses_slot_across_input_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_structured_alternation_counter_for_tests()
    settings = _settings()
    urls: list[str] = []

    def fake_post(url: str, **_kwargs: object):
        urls.append(url)
        if "groq.com" in url:
            return _FakeResponse(429, {"error": {"message": "rate limited"}})
        return _cerebras_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    result = request_structured_output(
        settings,
        "Return JSON.",
        '{"input": true}',
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
        input_variants=[
            {"mode": "compact", "input": '{"input": true}'},
            {"mode": "reduced", "input": '{"input": true}'},
        ],
        stage_name="intake_reasoning",
        correlation_id="msg-3",
    )

    assert result.text == '{"ok": true}'
    assert any("groq.com" in u for u in urls)
    assert any("cerebras.ai" in u for u in urls)
    assert result.request_meta.get("llm_alternation_slot") == "groq"
    assert result.request_meta["llm_selected_provider"] == "cerebras"
    assert result.request_meta["llm_fallback_used"] is True


def test_structured_alternation_is_stable_for_each_message_stage_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_structured_alternation_counter_for_tests()
    settings = _settings()
    urls: list[str] = []

    def fake_post(url: str, **_kwargs: object):
        urls.append(url)
        return _cerebras_success() if "cerebras.ai" in url else _groq_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    signal = request_structured_output(
        settings,
        "Return JSON.",
        '{"input": true}',
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
        stage_name="signal_extraction",
        correlation_id="msg-stage-stable",
    )
    intake = request_structured_output(
        settings,
        "Return JSON.",
        '{"input": true}',
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
        stage_name="intake_reasoning",
        correlation_id="msg-stage-stable",
    )
    signal_repeated = request_structured_output(
        settings,
        "Return JSON.",
        '{"input": true}',
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
        stage_name="signal_extraction",
        correlation_id="msg-stage-stable",
    )
    intake_repeated = request_structured_output(
        settings,
        "Return JSON.",
        '{"input": true}',
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
        stage_name="intake_reasoning",
        correlation_id="msg-stage-stable",
    )

    assert signal.request_meta["llm_alternation_slot"] == signal_repeated.request_meta["llm_alternation_slot"]
    assert intake.request_meta["llm_alternation_slot"] == intake_repeated.request_meta["llm_alternation_slot"]
    assert signal.request_meta["llm_alternation_key"] == signal_repeated.request_meta["llm_alternation_key"]
    assert intake.request_meta["llm_alternation_key"] == intake_repeated.request_meta["llm_alternation_key"]
    assert len(urls) == 4


def test_structured_alternation_distinct_stages_rotate(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_structured_alternation_counter_for_tests()
    settings = _settings()
    urls: list[str] = []

    def fake_post(url: str, **_kwargs: object):
        urls.append(url)
        if "cerebras.ai" in url:
            return _cerebras_success()
        return _groq_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    first = request_structured_output(
        settings,
        "Return JSON.",
        '{"input": true}',
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
        stage_name="stage_a",
        correlation_id="msg-1",
    )
    second = request_structured_output(
        settings,
        "Return JSON.",
        '{"input": true}',
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
        stage_name="stage_b",
        correlation_id="msg-2",
    )
    third = request_structured_output(
        settings,
        "Return JSON.",
        '{"input": true}',
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
        stage_name="stage_a",
        correlation_id="msg-1",
    )

    assert first.request_meta.get("llm_alternation_slot") == "groq"
    assert second.request_meta.get("llm_alternation_slot") == "cerebras"
    assert third.request_meta.get("llm_alternation_slot") == "groq"
    assert len(urls) == 3
    assert "groq.com" in urls[0]
    assert "cerebras.ai" in urls[1]
    assert "groq.com" in urls[2]


def test_structured_provider_selection_is_stable_per_message_and_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_structured_alternation_counter_for_tests()
    settings = _settings()

    def fake_post(url: str, **_kwargs: object):
        return _cerebras_success() if "cerebras.ai" in url else _groq_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    def call(correlation_id: str, stage_name: str = "business_reasoning"):
        return request_structured_output(
            settings,
            "Return JSON.",
            '{"input": true}',
            json_schema=_SCHEMA,
            schema_name="unit_test_schema",
            stage_name=stage_name,
            correlation_id=correlation_id,
        )

    first = call("msg-stable")
    for index in range(12):
        call(f"msg-other-{index}", stage_name=f"stage-{index % 3}")
    repeated = call("msg-stable")

    assert repeated.request_meta["llm_alternation_slot"] == first.request_meta["llm_alternation_slot"]
    assert repeated.request_meta["llm_alternation_key"] == first.request_meta["llm_alternation_key"]
    assert repeated.request_meta["llm_alternation_strategy"] == "stable_correlation_hash_v1"


def test_temperature_reaches_responses_and_openai_compatible_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    bodies: list[tuple[str, dict[str, object]]] = []

    def fake_post(url: str, **kwargs: object):
        bodies.append((url, kwargs.get("json") or {}))
        return _cerebras_success() if "cerebras.ai" in url else _groq_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    request_structured_output(
        settings,
        "Return JSON.",
        '{"input": true}',
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
        stage_name="signal_extraction",
        correlation_id="msg-2",
        temperature=0.31,
    )
    request_structured_output(
        settings,
        "Return JSON.",
        '{"input": true}',
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
        stage_name="signal_extraction",
        correlation_id="msg-0",
        temperature=0.47,
    )

    groq_body = next(body for url, body in bodies if "groq.com" in url)
    cerebras_body = next(body for url, body in bodies if "cerebras.ai" in url)
    assert groq_body["temperature"] == 0.31
    assert cerebras_body["temperature"] == 0.47


def test_structured_alternation_distributes_by_stable_correlation(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_structured_alternation_counter_for_tests()
    settings = _settings()
    assert settings.llm_structured_provider_alternation is True
    urls: list[str] = []

    def fake_post(url: str, **_kwargs: object):
        urls.append(url)
        if "cerebras.ai" in url:
            return _cerebras_success()
        return _groq_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    first = _call(settings, correlation_id="msg-0")
    second = _call(settings, correlation_id="msg-2")
    third = _call(settings, correlation_id="msg-0")
    assert len(urls) == 3
    assert "groq.com" in urls[0]
    assert "cerebras.ai" in urls[1]
    assert "groq.com" in urls[2]
    assert first.request_meta.get("llm_alternation_slot") == "groq"
    assert second.request_meta.get("llm_alternation_slot") == "cerebras"
    assert third.request_meta.get("llm_alternation_slot") == "groq"
    assert first.request_meta.get("llm_structured_provider_alternation") is True


def test_structured_alternation_disabled_without_cerebras_key(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_structured_alternation_counter_for_tests()
    settings = _settings(LLM_STRUCTURED_PROVIDER_ALTERNATION="1", CEREBRAS_API_KEY="")
    assert settings.llm_structured_provider_alternation is False
    urls: list[str] = []

    def fake_post(url: str, **_kwargs: object):
        urls.append(url)
        return _groq_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    _call(settings)
    _call(settings)
    assert len(urls) == 2
    assert all("groq.com" in u for u in urls)


def test_groq_success_does_not_use_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(LLM_STRUCTURED_PROVIDER_ALTERNATION="0")
    calls: list[str] = []

    def fake_post(url: str, **_: object) -> _FakeResponse:
        calls.append(url)
        return _groq_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    result = _call(settings)

    assert result.text == '{"ok": true}'
    assert len(calls) == 1
    assert result.request_meta["llm_selected_provider"] == "groq"
    assert result.request_meta["llm_fallback_used"] is False
    assert result.request_meta["llm_provider_attempts"] == [
        {
            "provider": "groq",
            "model": "openai/gpt-oss-120b",
            "status": "success",
            "error_class": None,
            "retryable": None,
            "latency_ms": result.request_meta["llm_provider_attempts"][0]["latency_ms"],
        }
    ]


def test_groq_key_pool_rotates_starting_key_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Consecutive calls should start from a different pool key, not always keys[0].

    Prevents the first key from absorbing all traffic until it hits a rate limit;
    load spreads across the pool from the very first call.
    """
    from groq_client import reset_groq_key_rotation_counter_for_tests

    reset_groq_key_rotation_counter_for_tests()
    settings = _settings(
        LLM_STRUCTURED_PROVIDER_ALTERNATION="0",
        GROQ_API_KEY="gsk_test",
        GROQ_API_KEYS="gsk_test,gsk_second,gsk_third",
    )
    used_keys: list[str] = []

    def fake_post(url: str, headers: dict[str, str], **_: object) -> _FakeResponse:
        used_keys.append(headers["Authorization"].removeprefix("Bearer "))
        return _groq_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    _call(settings)
    _call(settings)
    _call(settings)
    _call(settings)

    assert used_keys == ["gsk_test", "gsk_second", "gsk_third", "gsk_test"]


def test_groq_key_pool_rotation_still_tries_all_keys_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotating the starting key must not shrink the fallback chain within one call."""
    from groq_client import reset_groq_key_rotation_counter_for_tests

    reset_groq_key_rotation_counter_for_tests()
    settings = _settings(
        LLM_STRUCTURED_PROVIDER_ALTERNATION="0",
        GROQ_API_KEY="gsk_test",
        GROQ_API_KEYS="gsk_test,gsk_second,gsk_third",
    )
    used_keys: list[str] = []

    def fake_post(url: str, headers: dict[str, str], **_: object) -> _FakeResponse:
        key = headers["Authorization"].removeprefix("Bearer ")
        used_keys.append(key)
        if key != "gsk_third":
            return _FakeResponse(429, {"error": {"message": "rate limited"}})
        return _groq_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    result = _call(settings)

    assert result.text == '{"ok": true}'
    assert used_keys == ["gsk_test", "gsk_second", "gsk_third"]


def test_openai_chat_402_credits_uses_groq_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        LLM_PRIMARY_PROVIDER="openai_chat",
        LLM_FALLBACK_PROVIDERS="groq,cerebras",
        LLM_STRUCTURED_PROVIDER_ALTERNATION="0",
        OPENAI_COMPAT_BASE_URL="https://openrouter.ai/api/v1",
        OPENAI_COMPAT_API_KEY="or_test",
        OPENAI_COMPAT_MODEL="openai/gpt-4o-mini",
    )
    calls: list[str] = []

    def fake_post(url: str, **_: object) -> _FakeResponse:
        calls.append(url)
        if "openrouter.ai" in url:
            return _FakeResponse(
                402,
                {"error": {"message": "This request requires more credits, or fewer max_tokens."}},
            )
        return _groq_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    result = _call(settings)

    assert result.text == '{"ok": true}'
    assert len(calls) == 2
    assert "openrouter.ai" in calls[0]
    assert "groq.com" in calls[1]
    assert result.request_meta["llm_selected_provider"] == "groq"
    assert result.request_meta["llm_fallback_used"] is True
    assert result.request_meta["llm_fallback_reason"] == "openai_chat_quota_exhausted"
    assert result.request_meta["llm_provider_attempts"][0]["retryable"] is True


def test_cerebras_404_uses_groq_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate B-shaped chain: cerebras primary returns 404 → groq fallback succeeds."""
    settings = _settings(
        LLM_PRIMARY_PROVIDER="cerebras",
        LLM_FALLBACK_PROVIDERS="groq",
        LLM_STRUCTURED_PROVIDER_ALTERNATION="0",
    )
    calls: list[str] = []

    def fake_post(url: str, **_: object) -> _FakeResponse:
        calls.append(url)
        if "cerebras.ai" in url:
            return _FakeResponse(404, {"error": {"message": "model not found"}})
        return _groq_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    result = _call(settings)

    assert result.text == '{"ok": true}'
    assert len(calls) == 2
    assert "cerebras.ai" in calls[0]
    assert "groq.com" in calls[1]
    assert result.request_meta["llm_selected_provider"] == "groq"
    assert result.request_meta["llm_fallback_used"] is True
    assert result.request_meta["llm_fallback_reason"] == "cerebras_not_found"
    assert result.request_meta["llm_provider_attempts"][0]["error_class"] == "not_found"
    assert result.request_meta["llm_provider_attempts"][0]["retryable"] is True


def test_groq_fallback_does_not_leak_openai_compat_model_when_backend_is_openai_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELIVERY-1 RC-1: production-shaped chain (LLM_BACKEND=openai_chat, primary=openai_chat,
    fallback=groq,cerebras). settings.groq_model is resolved via OPENAI_COMPAT_MODEL in this
    backend mode (config.py:811-817) and must NOT leak into the real Groq HTTP call — Groq
    needs its own, backend-independent native model (GROQ_MODEL), the same way cerebras/nvidia
    already get their own native model fields.
    """
    settings = _settings(
        LLM_BACKEND="openai_chat",
        LLM_PRIMARY_PROVIDER="openai_chat",
        LLM_FALLBACK_PROVIDERS="groq,cerebras",
        LLM_STRUCTURED_PROVIDER_ALTERNATION="0",
        OPENAI_COMPAT_BASE_URL="https://openrouter.ai/api/v1",
        OPENAI_COMPAT_API_KEY="or_test",
        OPENAI_COMPAT_MODEL="openai/gpt-4o-mini",
        GROQ_MODEL="openai/gpt-oss-120b",
        CEREBRAS_API_KEY="",
    )
    seen_models: list[str] = []

    def fake_post(url: str, **kwargs: object) -> _FakeResponse:
        body = kwargs.get("json") or {}
        if isinstance(body, dict) and "model" in body:
            seen_models.append(str(body["model"]))
        if "openrouter.ai" in url:
            return _FakeResponse(402, {"error": {"message": "requires more credits"}})
        if "groq.com" in url:
            return _groq_success()
        raise AssertionError(f"unexpected call to {url}")

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    result = _call(settings)

    assert result.text == '{"ok": true}'
    assert result.request_meta["llm_selected_provider"] == "groq"
    assert seen_models[-1] == "openai/gpt-oss-120b", (
        f"Groq HTTP call used {seen_models[-1]!r} — OPENAI_COMPAT_MODEL leaked into "
        "the groq provider's model resolution instead of using GROQ_MODEL."
    )


def test_cerebras_fallback_uses_native_model_not_groq_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        LLM_PRIMARY_PROVIDER="groq",
        LLM_FALLBACK_PROVIDERS="cerebras",
        LLM_STRUCTURED_PROVIDER_ALTERNATION="0",
        CEREBRAS_MODEL="gpt-oss-120b",
    )
    seen_models: list[str] = []

    def fake_post(url: str, **kwargs: object) -> _FakeResponse:
        body = kwargs.get("json") or {}
        if isinstance(body, dict) and "model" in body:
            seen_models.append(str(body["model"]))
        if "groq.com" in url:
            return _FakeResponse(429, {"error": {"message": "rate limited"}})
        return _cerebras_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    result = request_structured_output(
        settings,
        "Return JSON.",
        '{"input": true}',
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
        model=settings.groq_model,
    )

    assert result.text == '{"ok": true}'
    assert seen_models[-1] == "gpt-oss-120b"
    assert result.request_meta["llm_selected_provider"] == "cerebras"


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (_FakeResponse(429, {"error": {"message": "rate limited"}}), "groq_rate_limit"),
        (_FakeResponse(500, {"error": {"message": "server unavailable"}}), "groq_server_error"),
    ],
)
def test_groq_transient_http_error_uses_cerebras_fallback(
    monkeypatch: pytest.MonkeyPatch,
    response: _FakeResponse,
    reason: str,
) -> None:
    settings = _settings(LLM_STRUCTURED_PROVIDER_ALTERNATION="0")
    calls: list[str] = []

    def fake_post(url: str, **_: object) -> _FakeResponse:
        calls.append(url)
        return response if len(calls) == 1 else _cerebras_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    result = _call(settings)

    assert result.text == '{"ok": true}'
    assert len(calls) == 2
    assert result.request_meta["llm_selected_provider"] == "cerebras"
    assert result.request_meta["llm_fallback_used"] is True
    assert result.request_meta["llm_fallback_reason"] == reason
    assert [a["provider"] for a in result.request_meta["llm_provider_attempts"]] == ["groq", "cerebras"]
    assert result.request_meta["llm_provider_attempts"][0]["retryable"] is True


def test_groq_json_validate_failed_uses_cerebras_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        LLM_STRUCTURED_PROVIDER_ALTERNATION="0",
        GROQ_API_KEY="gsk_a",
        GROQ_API_KEYS="gsk_a,gsk_b",
    )
    calls: list[str] = []

    def fake_post(url: str, **_: object) -> _FakeResponse:
        calls.append(url)
        if "groq.com" in url:
            return _FakeResponse(
                400,
                {
                    "error": {
                        "message": "Failed to generate JSON. See 'failed_generation' for more details.",
                        "code": "json_validate_failed",
                    }
                },
            )
        return _cerebras_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    result = _call(settings)

    assert result.text == '{"ok": true}'
    assert len(calls) == 3
    assert result.request_meta["llm_selected_provider"] == "cerebras"
    assert result.request_meta["llm_fallback_used"] is True
    assert result.request_meta["llm_fallback_reason"] == "groq_json_schema"
    assert [a["provider"] for a in result.request_meta["llm_provider_attempts"]] == [
        "groq",
        "groq",
        "cerebras",
    ]
    assert result.request_meta["llm_provider_attempts"][1]["error_class"] == "json_schema"


def test_groq_timeout_uses_cerebras_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(LLM_STRUCTURED_PROVIDER_ALTERNATION="0")
    calls: list[str] = []

    def fake_post(url: str, **_: object) -> _FakeResponse:
        calls.append(url)
        if len(calls) == 1:
            raise requests.Timeout("slow provider")
        return _cerebras_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    result = _call(settings)

    assert result.request_meta["llm_selected_provider"] == "cerebras"
    assert result.request_meta["llm_fallback_used"] is True
    assert result.request_meta["llm_fallback_reason"] == "groq_timeout"
    assert result.request_meta["llm_provider_attempts"][0]["error_class"] == "timeout"


def test_cerebras_transient_uses_nvidia_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        LLM_FALLBACK_PROVIDERS="cerebras,nvidia",
        LLM_STRUCTURED_PROVIDER_ALTERNATION="0",
        NVIDIA_API_KEY="nvapi_test",
        NVIDIA_BASE_URL="https://integrate.api.nvidia.com/v1",
        NVIDIA_MODEL="meta/llama-3.3-70b-instruct",
    )
    calls: list[str] = []

    def fake_post(url: str, **_: object) -> _FakeResponse:
        calls.append(url)
        if "groq.com" in url:
            return _FakeResponse(503, {"error": {"message": "temporary unavailable"}})
        if "cerebras.ai" in url:
            return _FakeResponse(503, {"error": {"message": "temporary unavailable"}})
        return _nvidia_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    result = _call(settings)

    assert result.text == '{"ok": true}'
    assert len(calls) == 3
    assert result.request_meta["llm_selected_provider"] == "nvidia"
    assert result.request_meta["llm_fallback_used"] is True
    assert [a["provider"] for a in result.request_meta["llm_provider_attempts"]] == [
        "groq",
        "cerebras",
        "nvidia",
    ]


def test_groq_invalid_api_key_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(LLM_STRUCTURED_PROVIDER_ALTERNATION="0")
    calls: list[str] = []

    def fake_post(url: str, **_: object) -> _FakeResponse:
        calls.append(url)
        return _FakeResponse(401, {"error": {"message": "invalid api key", "code": "invalid_api_key"}})

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    with pytest.raises(GroqClientError) as exc:
        _call(settings)

    assert len(calls) == 1
    assert "GROQ_API_KEY was rejected" in str(exc.value)
    assert exc.value.details["llm_provider_attempts"][0]["retryable"] is False


def test_missing_cerebras_key_reports_fallback_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(CEREBRAS_API_KEY="")
    calls: list[str] = []

    def fake_post(url: str, **_: object) -> _FakeResponse:
        calls.append(url)
        return _FakeResponse(429, {"error": {"message": "rate limited"}})

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    with pytest.raises(GroqClientError) as exc:
        _call(settings)

    assert len(calls) == 1
    assert "CEREBRAS_API_KEY" in str(exc.value)
    assert exc.value.details["llm_provider_attempts"][0]["retryable"] is True


def test_successful_invalid_structured_content_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(LLM_STRUCTURED_PROVIDER_ALTERNATION="0")
    calls: list[str] = []

    def fake_post(url: str, **_: object) -> _FakeResponse:
        calls.append(url)
        return _groq_invalid_json_text()

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    result = _call(settings)

    assert result.text == "not-json"
    assert len(calls) == 1
    assert result.request_meta["llm_selected_provider"] == "groq"
    assert result.request_meta["llm_fallback_used"] is False


def test_run_structured_stage_marks_provider_fallback_used(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(LLM_STRUCTURED_PROVIDER_ALTERNATION="0")
    calls: list[str] = []

    def fake_post(url: str, **_: object) -> _FakeResponse:
        calls.append(url)
        return _FakeResponse(503, {"error": {"message": "temporary unavailable"}}) if len(calls) == 1 else _cerebras_success()

    monkeypatch.setattr("groq_client.requests.post", fake_post)

    stage = run_structured_stage(
        settings,
        stage_name="unit_stage",
        instructions="Return JSON.",
        prompt_input={"input": True},
        json_schema=_SCHEMA,
        schema_name="unit_test_schema",
    )

    assert stage["fallback_used"] is True
    assert stage["request_meta"]["llm_selected_provider"] == "cerebras"
    assert stage["request_meta"]["llm_fallback_reason"] == "groq_server_error"


def test_llm_backend_groq_without_new_env_stays_single_provider() -> None:
    with patch("config._load_env_file", return_value=None):
        with patch.dict(os.environ, _env(LLM_FALLBACK_PROVIDERS="", LLM_PRIMARY_PROVIDER=""), clear=True):
            settings = load_settings(require_groq=True, require_google=False)

    assert settings.llm_primary_provider == "groq"
    assert settings.llm_fallback_providers == ()


def test_llm_backend_cerebras_stays_single_provider() -> None:
    with patch("config._load_env_file", return_value=None):
        with patch.dict(
            os.environ,
            _env(
                LLM_BACKEND="cerebras",
                LLM_PRIMARY_PROVIDER="",
                LLM_FALLBACK_PROVIDERS="",
                CEREBRAS_API_KEY="csk_test",
            ),
            clear=True,
        ):
            settings = load_settings(require_groq=True, require_google=False)

    assert settings.llm_backend == "openai_chat"
    assert settings.llm_primary_provider == "cerebras"
    assert settings.llm_fallback_providers == ()


def test_llm_backend_openai_chat_stays_single_provider() -> None:
    with patch("config._load_env_file", return_value=None):
        with patch.dict(
            os.environ,
            _env(
                LLM_BACKEND="openai_chat",
                LLM_PRIMARY_PROVIDER="",
                LLM_FALLBACK_PROVIDERS="",
                OPENAI_COMPAT_BASE_URL="http://127.0.0.1:11434/v1",
                OPENAI_COMPAT_MODEL="llama3.2",
            ),
            clear=True,
        ):
            settings = load_settings(require_groq=True, require_google=False)

    assert settings.llm_backend == "openai_chat"
    assert settings.llm_primary_provider == "openai_chat"
    assert settings.llm_fallback_providers == ()


def test_invalid_primary_provider_is_config_error() -> None:
    with patch("config._load_env_file", return_value=None):
        with patch.dict(os.environ, _env(LLM_PRIMARY_PROVIDER="bad"), clear=True):
            with pytest.raises(ConfigError):
                load_settings(require_groq=True, require_google=False)


def _structured_router_success() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "output_text": '{"ok": true}',
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"ok": true}'}],
                }
            ],
        },
        {"llm_selected_provider": "groq"},
    )


def _three_input_variants() -> list[dict[str, object]]:
    return [
        {"mode": "compact", "input": '{"input": "compact"}'},
        {"mode": "reduced", "input": '{"input": "reduced"}'},
        {"mode": "minimal", "input": '{"input": "minimal"}'},
    ]


def test_input_variant_degrades_on_413_payload_too_large() -> None:
    settings = _settings(LLM_STRUCTURED_PROVIDER_ALTERNATION="0")
    calls: list[str | None] = []

    def fake_router(*_args: object, mode: str | None = None, **_kwargs: object):
        calls.append(mode)
        if len(calls) == 1:
            raise GroqClientError("413 request too large for model context")
        return _structured_router_success()

    with patch("groq_client._post_structured_with_router", side_effect=fake_router):
        result = request_structured_output(
            settings,
            "Return JSON.",
            '{"input": true}',
            json_schema=_SCHEMA,
            schema_name="unit_test_schema",
            input_variants=_three_input_variants(),
        )

    assert result.text == '{"ok": true}'
    assert len(calls) == 2
    degradations = result.request_meta["degradations"]
    assert len(degradations) == 1
    assert degradations[0]["reason"] == "payload_too_large"
    assert degradations[0]["from_mode"] == "compact"
    assert degradations[0]["to_mode"] == "reduced"


def test_input_variant_degrades_on_429_throttle_pressure() -> None:
    settings = _settings(LLM_STRUCTURED_PROVIDER_ALTERNATION="0")
    calls: list[str | None] = []

    def fake_router(*_args: object, mode: str | None = None, **_kwargs: object):
        calls.append(mode)
        if len(calls) == 1:
            raise GroqClientError("429 rate limit exceeded")
        return _structured_router_success()

    with patch("groq_client._post_structured_with_router", side_effect=fake_router):
        result = request_structured_output(
            settings,
            "Return JSON.",
            '{"input": true}',
            json_schema=_SCHEMA,
            schema_name="unit_test_schema",
            input_variants=[
                {"mode": "compact", "input": '{"input": "compact"}'},
                {"mode": "reduced", "input": '{"input": "reduced"}'},
            ],
        )

    assert result.text == '{"ok": true}'
    assert len(calls) == 2
    degradations = result.request_meta["degradations"]
    assert len(degradations) == 1
    assert degradations[0]["reason"] == "throttle_pressure"
    assert degradations[0]["from_mode"] == "compact"
    assert degradations[0]["to_mode"] == "reduced"


def test_single_input_variant_does_not_swallow_413_error() -> None:
    settings = _settings(LLM_STRUCTURED_PROVIDER_ALTERNATION="0")

    def fake_router(*_args: object, **_kwargs: object):
        raise GroqClientError("413 request too large for model context")

    with patch("groq_client._post_structured_with_router", side_effect=fake_router):
        with pytest.raises(GroqClientError, match="413"):
            request_structured_output(
                settings,
                "Return JSON.",
                '{"input": true}',
                json_schema=_SCHEMA,
                schema_name="unit_test_schema",
                input_variants=[{"mode": "compact", "input": '{"input": true}'}],
            )
