"""Regression tests: Gate A must stay hermetic w.r.t. accidental KALK_TOP developer env."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.settings import AgentRuntimeSettings, load_agent_runtime_settings
from agent_runtime.validate import AgentRuntimeConfigError, assert_agent_run_ready, validate_agent_runtime_settings


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


def test_unrelated_unit_settings_have_no_kalk_top_url() -> None:
    settings = load_agent_runtime_settings()
    assert settings.kalk_top_base_url == ""
    assert settings.kalk_top_agent_key == ""


def test_url_without_key_still_fail_closed() -> None:
    issues = validate_agent_runtime_settings(_settings(kalk_top_base_url="http://127.0.0.1:8091"))
    assert any("KALK_TOP_AGENT_KEY" in item for item in issues)
    with pytest.raises(AgentRuntimeConfigError):
        assert_agent_run_ready(_settings(kalk_top_base_url="http://127.0.0.1:8091"))


@pytest.mark.kalk_top_config
def test_url_with_test_key_passes_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KALK_TOP_BASE_URL", "http://127.0.0.1:8091")
    monkeypatch.setenv("KALK_TOP_AGENT_KEY", "test-agent-key")
    issues = validate_agent_runtime_settings(
        _settings(kalk_top_base_url="http://127.0.0.1:8091", kalk_top_agent_key="test-agent-key")
    )
    assert not any("KALK_TOP_AGENT_KEY recommended" in item for item in issues)


def test_no_url_keeps_kalk_tool_unconfigured() -> None:
    issues = validate_agent_runtime_settings(_settings())
    assert not any("KALK_TOP_AGENT_KEY recommended" in item for item in issues)
