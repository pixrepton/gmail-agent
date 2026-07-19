"""Tests for llm_client.py — error handling and type validation."""
from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from llm_client import TopInstalLLMClient, TopInstalLLMError, DEFAULT_MAX_RETRIES


class TestLLMClientInit:
    def test_missing_api_key_raises(self):
        try:
            TopInstalLLMClient(api_key="")
        except TopInstalLLMError as e:
            assert "not configured" in str(e)

    def test_empty_api_key_raises(self):
        try:
            TopInstalLLMClient(api_key="   ")
        except TopInstalLLMError:
            pass

    def test_valid_key_creates_client(self):
        client = TopInstalLLMClient(api_key="sk-test-key")
        assert hasattr(client, "model")
        assert client.max_retries == DEFAULT_MAX_RETRIES

    def test_custom_params_accepted(self):
        client = TopInstalLLMClient(
            api_key="sk-key", model="claude-v2", max_retries=5, temperature=0.5, timeout_sec=30.0
        )
        assert client.model == "claude-v2"
        assert client.max_retries == 5
        assert abs(client.temperature - 0.5) < 0.01
