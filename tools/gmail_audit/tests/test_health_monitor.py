from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from event_spine.health_monitor import (
    _detect_risk_blocked_hitl,
    _detect_risk_stale_engagements,
    build_health_status,
)


def test_detect_risk_stale_engagements_skips_internal_task() -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    store = MagicMock()
    store.fetch_cases = MagicMock(
        return_value=[
            {
                "case_id": "case-internal",
                "case_family": "internal_task",
                "updated_at": old,
            },
            {
                "case_id": "case-lead",
                "case_family": "lead_opportunity",
                "updated_at": old,
            },
        ]
    )
    flags = _detect_risk_stale_engagements(store)
    case_ids = {f["case_id"] for f in flags}
    assert "case-lead" in case_ids
    assert "case-internal" not in case_ids


def test_detect_risk_blocked_hitl_flags_old_pending() -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    snapshots = [
        {
            "engagement_id": "eng_1",
            "case_id": "case_1",
            "created_at": old,
            "hitl_gate": {"required": True, "approved_at": ""},
        }
    ]
    flags = _detect_risk_blocked_hitl(snapshots)
    assert len(flags) == 1
    assert flags[0]["risk"] == "RISK:BLOCKED"


def test_build_health_status_includes_risk_flags() -> None:
    events = [
        {
            "event_type": "service_heartbeat",
            "source_repo": "gmail-agent",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload": {"status": "ok"},
        }
    ]
    store = MagicMock()
    store.fetch_cases = MagicMock(return_value=[])
    out = build_health_status(events, mailbox_store=store, engagement_snapshots=[])
    assert out.get("ok") is True
    assert "risk_flags" in out
    assert isinstance(out["risk_flags"], list)
