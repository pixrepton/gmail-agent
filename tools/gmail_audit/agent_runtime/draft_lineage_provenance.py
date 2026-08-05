"""Additive draft lineage provenance persisted on EngagementSnapshotV2."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

DraftOrigin = Literal["brain1", "brain2_fallback", "legacy_unknown"]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_draft_lineage_provenance(
    *,
    draft_origin: DraftOrigin,
    origin_correlation_id: str = "",
    origin_producer: str = "",
    origin_created_at: str | None = None,
) -> dict[str, Any]:
    producer = str(origin_producer or "").strip()
    if not producer:
        producer = "reply_drafter" if draft_origin == "brain1" else "generate_draft_reply"
    return {
        "draft_origin": draft_origin,
        "origin_correlation_id": str(origin_correlation_id or "").strip(),
        "origin_producer": producer,
        "origin_created_at": str(origin_created_at or _utc_now_iso()),
    }


def draft_origin_from_transport(transport: dict[str, Any] | None) -> DraftOrigin:
    if not isinstance(transport, dict):
        return "legacy_unknown"
    source = str(transport.get("source") or "").strip()
    if source == "brain1":
        return "brain1"
    if source == "brain2_fallback":
        return "brain2_fallback"
    return "legacy_unknown"


__all__ = [
    "DraftOrigin",
    "build_draft_lineage_provenance",
    "draft_origin_from_transport",
]
