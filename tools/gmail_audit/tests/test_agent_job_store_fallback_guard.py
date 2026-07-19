"""Guard: build_agent_job_store must not silently fall back to in-memory."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.jobs import InMemoryAgentJobStore, PostgresAgentJobStore, build_agent_job_store
from agent_runtime.validate import AgentRuntimeConfigError


def test_default_raises_without_database_url() -> None:
    settings = SimpleNamespace(mailbox_memory_database_url="")
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(AgentRuntimeConfigError, match="Postgres agent job store required"):
            build_agent_job_store(settings)


def test_allow_in_memory_returns_in_memory_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    settings = SimpleNamespace(mailbox_memory_database_url="")
    with patch.dict("os.environ", {}, clear=True):
        with caplog.at_level("WARNING"):
            store = build_agent_job_store(settings, allow_in_memory=True)
    assert isinstance(store, InMemoryAgentJobStore)
    assert any("AGENT_JOB_STORE_FALLBACK_TO_MEMORY" in rec.message for rec in caplog.records)


def test_postgres_when_settings_url_present() -> None:
    settings = SimpleNamespace(
        mailbox_memory_database_url="postgresql://user:pass@127.0.0.1:5432/mailbox"
    )
    store = build_agent_job_store(settings)
    assert isinstance(store, PostgresAgentJobStore)
