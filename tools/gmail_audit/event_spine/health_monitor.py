"""Component health monitor — derives live component status from os_events.

Provides read-only status for the System tab in Daszek.
Also evaluates deterministic risk flags from engagement snapshots.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from log_config import get_logger

from case_family_boundary import is_operational_feed_case_row

log = get_logger(__name__)

_KNOWN_COMPONENTS: dict[str, str] = {
    "gmail-agent": "Gmail Agent (Node B)",
    "rag-chat-asystent": "RAG Chat",
    "kalk-top": "Kalk-top",
    "cieplo-orchestrator": "Cieplo Orchestrator",
    "fast-kalk": "Fast-kalk Widget",
    "top-instal-generator": "Generator PDF",
    "daszek": "Daszek (Node A)",
}

# ── P3-4: Deterministic Risk Flags ──────────────────────────────────────


def _detect_risk_stale_engagements(
    mailbox_store: Any | None,
) -> list[dict[str, Any]]:
    """Rule 1: Engagement without status change > 7 days → RISK:STALE."""
    if mailbox_store is None:
        return []
    stale: list[dict[str, Any]] = []
    try:
        fetch_cases = getattr(mailbox_store, "fetch_cases", None)
        if not callable(fetch_cases):
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        for case in fetch_cases(limit=200) or []:
            if not isinstance(case, dict):
                continue
            if not is_operational_feed_case_row(case):
                continue
            updated_str = str(case.get("updated_at") or case.get("last_update_at") or "")
            if not updated_str:
                continue
            try:
                updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                if not updated.tzinfo:
                    updated = updated.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if updated < cutoff:
                stale.append({
                    "risk": "RISK:STALE",
                    "case_id": str(case.get("case_id") or ""),
                    "entity_id": str(case.get("entity_id") or ""),
                    "last_update": updated_str,
                    "days_since_update": round((datetime.now(timezone.utc) - updated).total_seconds() / 86400, 1),
                    "rule": "engagement_no_status_change_7d",
                })
    except Exception:
        log.warning("health_monitor._detect_risk_stale_engagements failed", exc_info=True)
    return stale


def _detect_risk_blocked_hitl(
    engagement_snapshots: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Rule 2: HITL not approved > 3 days → RISK:BLOCKED."""
    if not engagement_snapshots:
        return []
    blocked: list[dict[str, Any]] = []
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)
        for snap in engagement_snapshots:
            if not isinstance(snap, dict):
                continue
            hitl = snap.get("hitl_gate") or {}
            if not isinstance(hitl, dict):
                continue
            if not hitl.get("required"):
                continue
            approved_at_str = str(hitl.get("approved_at") or "")
            if approved_at_str:
                continue  # Already approved
            created_str = str(snap.get("created_at") or snap.get("generated_at") or "")
            if not created_str:
                continue
            try:
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                if not created.tzinfo:
                    created = created.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if created < cutoff:
                blocked.append({
                    "risk": "RISK:BLOCKED",
                    "engagement_id": str(snap.get("engagement_id") or snap.get("snapshot_id") or ""),
                    "case_id": str(snap.get("case_id") or ""),
                    "created_at": created_str,
                    "days_pending": round((datetime.now(timezone.utc) - created).total_seconds() / 86400, 1),
                    "rule": "hitl_not_approved_3d",
                })
    except Exception:
        log.warning("health_monitor._detect_risk_blocked_hitl failed", exc_info=True)
    return blocked


def _detect_risk_overdue_sla(
    engagement_snapshots: list[dict[str, Any]] | None,
    *,
    sla_hours: float = 48.0,
) -> list[dict[str, Any]]:
    """Rule 3: SLA exceeded → RISK:OVERDUE."""
    if not engagement_snapshots:
        return []
    overdue: list[dict[str, Any]] = []
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=sla_hours)
        for snap in engagement_snapshots:
            if not isinstance(snap, dict):
                continue
            op_status = snap.get("operational_status") or {}
            if isinstance(op_status, dict):
                status_code = str(op_status.get("code") or "")
                if status_code in {"completed", "resolved", "cancelled"}:
                    continue
            created_str = str(snap.get("created_at") or snap.get("generated_at") or "")
            if not created_str:
                continue
            try:
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                if not created.tzinfo:
                    created = created.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if created < cutoff:
                overdue.append({
                    "risk": "RISK:OVERDUE",
                    "engagement_id": str(snap.get("engagement_id") or snap.get("snapshot_id") or ""),
                    "case_id": str(snap.get("case_id") or ""),
                    "created_at": created_str,
                    "sla_hours": sla_hours,
                    "hours_elapsed": round((datetime.now(timezone.utc) - created).total_seconds() / 3600, 1),
                    "rule": "sla_exceeded",
                })
    except Exception:
        log.warning("health_monitor._detect_risk_overdue_sla failed", exc_info=True)
    return overdue


def evaluate_deterministic_risk_flags(
    *,
    mailbox_store: Any | None = None,
    engagement_snapshots: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate all 3 deterministic risk rules."""
    flags: list[dict[str, Any]] = []
    flags.extend(_detect_risk_stale_engagements(mailbox_store))
    blocked = _detect_risk_blocked_hitl(engagement_snapshots)
    # Dedup by engagement_id
    seen_eids = {f.get("engagement_id") for f in blocked}
    flags.extend(blocked)
    for od in _detect_risk_overdue_sla(engagement_snapshots):
        eid = od.get("engagement_id") or od.get("case_id") or ""
        if eid not in seen_eids:
            seen_eids.add(eid)
            flags.append(od)
    return flags


def _parse_heartbeat(raw: str | None) -> datetime:
    """Parse isoformat string to datetime; returns datetime.min if None or unparseable."""
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def build_health_status(
    recent_events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    mailbox_store: Any | None = None,
    engagement_snapshots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ts_now = now or datetime.now(timezone.utc)
    components: dict[str, dict[str, Any]] = {}

    for repo_key, display_name in _KNOWN_COMPONENTS.items():
        components[repo_key] = {
            "component": repo_key, "display_name": display_name,
            "status": "unknown", "last_heartbeat": None,
            "last_error": None, "event_count": 0,
        }

    for event in recent_events:
        source = str(event.get("source_repo") or "gmail-agent").strip()
        event_type = str(event.get("event_type") or "")
        occurred_str = str(event.get("occurred_at") or "")
        ev_status = str(event.get("status") or event.get("payload", {}).get("status") or "ok")

        if source not in components:
            components[source] = {
                "component": source, "display_name": source,
                "status": "unknown", "last_heartbeat": None,
                "last_error": None, "event_count": 0,
            }

        comp = components[source]
        comp["event_count"] += 1

        try:
            occurred = datetime.fromisoformat(occurred_str.replace("Z", "+00:00"))
            if not occurred.tzinfo:
                occurred = occurred.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        if not comp["last_heartbeat"] or occurred > _parse_heartbeat(comp["last_heartbeat"]):
            comp["last_heartbeat"] = occurred.isoformat()

        if "error" in event_type.lower() or "fail" in event_type.lower() or ev_status == "error":
            error_msg = str(event.get("payload", {}).get("summary_pl") or event_type)
            if not comp["last_error"]:
                comp["last_error"] = error_msg
                comp["status"] = "error"

    stale_minutes = 30
    for comp in components.values():
        if not comp["last_heartbeat"]:
            continue
        if comp["status"] == "error":
            continue
        try:
            heartbeat = datetime.fromisoformat(str(comp["last_heartbeat"]).replace("Z", "+00:00"))
            if not heartbeat.tzinfo:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            age = (ts_now - heartbeat).total_seconds() / 60
            comp["status"] = "stale" if age > stale_minutes else "ok"
        except (ValueError, TypeError):
            pass

    risk_flags = evaluate_deterministic_risk_flags(
        mailbox_store=mailbox_store,
        engagement_snapshots=engagement_snapshots,
    )

    return {
        "ok": True,
        "components": list(components.values()),
        "stale_threshold_minutes": stale_minutes,
        "generated_at": ts_now.isoformat(),
        "risk_flags": risk_flags,
    }


__all__ = [
    "build_health_status",
    "evaluate_deterministic_risk_flags",
]
