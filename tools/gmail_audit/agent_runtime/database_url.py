"""Shared mailbox-memory database URL resolution for agent runtime factories."""

from __future__ import annotations

from typing import Any

from agent_runtime.settings import AgentRuntimeSettings, load_agent_runtime_settings


def resolve_mailbox_memory_database_url(settings: Any) -> tuple[str, str]:
    settings_url = str(getattr(settings, "mailbox_memory_database_url", "") or "").strip()
    if settings_url:
        return settings_url, "settings.mailbox_memory_database_url"
    agent_url = str(load_agent_runtime_settings().mailbox_database_url or "").strip()
    if agent_url:
        return agent_url, "agent_runtime.mailbox_database_url"
    return "", "none"


def resolve_agent_runtime_database_url(settings: AgentRuntimeSettings) -> tuple[str, str]:
    url = str(getattr(settings, "mailbox_database_url", "") or "").strip()
    if url:
        return url, "agent_runtime.mailbox_database_url"
    agent_url = str(load_agent_runtime_settings().mailbox_database_url or "").strip()
    if agent_url:
        return agent_url, "agent_runtime.mailbox_database_url_env"
    return "", "none"
