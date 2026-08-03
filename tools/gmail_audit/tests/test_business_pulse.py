"""Tests for Business Pulse tools."""
from __future__ import annotations

import unittest
from types import SimpleNamespace


class FakeCursor:
    def __init__(self, rows=None):
        self._rows = rows or []

    def execute(self, query, *args):
        return self

    def fetchone(self):
        if self._rows:
            return self._rows[0]
        return None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeStore:
    def __init__(self):
        self._rows = [(5,), ("active", 3), ("won", 2), ("lost", 1)]

    def _connect(self):
        conn = SimpleNamespace()
        conn.cursor = lambda: FakeCursor(self._rows)
        return conn

    def fetch_cases(self, limit=50):
        return [{"case_id": "c1", "customer_name": "Test"}, {"case_id": "c2", "customer_name": "Test2"}]


class FakeSettings:
    mailbox_memory_database_url = ""
    daszek_operational_feed_push_base_url = ""
    daszek_operational_feed_case_limit = 50


class TestBusinessPulse(unittest.TestCase):
    def setUp(self):
        from agent_runtime.business_pulse import _bpcache_clear
        _bpcache_clear()
        self.store = FakeStore()
        self.settings = FakeSettings()

    def test_pipeline_summary_structure(self):
        from agent_runtime.business_pulse import get_pipeline_summary
        result = get_pipeline_summary(self.store, self.settings)
        self.assertIn("ok", result)

    def test_client_health_structure(self):
        from agent_runtime.business_pulse import get_client_health
        result = get_client_health(self.store, self.settings)
        self.assertIn("ok", result)

    def test_daily_delta_structure(self):
        from agent_runtime.business_pulse import get_daily_delta
        result = get_daily_delta(self.store, self.settings)
        self.assertIn("ok", result)

    def test_win_rate_structure(self):
        from agent_runtime.business_pulse import get_win_rate
        result = get_win_rate(self.store, self.settings)
        self.assertIn("ok", result)

    def test_top_clients_structure(self):
        from agent_runtime.business_pulse import get_top_clients
        result = get_top_clients(self.store, self.settings)
        self.assertIn("ok", result)

    def test_revenue_forecast_structure(self):
        from agent_runtime.business_pulse import get_revenue_forecast
        result = get_revenue_forecast(self.store, self.settings)
        self.assertIn("ok", result)


class TestRedactForLogging(unittest.TestCase):
    def test_redact_email_key(self):
        from agent_runtime.business_pulse import _redact_for_logging
        data = {"customer_email": "test@example.com", "name": "Jan"}
        result = _redact_for_logging(data)
        self.assertEqual(result["customer_email"], "[REDACTED]")
        self.assertEqual(result["name"], "Jan")

    def test_redact_nested(self):
        from agent_runtime.business_pulse import _redact_for_logging
        data = {"items": [{"email": "a@b.com"}, {"email": "b@c.com"}]}
        result = _redact_for_logging(data)
        for item in result["items"]:
            self.assertEqual(item["email"], "[REDACTED]")


# ── Placeholder monetary fields must be honest, not fabricated zeros ───────
#
# No table persists a per-case monetary value today (mailbox_memory_cases has
# no pricing column; pricing/OfferDTO is kalk-top's SoT, not gmail-agent's).
# Returning 0 for "total_value_pln" or "1" for "active_offers" looks like a
# real computed answer and can mislead an operator or the chat agent into
# treating "no pipeline value" as a real business signal. These fields must
# be explicitly untracked (None) with a "value_tracking": "not_implemented"
# marker instead of a fabricated number.


class _QueueCursor:
    """Returns one canned (fetchone, fetchall) pair per execute() call, in order."""

    def __init__(self, steps):
        self._steps = list(steps)
        self._current = ({}, [])

    def execute(self, query, *args):
        if self._steps:
            self._current = self._steps.pop(0)
        return self

    def fetchone(self):
        return self._current[0]

    def fetchall(self):
        return self._current[1]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _QueueStore:
    def __init__(self, steps):
        self._steps = steps

    def _connect(self):
        conn = SimpleNamespace()
        conn.cursor = lambda: _QueueCursor(self._steps)
        return conn


class TestBusinessPulseHonestPlaceholders(unittest.TestCase):
    def setUp(self):
        from agent_runtime.business_pulse import _bpcache_clear
        _bpcache_clear()
        self.settings = FakeSettings()

    def test_pipeline_summary_total_value_is_not_a_fabricated_zero(self):
        from agent_runtime.business_pulse import get_pipeline_summary
        store = _QueueStore(
            [
                ((5,), [(5,)]),  # total count
                ({}, [("active", 3)]),  # by_status
                ({}, [("case1", "Jan Kowalski", "active", "2026-01-01T00:00:00Z", "active")]),  # top rows
                ((2,), [(2,)]),  # offers_in_progress
            ]
        )
        result = get_pipeline_summary(store, self.settings)
        self.assertTrue(result["ok"])
        pipeline = result["pipeline"]
        self.assertIsNone(pipeline["total_value_pln"])
        self.assertEqual(pipeline["value_tracking"], "not_implemented")
        self.assertIsNone(pipeline["top_3_by_value"][0]["value_pln"])

    def test_win_rate_trend_is_not_a_fabricated_stable(self):
        from agent_runtime.business_pulse import get_win_rate
        store = _QueueStore([({}, [("completed", "won"), ("lost", "")])])
        result = get_win_rate(store, self.settings)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["win_rate"]["trend"])

    def test_top_clients_value_fields_are_not_fabricated(self):
        from agent_runtime.business_pulse import get_top_clients
        store = _QueueStore(
            [({}, [("case1", "Jan Kowalski", "2026-01-01T00:00:00Z", "active")])]
        )
        result = get_top_clients(store, self.settings)
        self.assertTrue(result["ok"])
        client = result["top_clients"][0]
        self.assertIsNone(client["pipeline_value_pln"])
        self.assertIsNone(client["active_offers"])

    def test_revenue_forecast_is_untracked_when_pipeline_value_is_untracked(self):
        from agent_runtime.business_pulse import get_revenue_forecast
        store = _QueueStore(
            [
                ({}, [("completed", "won"), ("lost", "")]),  # get_win_rate
                ((5,), [(5,)]),  # pipeline: total count
                ({}, [("active", 3)]),  # pipeline: by_status
                ({}, []),  # pipeline: top rows
                ((0,), [(0,)]),  # pipeline: offers_in_progress
            ]
        )
        result = get_revenue_forecast(store, self.settings)
        self.assertTrue(result["ok"])
        forecast = result["forecast"]
        self.assertIsNone(forecast["total_forecast_pln"])
        self.assertEqual(forecast["value_tracking"], "not_implemented")


if __name__ == "__main__":
    unittest.main()
