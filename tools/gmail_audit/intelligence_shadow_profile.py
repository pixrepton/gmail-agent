"""Shadow signal + intelligence flags: projection-only second layer (PR-5)."""

from __future__ import annotations

import os
from typing import Any


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def apply_intelligence_shadow_profile(
    stage_config: dict[str, Any],
    *,
    settings: Any | None = None,
    signal_runtime_mode: str = "legacy",
) -> bool:
    """
    When INTELLIGENCE_SHADOW_PROJECTION=1 and signal mode is shadow|active,
    enable understanding + decision pipeline + v2 proposals for projection only.
    """
    mode = str(signal_runtime_mode or "legacy").strip().lower()
    if mode not in {"shadow", "active"}:
        return False
    if not _env_bool("INTELLIGENCE_SHADOW_PROJECTION", False):
        if settings is None or not bool(getattr(settings, "intelligence_shadow_projection", False)):
            return False

    stage_config["understanding_output_enabled"] = True
    stage_config["decision_pipeline_enabled"] = True
    stage_config["decision_pipeline_dry_run_only"] = True
    stage_config["action_proposal_v2_enabled"] = True
    stage_config["intelligence_shadow_profile_applied"] = True
    return True


__all__ = ["apply_intelligence_shadow_profile"]
