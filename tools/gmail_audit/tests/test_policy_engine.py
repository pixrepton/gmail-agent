"""Tests for policy_engine.py — rule evaluation."""
from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from policy_engine import PolicyEngine


class TestPolicyEngineInit:
    def test_engine_creates_default(self):
        engine = PolicyEngine()
        assert engine is not None

    def test_engine_has_decision_method(self):
        engine = PolicyEngine()
        assert callable(getattr(engine, "evaluate", None))
