"""DeepSeek priority-1 structured-stage integration (DEEPSEEK-MIGRATION-1).

DeepSeek must become the first attempt in every structured LLM chain, ahead of whatever was
first before (Anthropic override, else the groq/cerebras/nvidia router chain), without changing
that pre-existing chain's own internal order or semantics. See docs/core/LLM_PROVIDER_MAP.md.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from central_llm_stage import primary_llm_provider, run_central_structured_stage
from config import Settings, load_settings
from context_assembler import AssembledContext
from groq_client import GroqClientError, deepseek_configured
from llm_contracts.signal_extraction import SignalExtractionResult


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = ""
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, object]:
        return self._payload


def _chat_success(content: str) -> _FakeResponse:
    return _FakeResponse(200, {"choices": [{"message": {"content": content}}]})


def _minimal_settings(**overrides: object) -> Settings:
    base = {
        "llm_backend": "groq",
        "openai_compat_base_url": "",
        "openai_compat_api_key": "",
        "groq_api_key": "gsk_test",
        "google_access_token": "",
        "google_client_id": "",
        "google_client_secret": "",
        "google_refresh_token": "",
        "google_token_endpoint": "https://oauth2.googleapis.com/token",
        "google_oauth_scopes": ("https://www.googleapis.com/auth/gmail.readonly",),
        "groq_model": "openai/gpt-oss-120b",
        "groq_native_model": "openai/gpt-oss-120b",
        "groq_base_url": "https://api.groq.com",
        "daszek_base_url": "",
        "daszek_login": "",
        "daszek_password": "",
        "daszek_v2_push_enabled": False,
        "case_guidance_enabled": False,
        "case_guidance_model": "openai/gpt-oss-120b",
        "case_guidance_remote_state_enabled": True,
        "anthropic_api_key": "",
        "anthropic_model": "claude-sonnet-4-20250514",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "GROQ_API_KEY": "gsk_test",
        "GROQ_MODEL": "openai/gpt-oss-120b",
        "HTTP_TIMEOUT": "5",
        "HTTP_MAX_RETRIES": "1",
        "HTTP_RETRY_BASE_DELAY": "2",
        "LLM_STRUCTURED_PROVIDER_ALTERNATION": "0",
    }
    env.update(overrides)
    return env


def _settings_from_env(**env_overrides: str) -> Settings:
    with patch("config._load_env_file", return_value=None):
        with patch.dict(os.environ, _env(**env_overrides), clear=True):
            return load_settings(require_groq=True, require_google=False)


# ── Config wiring ─────────────────────────────────────────────────────────


def test_deepseek_not_configured_by_default() -> None:
    settings = _settings_from_env()
    assert deepseek_configured(settings) is False
    assert settings.deepseek_model == "deepseek-v4-flash"
    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.deepseek_thinking_enabled is True
    assert settings.deepseek_reasoning_effort == "high"


def test_deepseek_configured_when_key_set() -> None:
    settings = _settings_from_env(DEEPSEEK_API_KEY="ds_test")
    assert deepseek_configured(settings) is True


def test_primary_llm_provider_deepseek_outranks_anthropic() -> None:
    settings = _minimal_settings(anthropic_api_key="anthropic_test", deepseek_api_key="ds_test")
    assert primary_llm_provider(settings) == "deepseek"


def test_primary_llm_provider_falls_back_to_anthropic_without_deepseek() -> None:
    settings = _minimal_settings(anthropic_api_key="anthropic_test")
    assert primary_llm_provider(settings) == "anthropic"


# ── Structured-stage tier-0 routing ──────────────────────────────────────


def test_deepseek_used_first_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_from_env(DEEPSEEK_API_KEY="ds_test")
    groq_json = json.dumps({"hvac_intent": "quote", "raw_geographic_signal": "Jaworzno"})
    calls: list[str] = []

    def fake_post(url: str, **_kwargs: object) -> _FakeResponse:
        calls.append(url)
        return _chat_success(groq_json)

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
            query_text="pompa ciepla",
            json_schema=SignalExtractionResult.model_json_schema(),
            schema_name="signal_extraction_v1",
            output_model=SignalExtractionResult,
        )

    assert len(calls) == 1
    assert "deepseek.com" in calls[0]
    assert out is not None
    assert out["central_llm_provider"] == "deepseek"
    assert out["parse_status"] == "pydantic_validated"


def test_deepseek_thinking_payload_sent_on_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_from_env(DEEPSEEK_API_KEY="ds_test")
    bodies: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> _FakeResponse:
        bodies.append(kwargs.get("json") or {})
        return _chat_success(json.dumps({"ok": True}))

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    with patch("central_llm_stage.build_context_assembler") as mock_asm:
        mock_asm.return_value.assemble.return_value = AssembledContext(
            company_context="ctx", assembled_at="2026-07-18T00:00:00+00:00"
        )
        run_central_structured_stage(
            settings,
            stage_name="business_reasoning",
            task_instructions="reason",
            prompt_input={"message": "test"},
            query_text="pompa",
            json_schema={"type": "object"},
            schema_name="business_reasoning_v1",
        )

    assert len(bodies) == 1
    assert bodies[0]["model"] == "deepseek-v4-flash"
    assert bodies[0]["thinking"] == {"type": "enabled"}
    assert bodies[0]["reasoning_effort"] == "high"


def test_deepseek_structured_stage_sends_json_mode_and_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_from_env(DEEPSEEK_API_KEY="ds_test")
    bodies: list[dict[str, object]] = []
    payload = json.dumps({"hvac_intent": "install", "raw_geographic_signal": "Katowice"})

    def fake_post(url: str, **kwargs: object) -> _FakeResponse:
        bodies.append(kwargs.get("json") or {})
        return _chat_success(payload)

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    schema = SignalExtractionResult.model_json_schema()
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
            json_schema=schema,
            schema_name="signal_extraction_v1",
            output_model=SignalExtractionResult,
        )

    assert out is not None
    assert len(bodies) == 1
    assert bodies[0]["response_format"] == {"type": "json_object"}
    system_message = (bodies[0]["messages"] or [])[0]["content"]  # type: ignore[index]
    assert "JSON Schema contract for signal_extraction_v1" in system_message
    assert '"hvac_intent"' in system_message


def test_deepseek_failure_falls_back_to_previous_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepSeek priority-1 failing must not crash the whole path — falls to the previously
    first provider (here: the router chain, since Anthropic is unconfigured)."""
    settings = _settings_from_env(DEEPSEEK_API_KEY="ds_test")
    groq_json = json.dumps({"ok": True})
    calls: list[str] = []

    def fake_post(url: str, **_kwargs: object) -> _FakeResponse:
        calls.append(url)
        if "deepseek.com" in url:
            return _FakeResponse(500, {"error": {"message": "server error"}})
        return _chat_success(groq_json) if "groq" not in url else _FakeResponse(
            200,
            {
                "output_text": groq_json,
                "output": [{"type": "message", "content": [{"type": "output_text", "text": groq_json}]}],
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
            json_schema={"type": "object"},
            schema_name="signal_extraction_v1",
        )

    assert len(calls) == 2
    assert "deepseek.com" in calls[0]
    assert "groq.com" in calls[1]
    assert out is not None
    assert out["central_llm_provider"] == "groq"


def test_deepseek_not_configured_uses_previous_chain_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_from_env()  # no DEEPSEEK_API_KEY
    assert deepseek_configured(settings) is False
    groq_json = json.dumps({"ok": True})
    calls: list[str] = []

    def fake_post(url: str, **_kwargs: object) -> _FakeResponse:
        calls.append(url)
        return _FakeResponse(
            200,
            {
                "output_text": groq_json,
                "output": [{"type": "message", "content": [{"type": "output_text", "text": groq_json}]}],
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
            json_schema={"type": "object"},
            schema_name="signal_extraction_v1",
        )

    assert len(calls) == 1
    assert "groq.com" in calls[0]
    assert out is not None
    assert out["central_llm_provider"] == "groq"


def test_deepseek_still_first_ahead_of_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both DeepSeek and Anthropic are configured, DeepSeek is tried first and Anthropic
    is never invoked on DeepSeek success."""
    settings = _minimal_settings(anthropic_api_key="anthropic_test", deepseek_api_key="ds_test")
    payload = json.dumps({"hvac_intent": "install", "raw_geographic_signal": "Katowice"})
    calls: list[str] = []

    def fake_post(url: str, **_kwargs: object) -> _FakeResponse:
        calls.append(url)
        return _chat_success(payload)

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    with patch("central_llm_stage.build_context_assembler") as mock_asm:
        mock_asm.return_value.assemble.return_value = AssembledContext(
            company_context="ctx", assembled_at="2026-07-18T00:00:00+00:00"
        )
        with patch("central_llm_stage._call_anthropic_raw_text") as mock_anthropic:
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

    mock_anthropic.assert_not_called()
    assert out is not None
    assert out["central_llm_provider"] == "deepseek"
    assert out["response_json"]["hvac_intent"] == "install"


def test_deepseek_retryable_failure_falls_back_to_anthropic_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _minimal_settings(anthropic_api_key="anthropic_test", deepseek_api_key="ds_test")
    payload = {"hvac_intent": "install", "raw_geographic_signal": "Katowice"}

    def fake_post(url: str, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse(500, {"error": {"message": "server error"}})

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    with patch("central_llm_stage.build_context_assembler") as mock_asm:
        mock_asm.return_value.assemble.return_value = AssembledContext(
            company_context="ctx", assembled_at="2026-07-18T00:00:00+00:00"
        )
        with patch("central_llm_stage._call_anthropic_raw_text", return_value=json.dumps(payload)) as mock_anthropic:
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

    mock_anthropic.assert_called_once()
    assert out is not None
    assert out["central_llm_provider"] == "anthropic"
    assert out["response_json"]["hvac_intent"] == "install"


def test_deepseek_permanent_request_error_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepSeek adapter/request contract errors must not be silently masked by Groq."""
    settings = _settings_from_env(DEEPSEEK_API_KEY="ds_bad_key")

    def fake_post(url: str, **_kwargs: object) -> _FakeResponse:
        if "deepseek.com" in url:
            return _FakeResponse(400, {"error": {"message": "Thinking mode does not support this tool_choice", "code": "invalid_request_error"}})
        raise AssertionError("fallback provider must not be called")

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    with patch("central_llm_stage.build_context_assembler") as mock_asm:
        mock_asm.return_value.assemble.return_value = AssembledContext(
            company_context="ctx", assembled_at="2026-07-18T00:00:00+00:00"
        )
        with pytest.raises(GroqClientError):
            run_central_structured_stage(
                settings,
                stage_name="signal_extraction",
                task_instructions="extract",
                prompt_input={"message": "test"},
                query_text="pompa",
                json_schema={"type": "object"},
                schema_name="signal_extraction_v1",
            )


def test_deepseek_auth_config_failure_falls_back_to_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepSeek auth/config failure should be visible degradation but not stop old structured chain."""
    settings = _settings_from_env(DEEPSEEK_API_KEY="ds_expired_key", ANTHROPIC_API_KEY="ask_test")
    payload = {
        "hvac_intent": "install",
        "raw_geographic_signal": "Radlin",
        "sender": {"email": "client@example.com", "name": "Client"},
        "topic": "Pompa ciepla",
    }

    def fake_post(url: str, **_kwargs: object) -> _FakeResponse:
        if "deepseek.com" in url:
            return _FakeResponse(401, {"error": {"message": "invalid api key"}})
        raise AssertionError("unexpected provider")

    monkeypatch.setattr("groq_client.requests.post", fake_post)
    with patch("central_llm_stage.build_context_assembler") as mock_asm:
        mock_asm.return_value.assemble.return_value = AssembledContext(
            company_context="ctx", assembled_at="2026-07-18T00:00:00+00:00"
        )
        with patch("central_llm_stage._call_anthropic_raw_text", return_value=json.dumps(payload)) as mock_anthropic:
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

    mock_anthropic.assert_called_once()
    assert out is not None
    assert out["central_llm_provider"] == "anthropic"
