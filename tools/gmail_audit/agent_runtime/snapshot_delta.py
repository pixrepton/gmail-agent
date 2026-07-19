"""Apply tool snapshot_delta onto EngagementSnapshot.v2 (Python-owned writes)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def apply_snapshot_delta(
    snapshot: EngagementSnapshotV2,
    delta: dict[str, Any],
) -> EngagementSnapshotV2:
    if not delta:
        return snapshot
    payload = snapshot.model_dump(mode="python")
    merged = _deep_merge(payload, delta)
    merged["version"] = snapshot.version
    merged["engagement_id"] = snapshot.engagement_id
    if "case_id" in delta:
        merged["case_id"] = str(delta.get("case_id") or "")
    else:
        merged["case_id"] = snapshot.case_id
    return EngagementSnapshotV2.model_validate(merged)


def decrement_steps(snapshot: EngagementSnapshotV2) -> EngagementSnapshotV2:
    remaining = max(0, int(snapshot.operational_status.steps_remaining) - 1)
    return snapshot.model_copy(
        update={
            "operational_status": snapshot.operational_status.model_copy(
                update={"steps_remaining": remaining}
            )
        }
    )
