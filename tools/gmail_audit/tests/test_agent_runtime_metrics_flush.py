"""Regression guard: metrics flush at the interval boundary must not deadlock.

Root cause: ``MetricsCollector`` used a non-reentrant ``threading.Lock`` while
``record_*`` (holding the lock) called ``_maybe_flush`` -> ``_flush`` ->
``report`` (re-acquiring the same lock). The first flush-interval boundary
deadlocked the collector. Fixed with ``threading.RLock``.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.metrics import MetricsCollector


def test_metrics_flush_at_interval_does_not_deadlock(tmp_path: Path) -> None:
    collector = MetricsCollector(
        metrics_path=str(tmp_path / "metrics.json"),
        flush_interval=1,  # every record triggers a flush while the lock is held
    )
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            collector.record_agent_turn(engagement_id="eng_1", tool="generate_draft_reply")
            collector.record_agent_turn(engagement_id="eng_1", tool="generate_draft_reply")
            collector.record_hitl(engagement_id="eng_1", action="approve")
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive(), "metrics flush deadlocked (non-reentrant lock)"
    assert not errors, f"metrics worker raised: {errors[0]!r}"

    report = collector.report()
    assert report["agent_turns"] == 2
    assert report["hitl"]["approves"] == 1
    assert (tmp_path / "metrics.json").exists()
