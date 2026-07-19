"""Adaptive policy: when projection composer uses LLM vs deterministic envelope."""

from __future__ import annotations

from typing import Any

from central_llm_stage import anthropic_configured
from config import Settings
from context_quality_contract import normalize_context_quality

PROJECTION_COMPOSER_MODE_ADAPTIVE = "adaptive"
PROJECTION_COMPOSER_MODE_DETERMINISTIC = "deterministic"
PROJECTION_COMPOSER_MODE_LLM = "llm"

PROJECTION_COMPOSER_MODES = frozenset(
    {
        PROJECTION_COMPOSER_MODE_ADAPTIVE,
        PROJECTION_COMPOSER_MODE_DETERMINISTIC,
        PROJECTION_COMPOSER_MODE_LLM,
    }
)

_SKIP_LANES = frozenset({"skip"})
_LIGHT_LANES = frozenset({"reference_only", "review_direct"})
_PLACEHOLDER_ESSENCE = frozenset(
    {
        "",
        "no case summary available.",
        "no case summary available",
    }
)


def projection_llm_available(settings: Settings | None) -> bool:
    if settings is None:
        return False
    if anthropic_configured(settings):
        return True
    if str(getattr(settings, "groq_api_key", "") or "").strip():
        return True
    if str(getattr(settings, "openai_compat_api_key", "") or "").strip() and str(
        getattr(settings, "openai_compat_base_url", "") or ""
    ).strip():
        return True
    if str(getattr(settings, "cerebras_api_key", "") or "").strip():
        return True
    if str(getattr(settings, "nvidia_api_key", "") or "").strip():
        return True
    return False


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


def _essence_text(context_tray_set: dict[str, Any]) -> str:
    for row in _list_of_dicts(context_tray_set.get("essence_tray")):
        for key in ("summary", "summary_text", "content_pl", "headline_co_pl"):
            text = str(row.get(key) or "").strip()
            if text:
                return text
    return ""


def _context_quality_from_stage_outputs(stage_outputs: dict[str, Any] | None) -> dict[str, Any]:
    stage_outputs = stage_outputs if isinstance(stage_outputs, dict) else {}
    mb = stage_outputs.get("mailbox_memory_result")
    if not isinstance(mb, dict):
        return {}
    pack = mb.get("context_pack") if isinstance(mb.get("context_pack"), dict) else {}
    for source in (pack.get("vnext"), pack):
        if not isinstance(source, dict):
            continue
        cq = source.get("context_quality")
        if isinstance(cq, dict):
            return normalize_context_quality(cq, embedded=True)
    return {}


def _projection_signals(
    context_tray_set: dict[str, Any],
    *,
    stage_outputs: dict[str, Any] | None,
) -> dict[str, Any]:
    stage_outputs = stage_outputs if isinstance(stage_outputs, dict) else {}
    preclass = stage_outputs.get("preclassification_result")
    preclass = preclass if isinstance(preclass, dict) else {}
    lane = str(preclass.get("lane") or "intake_llm").strip().lower()
    gaps_tray = _list_of_dicts(context_tray_set.get("gaps_tray"))
    conflicts_tray = _list_of_dicts(context_tray_set.get("conflicts_tray"))
    cq = _context_quality_from_stage_outputs(stage_outputs)
    essence = _essence_text(context_tray_set)
    essence_placeholder = essence.strip().lower() in _PLACEHOLDER_ESSENCE
    return {
        "lane": lane,
        "gap_tray_count": len(gaps_tray),
        "conflict_tray_count": len(conflicts_tray),
        "context_quality_gap_count": int(cq.get("gap_count") or 0),
        "context_quality_conflict_count": int(cq.get("conflict_count") or 0),
        "has_blocking_gaps": bool(cq.get("has_blocking_gaps")),
        "has_blocking_conflicts": bool(cq.get("has_blocking_conflicts")),
        "weak_evidence_count": int(cq.get("weak_evidence_count") or 0),
        "essence_placeholder": essence_placeholder,
        "essence_len": len(essence.strip()),
    }


def resolve_projection_composer_mode(
    context_tray_set: dict[str, Any],
    *,
    stage_outputs: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """
    Return composer routing: mode ``deterministic`` | ``llm``, reason, and signals.

    Default policy (``projection_composer_mode=adaptive``): use LLM only when trays/context
    need richer operator-facing composition; otherwise deterministic envelope.
    """

    trays = context_tray_set if isinstance(context_tray_set, dict) else {}
    signals = _projection_signals(trays, stage_outputs=stage_outputs)
    policy_mode = str(
        getattr(settings, "projection_composer_mode", PROJECTION_COMPOSER_MODE_ADAPTIVE) or PROJECTION_COMPOSER_MODE_ADAPTIVE
    ).strip().lower()
    if policy_mode not in PROJECTION_COMPOSER_MODES:
        policy_mode = PROJECTION_COMPOSER_MODE_ADAPTIVE

    llm_ok = projection_llm_available(settings)

    def _decision(mode: str, reason: str) -> dict[str, Any]:
        effective = mode
        if mode == PROJECTION_COMPOSER_MODE_LLM and not llm_ok:
            effective = PROJECTION_COMPOSER_MODE_DETERMINISTIC
            reason = f"{reason}_llm_unavailable"
        return {
            "mode": effective,
            "policy_mode": policy_mode,
            "decision_reason": reason,
            "live_llm": effective == PROJECTION_COMPOSER_MODE_LLM,
            "llm_available": llm_ok,
            "signals": signals,
        }

    if policy_mode == PROJECTION_COMPOSER_MODE_DETERMINISTIC:
        return _decision(PROJECTION_COMPOSER_MODE_DETERMINISTIC, "policy_forced_deterministic")

    if policy_mode == PROJECTION_COMPOSER_MODE_LLM:
        return _decision(PROJECTION_COMPOSER_MODE_LLM, "policy_forced_llm")

    lane = signals["lane"]
    if lane in _SKIP_LANES:
        return _decision(PROJECTION_COMPOSER_MODE_DETERMINISTIC, "lane_skip")

    gap_n = max(signals["gap_tray_count"], signals["context_quality_gap_count"])
    conflict_n = max(signals["conflict_tray_count"], signals["context_quality_conflict_count"])
    blocking = signals["has_blocking_gaps"] or signals["has_blocking_conflicts"]
    weak_evidence = signals["weak_evidence_count"] > 0

    if lane in _LIGHT_LANES:
        if blocking or gap_n or conflict_n or weak_evidence or signals["essence_placeholder"]:
            return _decision(PROJECTION_COMPOSER_MODE_LLM, "lane_light_needs_enrichment")
        return _decision(PROJECTION_COMPOSER_MODE_DETERMINISTIC, "lane_light_trays_sufficient")

    if signals["essence_placeholder"]:
        return _decision(PROJECTION_COMPOSER_MODE_LLM, "essence_missing")

    if blocking or gap_n or conflict_n:
        return _decision(PROJECTION_COMPOSER_MODE_LLM, "gaps_or_conflicts_present")

    if weak_evidence:
        return _decision(PROJECTION_COMPOSER_MODE_LLM, "weak_evidence")

    return _decision(PROJECTION_COMPOSER_MODE_DETERMINISTIC, "trays_sufficient")


__all__ = [
    "PROJECTION_COMPOSER_MODE_ADAPTIVE",
    "PROJECTION_COMPOSER_MODE_DETERMINISTIC",
    "PROJECTION_COMPOSER_MODE_LLM",
    "PROJECTION_COMPOSER_MODES",
    "projection_llm_available",
    "resolve_projection_composer_mode",
]
