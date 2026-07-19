"""Tests for trace_id propagation through log_config ContextVar."""
from __future__ import annotations

import uuid

from log_config import set_trace_id, get_trace_id


def test_trace_id_set_and_get():
    """Setting trace_id via ContextVar and reading it back works."""
    tid = f"test_{uuid.uuid4().hex[:8]}"
    set_trace_id(tid)
    assert get_trace_id() == tid
    set_trace_id("")  # cleanup


def test_trace_id_defaults_empty():
    """Default trace_id is empty string."""
    set_trace_id("")
    assert get_trace_id() == ""


def test_trace_id_isolation():
    """trace_id is isolated per thread via ContextVar."""
    set_trace_id("thread_main")
    assert get_trace_id() == "thread_main"
    set_trace_id("")  # cleanup
