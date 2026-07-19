"""Run manifest hooks for agent runtime (PR-F)."""

from __future__ import annotations

from typing import Any

from agent_runtime.primary_cutover import agent_runtime_primary_active
from agent_runtime.settings import load_agent_runtime_settings


def build_agent_runtime_manifest_slice(settings: Any | None = None) -> dict[str, Any]:
    """Serializable agent/feed flags for run manifest and CEL proof packs."""
    from daszek_engagement_feed import engagement_feed_source_enabled

    agent = load_agent_runtime_settings()
    engagement_feed = engagement_feed_source_enabled(settings)
    feed_source = "engagement_snapshot_v2" if engagement_feed else "legacy_projection_v3"
    legacy_v2_push = bool(getattr(settings, "daszek_v2_push_enabled", False)) and feed_source != "engagement_snapshot_v2"
    return {
        "enabled": bool(agent.enabled),
        "mode": str(agent.mode or "prep"),
        "primary_active": agent_runtime_primary_active(agent),
        "daszek_feed_source": feed_source,
        "daszek_legacy_v2_push_allowed": legacy_v2_push,
        "engagement_feed_auto": engagement_feed,
    }


def attach_agent_runtime_manifest(run_state: dict[str, Any], settings: Any | None = None) -> None:
    manifest = run_state.setdefault("manifest", {})
    if not isinstance(manifest, dict):
        manifest = {}
        run_state["manifest"] = manifest
    manifest["agent_runtime"] = build_agent_runtime_manifest_slice(settings)


__all__ = ["attach_agent_runtime_manifest", "build_agent_runtime_manifest_slice"]
