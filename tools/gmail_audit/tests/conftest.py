"""Pytest session hooks for AI-OS Phase 3 runtime proof manifest + Gate A hermeticism."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from aios_bounded_runtime_support import (
    begin_proof_manifest,
    finalize_proof_manifest,
    get_active_manifest,
    record_pytest_session_result,
    runtime_proof_required,
    set_active_manifest,
)

_AGENT_RUNTIME_ENV = (
    "AGENT_RUNTIME_MODE",
    "AGENT_RUNTIME_ENABLED",
)

# Developer .env pools leak into planner-chain assertions (groq_2/3/4, extra openrouter, …).
# Tests that need keys set them explicitly via monkeypatch after this fixture runs.
_HERMETIC_PROVIDER_ENV = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEYS",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_REASONING_EFFORT",
    "DEEPSEEK_THINKING_ENABLED",
    "CEREBRAS_API_KEY",
    "CEREBRAS_API_KEYS",
    "cerebras_api_key",
    "AGENT_CEREBRAS_API_KEY",
    "NVIDIA_API_KEY",
    "NVIDIA_API_KEYS",
    "nvidia_api_key",
    "AGENT_NVIDIA_API_KEY",
    "GROQ_API_KEY",
    "GROQ_API_KEYS",
    "GROQ_API_VL",
    "AGENT_GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENROUTER_API_KEYS",
    "AGENT_OPENAI_API_KEY",
    "OPENAI_COMPAT_API_KEY",
    "AGENT_OPENAI_NATIVE_API_KEY",
    "OPENAI_API_KEY",
    "CURSOR_API_KEY",
    "AGENT_CURSOR_API_KEY",
    # Case OS / feed plane — must not stick after agent/ingress tests.
    "DASZEK_FEED_SOURCE",
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "playwright: bounded Playwright runtime proof")
    config.addinivalue_line("markers", "kalk_top_config: opt into real KALK_TOP env for this test")
    config.addinivalue_line(
        "markers",
        "agent_runtime: opt into developer AGENT_RUNTIME_* env for this test",
    )
    config.addinivalue_line(
        "markers",
        "live_llm_env: keep developer LLM provider keys from process/.env for this test",
    )
    config.addinivalue_line(
        "markers",
        "llm_cache: allow Postgres LLM response cache under pytest for this test",
    )


@pytest.fixture(scope="session", autouse=True)
def _aios_phase3_runtime_manifest_session():
    if not runtime_proof_required():
        yield None
        return
    manifest = begin_proof_manifest()
    set_active_manifest(manifest)
    yield manifest


@pytest.fixture(autouse=True)
def _kalk_top_test_opt_in(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if request.node.get_closest_marker("kalk_top_config"):
        monkeypatch.setenv("GMAIL_AUDIT_KALK_TOP_TEST_OPT_IN", "1")
    else:
        monkeypatch.delenv("GMAIL_AUDIT_KALK_TOP_TEST_OPT_IN", raising=False)
        monkeypatch.delenv("KALK_TOP_BASE_URL", raising=False)
        monkeypatch.delenv("KALK_TOP_AGENT_KEY", raising=False)
        monkeypatch.delenv("TOPINSTAL_CALC_AGENT_API_KEY", raising=False)
    yield


@pytest.fixture(autouse=True)
def _hermetic_agent_runtime_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Unit tests must not inherit developer AGENT_RUNTIME_MODE=prep from .env / Case OS plane.

    Case OS profile `full` calls ``setdefault(AGENT_RUNTIME_MODE, prep)``. A bare
    ``delenv`` is therefore insufficient — an explicit ``legacy`` value blocks setdefault.
    """
    if request.node.get_closest_marker("agent_runtime"):
        yield
        return
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "legacy")
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "0")
    yield


@pytest.fixture(autouse=True)
def _hermetic_llm_provider_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Strip developer LLM provider keys so planner/eval chain tests stay deterministic."""
    if request.node.get_closest_marker("live_llm_env"):
        yield
        return
    # Block agent_runtime.settings._load_agent_runtime_env_file from re-hydrating .env mid-test.
    monkeypatch.setenv("GMAIL_AUDIT_SKIP_AGENT_DOTENV", "1")
    if request.node.get_closest_marker("llm_cache"):
        monkeypatch.delenv("GMAIL_AUDIT_DISABLE_LLM_CACHE", raising=False)
    else:
        monkeypatch.setenv("GMAIL_AUDIT_DISABLE_LLM_CACHE", "1")
    for name in _HERMETIC_PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, exitstatus: int, config: pytest.Config) -> None:
    if not runtime_proof_required():
        return
    manifest = get_active_manifest()
    if manifest is None:
        return
    stats = terminalreporter.stats
    record_pytest_session_result(
        manifest,
        passed=len(stats.get("passed", [])),
        skipped=len(stats.get("skipped", [])),
        failed=len(stats.get("failed", [])),
    )
    finalize_proof_manifest(manifest)
    set_active_manifest(None)
