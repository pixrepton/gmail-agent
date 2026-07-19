"""Shared case_family boundary between customer cases and internal tasks.

fetch_cases() audit (Phase 1):
  - daszek_v3_operational_feed.build_operational_feed_from_mailbox_store — filtered
    (Phase 3: desk_eligible on operational pool before feed.cases/desk selection)
  - calendar_runtime.CalendarRuntime.ingest_events — filtered
  - entity_linker.EntityLinker.link — filtered (customer identity link)
  - drive_case_linker.link_drive_candidate — filtered
  - gmail_intake cohort context-pack path — filtered
  - event_spine/health_monitor._detect_risk_stale_engagements — filtered
  - agent_runtime/lifecycle_monitor._find_cases_in_state_over_hours — filtered (dict rows)
  - agent_runtime/business_pulse — SQL ACTIVE_CUSTOMER_CASES_SQL_WHERE
  - agent_runtime/business_pulse.get_agent_health — connectivity probe only (limit=1)
  - mailbox_memory store protocol — unfiltered (callers apply boundary)
"""

from __future__ import annotations

from typing import Any

INTERNAL_TASK_CASE_FAMILY = "internal_task"

# SQL fragment for mailbox_memory_cases queries (Business Pulse, etc.)
ACTIVE_CUSTOMER_CASES_SQL_WHERE = "case_family != 'internal_task'"


def case_family_value(row: dict) -> str:
    return str(row.get("case_family") or "").strip()


def is_internal_task_row(row: dict) -> bool:
    return case_family_value(row) == INTERNAL_TASK_CASE_FAMILY


def is_operational_feed_case_row(row: dict) -> bool:
    """Rows eligible for feed.cases / feed.desk (excludes internal_task legacy family)."""
    if is_internal_task_row(row):
        return False
    return True


def filter_operational_feed_case_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Customer-case rows for linking, desk projection, and cohort reads."""
    return [row for row in rows if isinstance(row, dict) and is_operational_feed_case_row(row)]
