from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from daszek_bridge_queue_drain import maybe_worker_bridge_drain_tick


def test_worker_bridge_drain_skips_without_client() -> None:
    run_state: dict = {"summary": {}}
    settings = MagicMock()
    runtime = MagicMock()
    results = maybe_worker_bridge_drain_tick(
        run_state=run_state,
        settings=settings,
        runtime=runtime,
        max_items=5,
    )
    assert results == []


def test_worker_bridge_drain_calls_remote_drain() -> None:
    run_state: dict = {"summary": {}, "daszek_client": MagicMock()}
    settings = MagicMock()
    settings.signal_journal_jsonl_mirror_enabled = False
    settings.groq_model = "test"
    settings.signal_runtime_mode = "active"
    runtime = MagicMock()
    runtime.store = MagicMock()
    runtime.graph_store = None

    with patch("daszek_bridge_queue_drain.fetch_remote_pending_bridge_rows", return_value=[]):
        with patch("daszek_bridge_queue_drain.drain_bridge_rows", return_value=[]) as drain_mock:
            results = maybe_worker_bridge_drain_tick(
                run_state=run_state,
                settings=settings,
                runtime=runtime,
                max_items=3,
            )
    assert results == []
    drain_mock.assert_called_once()
