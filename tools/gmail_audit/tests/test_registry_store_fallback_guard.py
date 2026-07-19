"""Guard: build_registry_for_reconcile must not silently fall back to in-memory."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.agent_reconcile import build_registry_for_reconcile
from agent_runtime.engagement_resolver import resolve_engagement_for_case
from agent_runtime.validate import AgentRuntimeConfigError
from correlation_registry.store import InMemoryCorrelationRegistryStore


def test_default_raises_without_database_url() -> None:
    settings = SimpleNamespace(mailbox_memory_database_url="")
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(AgentRuntimeConfigError, match="Postgres correlation registry required"):
            build_registry_for_reconcile(settings)


def test_allow_in_memory_returns_in_memory_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    settings = SimpleNamespace(mailbox_memory_database_url="")
    with patch.dict("os.environ", {}, clear=True):
        with caplog.at_level("WARNING"):
            registry = build_registry_for_reconcile(settings, allow_in_memory=True)
    assert registry is not None
    assert isinstance(registry.store, InMemoryCorrelationRegistryStore)
    assert any(
        "CORRELATION_REGISTRY_STORE_FALLBACK_TO_MEMORY" in rec.message for rec in caplog.records
    )


def test_postgres_when_settings_url_present() -> None:
    settings = SimpleNamespace(
        mailbox_memory_database_url="postgresql://user:pass@127.0.0.1:5432/mailbox"
    )
    with patch(
        "agent_runtime.agent_reconcile.build_correlation_registry_service",
    ) as mock_build:
        mock_build.return_value.bootstrap = lambda: None
        build_registry_for_reconcile(settings)
    mock_build.assert_called_once_with(
        "postgresql://user:pass@127.0.0.1:5432/mailbox",
        in_memory=False,
    )


def test_resolve_engagement_without_registry_or_url_raises() -> None:
    with pytest.raises(RuntimeError, match="correlation registry unavailable"):
        resolve_engagement_for_case("case_no_registry")
