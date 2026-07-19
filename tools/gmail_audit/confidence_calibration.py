"""Threshold calibration from Daszek feedback history (server-computed profile).

Operator quality tuning belongs to structured calibration events (`feedback_event_contract.FeedbackEvent`),
not adjudication — see `feedback_event_contract` and `operator_feedback_runtime`.
"""

from __future__ import annotations

from typing import Any

from confidence_review import DEFAULT_THRESHOLDS


def merge_threshold_overrides(
    base: dict[str, float] | None,
    calibration_profile: dict[str, Any] | None,
) -> dict[str, float]:
    """Apply server-provided per-domain deltas to DEFAULT_THRESHOLDS or a base map."""
    out = dict(base or DEFAULT_THRESHOLDS)
    if not calibration_profile or not isinstance(calibration_profile, dict):
        return out
    deltas = calibration_profile.get("domain_threshold_deltas") or {}
    if not isinstance(deltas, dict):
        return out
    for key, delta in deltas.items():
        if key not in out:
            continue
        try:
            out[key] = max(0.05, min(0.98, float(out[key]) + float(delta)))
        except (TypeError, ValueError):
            continue
    return out


def calibration_meta(calibration_profile: dict[str, Any] | None) -> dict[str, Any]:
    if not calibration_profile or not isinstance(calibration_profile, dict):
        return {}
    return {
        "quality_ratio": calibration_profile.get("quality_ratio"),
        "feedback_sample_size": calibration_profile.get("feedback_sample_size"),
        "source": calibration_profile.get("source"),
    }
