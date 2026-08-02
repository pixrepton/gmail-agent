"""PR-F: primary mode cutover — production Digital Twin, legacy feed deprecated."""

from __future__ import annotations

import os
from typing import Any

from agent_runtime.settings import AgentRuntimeSettings, load_agent_runtime_settings


def agent_runtime_primary_active(settings: AgentRuntimeSettings | None = None) -> bool:
    settings = settings or load_agent_runtime_settings()
    return bool(settings.enabled) and str(settings.mode or "").strip().lower() == "primary"


def legacy_feed_explicitly_requested() -> bool:
    raw = str(os.getenv("DASZEK_FEED_SOURCE", "") or "").strip().lower()
    return raw in {"legacy", "mailbox_memory", "projection_v3"}


def validate_primary_cutover_settings(settings: AgentRuntimeSettings | None = None) -> list[str]:
    """Issues when production primary contract is violated."""
    settings = settings or load_agent_runtime_settings()
    issues: list[str] = []
    if not settings.enabled:
        return issues
    mode = str(settings.mode or "").strip().lower()
    if mode == "legacy":
        issues.append("agent runtime enabled with AGENT_RUNTIME_MODE=legacy is inconsistent (use legacy to disable, or prep|primary)")
    if mode == "primary" and not str(settings.openai_api_key or "").strip():
        issues.append("AGENT_OPENAI_API_KEY required for AGENT_RUNTIME_MODE=primary")
    if mode == "primary" and legacy_feed_explicitly_requested():
        issues.append(
            "DASZEK_FEED_SOURCE=legacy conflicts with primary Digital Twin (use engagement_snapshot_v2 or unset)"
        )
    return issues


def build_primary_cutover_doctor_check(settings: AgentRuntimeSettings | None = None) -> dict[str, Any]:
    settings = settings or load_agent_runtime_settings()
    issues = validate_primary_cutover_settings(settings)
    primary = agent_runtime_primary_active(settings)
    if not settings.enabled:
        status = "skipped"
    elif issues:
        status = "failed"
    elif primary:
        status = "ok"
    else:
        status = "ok" if str(settings.mode or "") == "prep" else "failed"
    return {
        "status": status,
        "enabled": settings.enabled,
        "mode": settings.mode,
        "primary_active": primary,
        "legacy_feed_env": str(os.getenv("DASZEK_FEED_SOURCE", "") or "").strip() or "(auto)",
        "issues": issues,
    }


__all__ = [
    "agent_runtime_primary_active",
    "build_primary_cutover_doctor_check",
    "legacy_feed_explicitly_requested",
    "validate_primary_cutover_settings",
]
