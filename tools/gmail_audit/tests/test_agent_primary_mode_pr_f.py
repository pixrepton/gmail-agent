from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.agent_reconcile import legacy_downstream_reconcile_active
from agent_runtime.digital_twin_dod import build_digital_twin_doctor_check
from agent_runtime.manifest import build_agent_runtime_manifest_slice
from agent_runtime.primary_cutover import (
    agent_runtime_primary_active,
    validate_primary_cutover_settings,
)
from agent_runtime.settings import load_agent_runtime_settings
from agent_runtime.validate import AgentRuntimeConfigError
from config import Settings, canonical_production_violations
from daszek_engagement_feed import engagement_feed_source_enabled
import importlib.util

_canonical_spec = importlib.util.spec_from_file_location(
    "test_canonical_runtime_profile",
    Path(__file__).resolve().parent / "test_canonical_runtime_profile.py",
)
_canonical_mod = importlib.util.module_from_spec(_canonical_spec)
assert _canonical_spec.loader is not None
_canonical_spec.loader.exec_module(_canonical_mod)
_minimal_canonical_settings = _canonical_mod._minimal_canonical_settings


def test_primary_mode_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "primary")
    monkeypatch.setenv("AGENT_OPENAI_API_KEY", "sk-test")
    assert agent_runtime_primary_active()
    assert engagement_feed_source_enabled()


def test_primary_rejects_legacy_feed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "primary")
    monkeypatch.setenv("AGENT_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DASZEK_FEED_SOURCE", "legacy")
    issues = validate_primary_cutover_settings(load_agent_runtime_settings())
    assert any("legacy" in i.lower() for i in issues)
    assert not engagement_feed_source_enabled()


def test_canonical_production_requires_primary_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "prep")
    monkeypatch.setenv("AGENT_OPENAI_API_KEY", "sk-test")
    s: Settings = _minimal_canonical_settings()
    viol = canonical_production_violations(s)
    assert any("primary" in v.lower() for v in viol)


def test_legacy_mode_with_agent_enabled_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "legacy")
    monkeypatch.setenv("AGENT_OPENAI_API_KEY", "sk-test")
    with pytest.raises(AgentRuntimeConfigError):
        legacy_downstream_reconcile_active()


def test_manifest_slice_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "primary")
    monkeypatch.setenv("AGENT_OPENAI_API_KEY", "sk-test")
    settings = type("S", (), {"daszek_v2_push_enabled": False})()
    slice_ = build_agent_runtime_manifest_slice(settings)
    assert slice_["primary_active"] is True
    assert slice_["daszek_feed_source"] == "engagement_snapshot_v2"
    assert slice_["daszek_legacy_v2_push_allowed"] is False


def test_digital_twin_doctor_primary_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "primary")
    monkeypatch.setenv("AGENT_OPENAI_API_KEY", "sk-test")
    rep = build_digital_twin_doctor_check()
    assert rep["status"] == "ok"
    assert rep["primary_active"] is True


def test_canonical_production_ok_with_primary_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "primary")
    monkeypatch.setenv("AGENT_OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("DASZEK_FEED_SOURCE", raising=False)
    s: Settings = _minimal_canonical_settings()
    viol = canonical_production_violations(s)
    agent_viol = [v for v in viol if "AGENT_" in v or "DASZEK_FEED" in v]
    assert agent_viol == []
