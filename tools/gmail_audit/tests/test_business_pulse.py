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


if __name__ == "__main__":
    unittest.main()
