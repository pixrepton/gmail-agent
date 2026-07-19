"""Tests for pattern_discovery.py — regex gap detection and proposal generation."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from pattern_discovery import PatternDiscovery, _longest_common_prefix, _longest_common_suffix
from divergence_loop import CANDIDATE_PATTERN


def _mock_conn() -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = []
    cur.fetchone.return_value = None
    cursor_cm = MagicMock()
    cursor_cm.__enter__.return_value = cur
    cursor_cm.__exit__.return_value = None
    conn.cursor.return_value = cursor_cm
    return conn


# ── _longest_common_prefix ──────────────────────────────────────────────

class TestLongestCommonPrefix:
    def test_empty_list(self):
        assert _longest_common_prefix([]) == ""

    def test_single_string(self):
        assert _longest_common_prefix(["abc"]) == "abc"

    def test_common_prefix(self):
        assert _longest_common_prefix(["prefix_abc", "prefix_xyz", "prefix_123"]) == "prefix_"

    def test_no_common_prefix(self):
        assert _longest_common_prefix(["abc", "xyz", "123"]) == ""

    def test_all_same(self):
        assert _longest_common_prefix(["same", "same", "same"]) == "same"


# ── _longest_common_suffix ──────────────────────────────────────────────

class TestLongestCommonSuffix:
    def test_empty_list(self):
        assert _longest_common_suffix([]) == ""

    def test_single_string(self):
        assert _longest_common_suffix(["abc"]) == "abc"

    def test_common_suffix(self):
        assert _longest_common_suffix(["abc_suffix", "xyz_suffix", "123_suffix"]) == "_suffix"

    def test_no_common_suffix(self):
        assert _longest_common_suffix(["abc", "xyz", "123"]) == ""


# ── PatternDiscovery.suggest_pattern ────────────────────────────────────

class TestSuggestPattern:
    def test_fewer_than_3_values_returns_none(self):
        pd = PatternDiscovery(_mock_conn())
        assert pd.suggest_pattern(["a", "b"]) is None

    def test_common_prefix_found(self):
        pd = PatternDiscovery(_mock_conn())
        pattern = pd.suggest_pattern(["ul. Warszawska", "ul. Krakowska", "ul. Gdanska"])
        assert pattern is not None
        assert "ul" in pattern

    def test_common_suffix_found(self):
        pd = PatternDiscovery(_mock_conn())
        pattern = pd.suggest_pattern(["Kraków", "Warszawa", "Gdańsk"])
        # No common prefix or suffix with len > 3, should return None
        assert pattern is None

    def test_no_common_pattern(self):
        pd = PatternDiscovery(_mock_conn())
        pattern = pd.suggest_pattern(["abc123", "xyz789", "def456"])
        assert pattern is None

    def test_short_prefix_ignored(self):
        """Prefix krotszy niz 3 znaki jest ignorowany."""
        pd = PatternDiscovery(_mock_conn())
        pattern = pd.suggest_pattern(["abX", "abY", "abZ"])
        assert pattern is None


# ── PatternDiscovery.generate_proposal ──────────────────────────────────

class TestGenerateProposal:
    def test_uses_candidate_pattern_status(self):
        pd = PatternDiscovery(_mock_conn())
        proposal = pd.generate_proposal("customer_phone", r"\d{9}", ["123456789", "987654321"])
        assert proposal["status"] == CANDIDATE_PATTERN
        assert proposal["fact_key"] == "customer_phone"
        assert proposal["proposed_pattern"] == r"\d{9}"

    def test_confidence_scales_with_examples(self):
        pd = PatternDiscovery(_mock_conn())
        p1 = pd.generate_proposal("k1", r".+", ["a"] * 10)
        p2 = pd.generate_proposal("k2", r".+", ["a"] * 2)
        assert p1["confidence"] >= p2["confidence"]

    def test_examples_limited_to_5(self):
        pd = PatternDiscovery(_mock_conn())
        examples = [f"val_{i}" for i in range(20)]
        proposal = pd.generate_proposal("test_key", r"val_\d+", examples)
        assert len(proposal["supporting_examples"]) <= 5


# ── PatternDiscovery.find_regex_gaps ────────────────────────────────────

class TestFindRegexGaps:
    def test_no_gaps_returns_empty(self):
        conn = _mock_conn()
        pd = PatternDiscovery(conn)
        result = pd.find_regex_gaps()
        assert result == []

    def test_finds_llm_only_facts(self):
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [
            ("case_1", "customer_phone", "123456789"),
            ("case_2", "city", "Krakow"),
        ]
        pd = PatternDiscovery(conn)
        result = pd.find_regex_gaps()
        assert len(result) == 2
        assert result[0]["case_id"] == "case_1"
        assert result[0]["fact_key"] == "customer_phone"


# ── PatternDiscovery.run_discovery ──────────────────────────────────────

class TestRunDiscovery:
    def test_empty_gaps_returns_empty(self):
        conn = _mock_conn()
        conn.cursor().fetchall.return_value = []
        pd = PatternDiscovery(conn)
        proposals = pd.run_discovery()
        assert proposals == []

    def test_generates_proposals_for_gaps(self):
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [
            ("c1", "customer_phone", "600700800"),
            ("c2", "customer_phone", "600700801"),
            ("c3", "customer_phone", "600700802"),
        ]
        pd = PatternDiscovery(conn)
        proposals = pd.run_discovery()
        assert len(proposals) >= 1
        assert proposals[0]["fact_key"] == "customer_phone"
        assert proposals[0]["status"] == CANDIDATE_PATTERN
