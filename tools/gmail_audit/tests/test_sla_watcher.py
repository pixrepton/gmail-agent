"""Tests for sla_watcher.py - SLA violation checks and oneshot."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))


class TestCheckSlaViolations:
    """check_sla_violations returns dict with correct keys."""
    # fetch_decision_queue is imported via "from divergence_loop import fetch_decision_queue"
    # inside check_sla_violations, so we patch divergence_loop.fetch_decision_queue.

    def test_returns_expected_keys(self):
        from sla_watcher import check_sla_violations

        mock_conn = MagicMock()

        fake_queue = [
            {"priority": "critical", "proposal_id": "p1"},
            {"priority": "high", "proposal_id": "p2"},
            {"priority": "low", "proposal_id": "p3"},
        ]

        with patch("divergence_loop.fetch_decision_queue", return_value=fake_queue):
            result = check_sla_violations(mock_conn)

        assert isinstance(result, dict)
        assert "critical" in result
        assert "high" in result
        assert "total_pending" in result
        assert "checked_at" in result

    def test_total_pending_matches_queue_length(self):
        from sla_watcher import check_sla_violations

        mock_conn = MagicMock()
        fake_queue = [{"priority": "low"}] * 5

        with patch("divergence_loop.fetch_decision_queue", return_value=fake_queue):
            result = check_sla_violations(mock_conn)

        assert result["total_pending"] == 5

    def test_critical_and_high_partitioned_correctly(self):
        from sla_watcher import check_sla_violations

        mock_conn = MagicMock()
        fake_queue = [
            {"priority": "critical", "id": 1},
            {"priority": "high", "id": 2},
            {"priority": "critical", "id": 3},
            {"priority": "low", "id": 4},
            {"priority": "high", "id": 5},
        ]

        with patch("divergence_loop.fetch_decision_queue", return_value=fake_queue):
            result = check_sla_violations(mock_conn)

        assert len(result["critical"]) == 2
        assert len(result["high"]) == 2

    def test_checked_at_is_isoformat_string(self):
        from sla_watcher import check_sla_violations

        mock_conn = MagicMock()

        with patch("divergence_loop.fetch_decision_queue", return_value=[]):
            result = check_sla_violations(mock_conn)

        checked_at = result["checked_at"]
        assert isinstance(checked_at, str)
        assert "T" in checked_at

    def test_empty_queue_returns_zero_pending(self):
        from sla_watcher import check_sla_violations

        mock_conn = MagicMock()

        with patch("divergence_loop.fetch_decision_queue", return_value=[]):
            result = check_sla_violations(mock_conn)

        assert result["total_pending"] == 0
        assert result["critical"] == []
        assert result["high"] == []


class TestSlaWatcherOneshot:
    """sla_watcher_oneshot returns error when no db_url configured."""

    def test_no_db_url_returns_error(self):
        from sla_watcher import sla_watcher_oneshot

        settings = MagicMock()
        settings.mailbox_memory_database_url = ""

        result = sla_watcher_oneshot(settings)

        assert isinstance(result, dict)
        assert result["ok"] is False
        assert "error" in result
        assert "Database not configured" in result["error"]

    def test_missing_attribute_returns_error(self):
        from sla_watcher import sla_watcher_oneshot

        settings = MagicMock(spec=[])
        result = sla_watcher_oneshot(settings)

        assert result["ok"] is False
        assert "Database not configured" in result["error"]
