"""Shared pytest fixtures — isolate process env leaks between tests."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _restore_os_environ_after_test() -> None:
    """Full-suite runs leak AGENT_/OPENAI_ env from earlier tests into planner endpoint order."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
