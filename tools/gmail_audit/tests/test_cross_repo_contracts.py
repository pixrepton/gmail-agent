"""Cross-repo contract tests — verify API response contracts between Node A and Node B.

Hits actual Node B endpoints (localhost:8766) and validates response structure.
Guards against silent contract breakage when changing data structures.
All tests are read-only — no mutations.
"""
from __future__ import annotations

import json
import os
import unittest
from urllib.error import URLError
from urllib.request import urlopen, Request

NODE_B_BASE = os.environ.get("NODE_B_TEST_URL", "http://127.0.0.1:8766")


def _fetch(path: str) -> dict:
    """Fetch JSON from Node B API."""
    url = f"{NODE_B_BASE}{path}"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=10) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)
    except URLError as exc:
        if hasattr(exc, "code") and exc.code == 404:
            return {"ok": False, "_status": 404, "error": f"Endpoint not found: {path}"}
        return {"ok": False, "error": str(exc)}
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"Invalid JSON: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


class SkipIfNodeBDown:
    """Skip all tests if Node B is unreachable."""

    @staticmethod
    def check() -> bool:
        try:
            data = _fetch("/health")
            return data.get("ok", False) or bool(data.get("mode"))
        except Exception:
            return False


class TestHealthContract(unittest.TestCase):
    """GET /health — service health."""

    def setUp(self):
        if not SkipIfNodeBDown.check():
            self.skipTest("Node B is not available")

    def test_health_returns_required_fields(self):
        data = _fetch("/health")
        self.assertIn("mode", data)
        self.assertIn("truth_source", data)


class TestOsEventsContract(unittest.TestCase):
    """GET /system/os-events/recent — system events."""

    def setUp(self):
        if not SkipIfNodeBDown.check():
            self.skipTest("Node B is not available")

    def test_os_events_returns_items(self):
        data = _fetch("/system/os-events/recent?limit=5")
        self.assertTrue(data.get("ok", False), f"Response not ok: {data}")
        self.assertIn("items", data)
        self.assertIsInstance(data["items"], list)
        self.assertIn("count", data)

    def test_os_events_items_structure(self):
        data = _fetch("/system/os-events/recent?limit=3")
        if data["items"]:
            item = data["items"][0]
            if isinstance(item, dict):
                self.assertIn("event_type", item)


class TestDecisionQueueContract(unittest.TestCase):
    """GET /system/decision-queue — decision queue with SLA."""

    def setUp(self):
        if not SkipIfNodeBDown.check():
            self.skipTest("Node B is not available")

    def test_decision_queue_returns_items(self):
        data = _fetch("/system/decision-queue?limit=5")
        if data.get("_status") == 404:
            self.skipTest("Not available in this runtime")
        self.assertIn("items", data)
        self.assertIsInstance(data.get("items"), list)

    def test_decision_queue_item_structure(self):
        data = _fetch("/system/decision-queue?limit=3")
        if data.get("_status") == 404:
            self.skipTest("Not available in this runtime")
        for item in data.get("items", []):
            self.assertIn("proposal_id", item)
            self.assertIn("priority", item)
            self.assertIn("hours_waiting", item)
            self.assertIn("created_at", item)


class TestBriefingContract(unittest.TestCase):
    """GET /system/briefing — NL briefing."""

    def setUp(self):
        if not SkipIfNodeBDown.check():
            self.skipTest("Node B is not available")

    def test_briefing_returns_text(self):
        data = _fetch("/system/briefing")
        if data.get("_status") == 404:
            return  # endpoint not available on this env
        self.assertTrue(data.get("ok", False))
        self.assertIn("briefing", data)
        self.assertIsInstance(data["briefing"], str)
        self.assertGreater(len(data["briefing"]), 10)


class TestCostSummaryContract(unittest.TestCase):
    """GET /system/cost-summary — token cost tracking."""

    def setUp(self):
        if not SkipIfNodeBDown.check():
            self.skipTest("Node B is not available")

    def test_cost_summary_structure(self):
        data = _fetch("/system/cost-summary")
        self.assertTrue(data.get("ok", False))
        self.assertIn("today", data)
        self.assertIn("this_week", data)

    def test_cost_summary_has_tokens(self):
        data = _fetch("/system/cost-summary")
        today = data.get("today", {})
        self.assertIn("tokens", today)
        self.assertIn("estimated_cost_pln", today)


class TestQualitySummaryContract(unittest.TestCase):
    """GET /system/quality-summary — quality scoring."""

    def setUp(self):
        if not SkipIfNodeBDown.check():
            self.skipTest("Node B is not available")

    def test_quality_summary_structure(self):
        data = _fetch("/system/quality-summary")
        self.assertIn("avg_quality_score", data)
        self.assertIn("exact_match_pct", data)
        self.assertIn("total_responses", data)


class TestConstitutionContract(unittest.TestCase):
    """GET /system/constitution — active agent constitution."""

    def setUp(self):
        if not SkipIfNodeBDown.check():
            self.skipTest("Node B is not available")

    def test_constitution_has_sections(self):
        data = _fetch("/system/constitution")
        self.assertTrue(data.get("ok", False), f"Constitution error: {data}")
        self.assertIn("sections", data)
        self.assertIn("forbidden_actions", data)

    def test_constitution_has_forbidden_actions(self):
        data = _fetch("/system/constitution")
        self.assertIsInstance(data.get("forbidden_actions", []), list)
        self.assertGreater(len(data.get("forbidden_actions", [])), 0)


class TestBusinessDictionaryContract(unittest.TestCase):
    """GET /business-dictionary/terms — business dictionary."""

    def setUp(self):
        if not SkipIfNodeBDown.check():
            self.skipTest("Node B is not available")

    def test_dictionary_returns_terms(self):
        data = _fetch("/business-dictionary/terms?limit=5")
        self.assertIn("terms", data)
        self.assertIsInstance(data.get("terms"), list)

    def test_dictionary_stats_structure(self):
        data = _fetch("/business-dictionary/stats")
        self.assertTrue(data.get("ok", False))
        self.assertIn("stats", data)


if __name__ == "__main__":
    unittest.main()
