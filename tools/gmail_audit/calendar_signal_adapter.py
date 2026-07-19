"""Calendar-specific normalization into RawObservation and CanonicalSignal."""

from __future__ import annotations

from typing import Any

from raw_observation_contract import RawObservation, build_raw_observation
from signal_contract import CanonicalSignal, build_canonical_signal


def build_calendar_raw_observation(
    *,
    source_ref: dict[str, Any],
    observed_at: str,
    payload: dict[str, Any],
    created_by_runtime: str = "calendar_runtime",
) -> RawObservation:
    return build_raw_observation(
        observation_kind="calendar_event_observed",
        source_kind="google_calendar",
        source_ref=source_ref,
        observed_at=observed_at,
        occurred_at=str(payload.get("start_at") or payload.get("created") or observed_at),
        payload=payload,
        source_marker=str(source_ref.get("calendar_event_id") or ""),
        created_by_runtime=created_by_runtime,
    )


def build_calendar_signal(
    *,
    source_ref: dict[str, Any],
    observed_at: str,
    payload: dict[str, Any],
    raw_observation: RawObservation | None = None,
    created_by_runtime: str = "calendar_runtime",
) -> CanonicalSignal:
    observation = raw_observation or build_calendar_raw_observation(
        source_ref=source_ref,
        observed_at=observed_at,
        payload=payload,
        created_by_runtime=created_by_runtime,
    )
    case_id = str(payload.get("case_id") or "")
    summary = str(payload.get("summary") or "")
    return build_canonical_signal(
        signal_kind="calendar_event_observed",
        source_kind="google_calendar",
        source_ref=observation.source_ref,
        observed_at=observed_at,
        effective_at=str(payload.get("start_at") or observed_at),
        case_key_hint=case_id,
        thread_key_hint=case_id,
        business_lane="operations",
        signal_summary_pl=f"Kalendarz: {summary}".strip(),
        payload=payload,
        artifacts={"source": "calendar_runtime", "raw_observation_id": observation.observation_id},
        revision_marker=str(source_ref.get("calendar_event_id") or ""),
        created_by_runtime=created_by_runtime,
    )


__all__ = ["build_calendar_raw_observation", "build_calendar_signal"]
