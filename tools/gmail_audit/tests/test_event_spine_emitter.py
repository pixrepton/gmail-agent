from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from correlation_registry.store import InMemoryCorrelationRegistryStore  # noqa: F401
from event_spine.emitter import publish_os_event


def test_publish_os_event_requires_database_url() -> None:
    assert publish_os_event(database_url="", event_type="test") is None
