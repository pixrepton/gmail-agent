"""Backfill correlation registry links from mailbox_memory_cases."""

from __future__ import annotations

import json
from typing import Any

from agent_runtime.materialize import _register_email_identity, _register_engagement_link


def _row_metadata(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("metadata")
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, str) and meta.strip():
        try:
            parsed = json.loads(meta)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def backfill_identities(
    *,
    correlation_store: Any,
    mailbox_store: Any | None = None,
    dry_run: bool = False,
    limit: int = 5000,
) -> dict[str, Any]:
    """Register mailbox_case + email identity links for cases missing registry entries."""
    if correlation_store is None:
        return {"ok": False, "error": "correlation_store required", "identities_count": 0}

    fetch_cases = getattr(mailbox_store, "fetch_cases", None) if mailbox_store is not None else None
    if not callable(fetch_cases):
        return {"ok": False, "error": "mailbox_store.fetch_cases required", "identities_count": 0}

    rows = fetch_cases(limit=max(1, int(limit))) or []
    linked = 0
    skipped = 0
    conflicts = 0

    for row in rows:
        if not isinstance(row, dict):
            continue
        case_id = str(row.get("case_id") or "").strip()
        if not case_id:
            continue
        meta = _row_metadata(row)
        engagement_id = str(
            meta.get("staging_engagement_id")
            or meta.get("engagement_id")
            or ""
        ).strip()
        email = str(row.get("customer_email") or meta.get("customer_email") or "").strip()
        if not engagement_id and not email:
            skipped += 1
            continue

        if dry_run:
            linked += 1
            continue

        try:
            if engagement_id:
                _register_engagement_link(
                    correlation_store,
                    engagement_id=engagement_id,
                    case_id=case_id,
                )
            if email:
                _register_email_identity(
                    correlation_store,
                    email=email,
                    case_id=case_id,
                    customer_name=str(row.get("customer_name") or meta.get("customer_name") or ""),
                )
            linked += 1
        except Exception:
            conflicts += 1

    return {
        "ok": True,
        "identities_count": linked,
        "skipped": skipped,
        "conflicts": conflicts,
        "scanned": len(rows),
        "dry_run": dry_run,
    }


__all__ = ["backfill_identities"]
