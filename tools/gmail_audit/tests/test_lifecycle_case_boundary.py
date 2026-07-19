from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.lifecycle_monitor import _find_cases_in_state_over_hours
from llm_contracts.case_lifecycle import CaseLifecycleState


def test_lifecycle_monitor_skips_internal_task_rows() -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    store = MagicMock()
    store.fetch_case_ids_in_state_since = None
    store.fetch_cases_by_state = MagicMock(
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

    overdue = _find_cases_in_state_over_hours(
        store,
        CaseLifecycleState.STAGNATING,
        hours=1,
    )

    assert "case-lead" in overdue
    assert "case-internal" not in overdue
