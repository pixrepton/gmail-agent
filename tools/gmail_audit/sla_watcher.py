"""SLA Watcher — background monitor for pending decision escalations.

Checks divergence_loop fetch_decision_queue() periodically and logs SLA violations
as os_events. Runs as FastAPI BackgroundTask (every 15 min) or CLI --oneshot.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from _protocols import DatabaseConnection

from log_config import get_logger

logger = get_logger(__name__)


def check_sla_violations(conn: DatabaseConnection) -> dict[str, Any]:
    """Fetch pending decisions, check SLA, return violations."""
    from divergence_loop import fetch_decision_queue

    queue = fetch_decision_queue(conn, limit=200)
    critical = [d for d in queue if d.get("priority") == "critical"]
    high = [d for d in queue if d.get("priority") == "high"]

    now = datetime.now(timezone.utc).isoformat()
    return {
        "critical": critical,
        "high": high,
        "total_pending": len(queue),
        "checked_at": now,
    }


def maybe_escalate(settings: Any, conn: DatabaseConnection) -> list[dict[str, Any]]:
    """Check SLA violations and escalate via os_events. Returns new escalations."""
    from event_spine.query import fetch_recent_os_events

    violations = check_sla_violations(conn)
    escalated: list[dict[str, Any]] = []
    db_url = str(getattr(settings, "mailbox_memory_database_url", "") or "")

    # Get recently escalated (last 60 min) to avoid duplicates
    recently_escalated: set[str] = set()
    if db_url:
        recent_events = fetch_recent_os_events(db_url, limit=100)
        for ev in recent_events:
            if isinstance(ev, dict) and str(ev.get("event_type", "")).startswith("sla.violation"):
                recently_escalated.add(str(ev.get("case_id", "")) or str(ev.get("proposal_id", "")))

    for item in violations.get("critical", []):
        eid = str(item.get("proposal_id", "") or item.get("case_id", ""))
        if eid and eid not in recently_escalated:
            _emit_sla_escalation(settings, item, severity="critical", db_url=db_url)
            escalated.append(item)
            logger.warning("SLA CRITICAL: proposal %s waiting >24h", eid)

    for item in violations.get("high", []):
        eid = str(item.get("proposal_id", "") or item.get("case_id", ""))
        if eid and eid not in recently_escalated:
            _emit_sla_escalation(settings, item, severity="high", db_url=db_url)
            escalated.append(item)
            logger.info("SLA HIGH: proposal %s waiting >4h", eid)

    return escalated


def _emit_sla_escalation(settings: Any, item: dict[str, Any], severity: str, db_url: str = "") -> None:
    """Write SLA escalation as os_event."""
    try:
        from event_spine.emitter import publish_os_event
        from datetime import datetime, timezone

        publish_os_event(
            db_url=db_url,
            event_type=f"sla.violation.{severity}",
            source="sla_watcher",
            payload={
                "proposal_id": str(item.get("proposal_id", "")),
                "case_id": str(item.get("case_id", "")),
                "engagement_id": str(item.get("engagement_id", "")),
                "hours_waiting": item.get("hours_waiting", 0),
                "severity": severity,
                "summary_pl": item.get("summary_pl", ""),
            },
        )
    except Exception as exc:
        logger.warning("Failed to emit SLA escalation: %s", exc)


def sla_watcher_oneshot(settings: Any) -> dict[str, Any]:
    """Run SLA watcher once — for CLI --oneshot."""
    import psycopg

    db_url = str(getattr(settings, "mailbox_memory_database_url", "") or "")
    if not db_url:
        return {"ok": False, "error": "Database not configured."}

    conn = psycopg.connect(db_url)
    violations = check_sla_violations(conn)
    escalated = maybe_escalate(settings, conn)
    conn.close()

    return {
        "ok": True,
        "violations": violations,
        "escalated": len(escalated),
    }
