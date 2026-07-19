"""Resolve engagement_id for a mailbox case (PR-A) + staging (RFC E2)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from correlation_registry.service import CorrelationRegistryService, build_correlation_registry_service


@dataclass(frozen=True)
class EngagementResolution:
    engagement_id: str
    case_id: str
    created: bool = False
    staging: bool = False


def new_staging_engagement_id(signal_id: str = "") -> str:
    suffix = str(signal_id or uuid.uuid4().hex)[:12]
    return f"stg_{suffix}"


def resolve_staging_engagement(
    signal: dict[str, Any],
    *,
    signal_id: str = "",
) -> EngagementResolution:
    """Staging engagement without case_id — TUM deep_understand path."""
    eid = new_staging_engagement_id(str(signal_id or signal.get("signal_id") or ""))
    return EngagementResolution(engagement_id=eid, case_id="", created=True, staging=True)


def resolve_engagement_for_case(
    case_id: str,
    *,
    registry: CorrelationRegistryService | None = None,
    database_url: str = "",
    customer_email: str = "",
    message_id: str = "",
) -> EngagementResolution:
    """
    Return engagement_id for case_id via correlation_registry mailbox_case link.
    Creates identity+engagement+link when missing and email provided.
    """
    cid = str(case_id or "").strip()
    if not cid:
        raise ValueError("case_id is required")
    svc = registry
    if svc is None:
        db_url = str(database_url or "").strip()
        if db_url:
            svc = build_correlation_registry_service(db_url, in_memory=False)
        else:
            raise RuntimeError("correlation registry unavailable (set MAILBOX_MEMORY_DATABASE_URL)")
    if svc is None:
        raise RuntimeError("correlation registry unavailable (set MAILBOX_MEMORY_DATABASE_URL)")
    existing = svc.lookup_by_case_id(cid)
    if existing and str(existing.get("engagement_id") or "").strip():
        return EngagementResolution(
            engagement_id=str(existing["engagement_id"]),
            case_id=cid,
            created=False,
        )
    email = str(customer_email or "").strip()
    if not email:
        raise ValueError(
            f"No engagement linked to case_id={cid!r}; provide customer_email to create one"
        )
    result = svc.sync_mailbox_case(
        case_id=cid,
        customer_email=email,
        message_id=str(message_id or ""),
    )
    engagement_id = str(result.get("engagement_id") or "").strip()
    if not engagement_id:
        raise RuntimeError(f"sync_mailbox_case did not return engagement_id for {cid!r}")
    return EngagementResolution(engagement_id=engagement_id, case_id=cid, created=True)


def extract_case_id_from_signal(signal: dict[str, Any]) -> str:
    for key in ("case_id", "case_key_hint"):
        value = str(signal.get(key) or "").strip()
        if value.startswith("case_"):
            return value
    payload = signal.get("payload_json") if isinstance(signal.get("payload_json"), dict) else {}
    for key in ("case_id", "mailbox_case_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""
