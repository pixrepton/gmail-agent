"""SPINE-WORKER-TICK-01 — idle sleep drains agent_chat_jobs in short chunks."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import signal_worker


def test_idle_sleep_with_agent_chat_drain_ticks_between_chunks():
    settings = SimpleNamespace(
        gmail_change_detection_enabled=True,
        drive_change_detection_enabled=False,
        gmail_history_poll_interval_sec=30,
        drive_changes_poll_interval_sec=120,
        agent_chat_jobs_tick_interval_sec=10,
        agent_chat_jobs_max_per_tick=3,
    )
    sleeps: list[float] = []

    def _fake_sleep(seconds: float) -> bool:
        sleeps.append(float(seconds))
        return False

    tick = MagicMock(return_value={"processed": 0})
    with patch.object(signal_worker, "_sleep_with_abort", side_effect=_fake_sleep):
        with patch(
            "agent_runtime.agent_chat_worker.process_agent_chat_jobs_tick",
            tick,
        ):
            aborted = signal_worker._idle_sleep_with_agent_chat_drain(settings)

    assert aborted is False
    assert sleeps == [10.0, 10.0, 10.0]
    assert tick.call_count == 2
    tick.assert_called_with(settings, max_jobs=3)


def test_idle_sleep_aborts_on_sigterm():
    settings = SimpleNamespace(
        gmail_change_detection_enabled=True,
        drive_change_detection_enabled=False,
        gmail_history_poll_interval_sec=20,
        agent_chat_jobs_tick_interval_sec=10,
        agent_chat_jobs_max_per_tick=1,
    )
    with patch.object(signal_worker, "_sleep_with_abort", return_value=True):
        with patch("agent_runtime.agent_chat_worker.process_agent_chat_jobs_tick") as tick:
            aborted = signal_worker._idle_sleep_with_agent_chat_drain(settings)
    assert aborted is True
    tick.assert_not_called()
