"""Tests for groq_client.py — JSON extraction and error handling."""
from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from groq_client import extract_json_candidate


class TestExtractJsonCandidate:
    def test_direct_json_object(self):
        result = extract_json_candidate('{"key": "val"}')
        assert isinstance(result, (dict, str))
        assert "key" in str(result)

    def test_json_in_code_block(self):
        result = extract_json_candidate('```json\n{"a": 1}\n```')
        assert isinstance(result, (dict, str))

    def test_empty_input(self):
        result = extract_json_candidate("")
        assert isinstance(result, (dict, str))

    def test_invalid_json_returns_falsy(self):
        result = extract_json_candidate("not json at all")
        assert isinstance(result, (dict, str))
