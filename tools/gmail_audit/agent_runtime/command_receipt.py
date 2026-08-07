"""Command receipt for operator_command spine (AI-OS 6.3)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def build_command_receipt(
    *,
    command_id: str,
    signal_id: str,
    status: str,
    engagement_id: str = "",
    proposal_ids: list[str] | None = None,
    hitl_required: bool = False,
    journal_inserted: bool = True,
    error: str = "",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    receipt_status = str(status or "failed").strip().lower()
    if hitl_required and receipt_status == "completed":
        receipt_status = "hitl_required"
    return {
        "receipt_kind": "operator_command",
        "command_id": str(command_id or ""),
        "signal_id": str(signal_id or ""),
        "status": receipt_status,
        "engagement_id": str(engagement_id or ""),
        "proposal_ids": list(proposal_ids or []),
        "hitl_required": bool(hitl_required),
        "journal_inserted": bool(journal_inserted),
        "error": str(error or ""),
        "warnings": list(warnings or []),
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


__all__ = ["build_command_receipt"]
