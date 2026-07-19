from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

TOOL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = TOOL_DIR / "scripts"
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

_spec = importlib.util.spec_from_file_location(
    "backfill_correlation_registry_cli",
    SCRIPTS_DIR / "run_backfill_correlation_registry.py",
)
assert _spec and _spec.loader
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


def test_cron_delta_mode_queries_recent_cases_only() -> None:
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.fetchall.side_effect = [
        [{"case_id": "c1", "customer_email": "a@b.c", "thread_id": "t1", "message_id": "m1"}],
        [],
    ]

    cases = backfill._fetch_delta_cases(mock_conn, 24)
    assert len(cases) == 1
    sql = mock_cursor.execute.call_args_list[0][0][0]
    assert "hours" in sql
    assert "mailbox_memory_cases" in sql


def test_dry_run_stats_count_planned_links() -> None:
    from correlation_registry.preview import empty_dry_run_stats, plan_mailbox_case_sync, accumulate_plan
    from correlation_registry.store import InMemoryCorrelationRegistryStore

    store = InMemoryCorrelationRegistryStore()
    stats = empty_dry_run_stats()
    plan = plan_mailbox_case_sync(
        store,
        case_id="case-dry-1",
        customer_email="dry@test.pl",
    )
    accumulate_plan(stats, plan)
    assert stats["identities_would_create"] == 1
    assert stats["engagements_would_create"] == 1
    assert stats["links_would_create"] >= 2


def test_main_cron_dry_run_exits_zero() -> None:
    mock_service = MagicMock()
    mock_conn = MagicMock()
    with patch.object(sys, "argv", ["backfill", "--cron", "--delta-hours", "24", "--dry-run"]):
        with patch.object(backfill, "load_settings") as mock_settings:
            mock_settings.return_value = MagicMock(mailbox_memory_database_url="postgresql://local/test")
            with patch.object(backfill, "build_correlation_registry_service", return_value=mock_service):
                with patch("psycopg.connect", return_value=mock_conn):
                    with patch.object(backfill, "_fetch_delta_cases", return_value=[]):
                        with patch.object(backfill, "_fetch_delta_links", return_value=[]):
                            code = backfill.main()
    assert code == 0


def test_register_links_payload_idempotent_under_cron_resync() -> None:
    from correlation_registry.service import CorrelationRegistryService
    from correlation_registry.store import InMemoryCorrelationRegistryStore

    svc = CorrelationRegistryService(InMemoryCorrelationRegistryStore())
    payload = {
        "identity_email": "cron@test.pl",
        "links": [{"link_type": "mailbox_case", "target_id": "case-cron-1", "source_repo": "gmail-agent"}],
    }
    first = svc.register_links_payload(payload)
    second = svc.register_links_payload(payload)
    assert first["engagement_id"] == second["engagement_id"]
