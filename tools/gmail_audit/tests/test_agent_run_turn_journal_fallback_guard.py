"""Guard: build_turn_journal / execute_agent_run must not silently fall back to in-memory."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.planner import MockSequencePlanner
from agent_runtime.run import build_turn_journal, execute_agent_run
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.store import InMemoryOperatorEngagementStore
from agent_runtime.turn_journal import InMemoryAgentTurnJournal, PostgresAgentTurnJournal
from agent_runtime.validate import AgentRuntimeConfigError


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


def test_build_turn_journal_raises_when_enabled_without_database_url() -> None:
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(AgentRuntimeConfigError, match="Postgres agent turn journal required"):
            build_turn_journal(_settings(enabled=True, mailbox_database_url=""))


def test_build_turn_journal_returns_none_when_disabled_without_url() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert build_turn_journal(_settings(enabled=False, mailbox_database_url="")) is None


def test_build_turn_journal_allow_in_memory_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    with patch.dict("os.environ", {}, clear=True):
        with caplog.at_level("WARNING"):
            journal = build_turn_journal(
                _settings(enabled=True, mailbox_database_url=""),
                allow_in_memory=True,
            )
    assert isinstance(journal, InMemoryAgentTurnJournal)
    assert any("AGENT_RUN_TURN_JOURNAL_STORE_FALLBACK_TO_MEMORY" in rec.message for rec in caplog.records)


def test_build_turn_journal_postgres_when_url_present() -> None:
    journal = build_turn_journal(
        _settings(mailbox_database_url="postgresql://user:pass@127.0.0.1:5432/mailbox")
    )
    assert isinstance(journal, PostgresAgentTurnJournal)


def test_execute_agent_run_raises_without_journal_when_enabled() -> None:
    store = InMemoryOperatorEngagementStore()
    store.init_snapshot_from_signal(
        signal={"signal_id": "sig_guard"},
        case_id="case_guard",
        engagement_id="eng_guard",
    )
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(AgentRuntimeConfigError, match="Postgres agent turn journal required"):
            execute_agent_run(
                "eng_guard",
                store=store,
                settings=_settings(enabled=True, mailbox_database_url=""),
                planner=MockSequencePlanner(["report_gaps_and_stop"]),
                require_enabled=False,
            )
