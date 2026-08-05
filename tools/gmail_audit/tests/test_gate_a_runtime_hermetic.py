"""Gate A hermeticism: explicit env mapping, not pytest detection in production modules."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.settings import _load_agent_runtime_env_file, load_agent_runtime_settings
from central_llm_stage import _cache_read, _llm_cache_disabled


def test_runtime_settings_without_dotenv_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default Gate A: no developer .env hydration (GMAIL_AUDIT_SKIP_AGENT_DOTENV=1 via conftest)."""
    monkeypatch.setenv("GMAIL_AUDIT_SKIP_AGENT_DOTENV", "1")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from agent_runtime import settings as settings_mod

    settings_mod._DOTENV_LOADED = False
    assert _load_agent_runtime_env_file() is None
    settings = load_agent_runtime_settings()
    assert settings.kalk_top_base_url == ""
    assert settings.kalk_top_agent_key == ""


@pytest.mark.agent_runtime
def test_explicit_dotenv_opt_in_can_load_when_skip_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in: clearing SKIP allows dotenv path when no provider keys are already set."""
    monkeypatch.delenv("GMAIL_AUDIT_SKIP_AGENT_DOTENV", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    from agent_runtime import settings as settings_mod

    settings_mod._DOTENV_LOADED = False
    # No .env file required for this assertion — loader must not be blocked by pytest heuristics.
    _load_agent_runtime_env_file()
    assert settings_mod._DOTENV_LOADED is True


def test_llm_cache_disabled_by_default_under_gate_a(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_AUDIT_DISABLE_LLM_CACHE", "1")
    assert _llm_cache_disabled() is True
    assert _cache_read("nonexistent-key", "test_stage") is None


@pytest.mark.llm_cache
def test_llm_cache_opt_in_allows_read_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """@llm_cache clears DISABLE flag; read still returns None without DB (no live hit required)."""
    monkeypatch.delenv("GMAIL_AUDIT_DISABLE_LLM_CACHE", raising=False)
    assert _llm_cache_disabled() is False
    with patch("central_llm_stage._get_cache_db_url", return_value=""):
        assert _cache_read("hermetic-cache-key", "test_stage") is None


@pytest.mark.live_llm_env
def test_provider_keys_survive_live_llm_env_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "sk-hermetic-test-key")
    monkeypatch.delenv("GMAIL_AUDIT_SKIP_AGENT_DOTENV", raising=False)
    from agent_runtime.settings import build_agent_planner_endpoints

    settings = load_agent_runtime_settings()
    endpoints = build_agent_planner_endpoints(settings)
    labels = [endpoint.label for endpoint in endpoints]
    assert any(label.startswith("groq") for label in labels)


_PROBE = """
import json, os, sys
sys.path.insert(0, {tool_dir!r})
for name in ("GMAIL_AUDIT_SKIP_AGENT_DOTENV", "GMAIL_AUDIT_DISABLE_LLM_CACHE", "PYTEST_CURRENT_TEST"):
    os.environ.pop(name, None)
os.environ["KALK_TOP_BASE_URL"] = "http://127.0.0.1:8091"
os.environ["KALK_TOP_AGENT_KEY"] = "subprocess-test-key"
from agent_runtime.settings import load_agent_runtime_settings
s = load_agent_runtime_settings()
print(json.dumps({{"kalk_top_base_url": s.kalk_top_base_url, "kalk_top_agent_key_set": bool(s.kalk_top_agent_key)}}))
"""


def test_production_subprocess_without_hermetic_flags_keeps_kalk_top() -> None:
    out = subprocess.run(
        [sys.executable, "-c", _PROBE.format(tool_dir=str(TOOL_DIR))],
        capture_output=True,
        text=True,
        cwd=str(TOOL_DIR),
        check=True,
    )
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload["kalk_top_base_url"] == "http://127.0.0.1:8091"
    assert payload["kalk_top_agent_key_set"] is True
