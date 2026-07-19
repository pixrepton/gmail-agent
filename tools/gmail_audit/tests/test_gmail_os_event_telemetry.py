from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from event_spine.gmail_telemetry import (
    publish_gmail_feed_push_event,
    publish_gmail_reconcile_completed,
)


def test_publish_gmail_feed_push_event_success() -> None:
    captured: list[dict] = []

    def _capture(**kwargs):  # type: ignore[no-untyped-def]
        captured.append(dict(kwargs))
        return "osevt_feed_ok"

    settings = __import__("types").SimpleNamespace(mailbox_memory_database_url="postgresql://test")
    with patch("event_spine.gmail_telemetry.publish_os_event", side_effect=_capture):
        eid = publish_gmail_feed_push_event(
            settings,
            ok=True,
            snapshot_id="snap-1",
            case_id="case_1",
            trigger="cel_reconcile",
        )
    assert eid == "osevt_feed_ok"
    assert captured[0]["event_type"] == "gmail.feed.pushed"
    assert captured[0]["payload"]["summary_pl"]


def test_publish_gmail_feed_push_event_failure() -> None:
    with patch("event_spine.gmail_telemetry.publish_os_event", return_value="osevt_fail") as mock:
        settings = __import__("types").SimpleNamespace(mailbox_memory_database_url="postgresql://test")
        publish_gmail_feed_push_event(settings, ok=False, error="timeout")
    assert mock.call_args.kwargs["event_type"] == "gmail.feed.push_failed"
    assert mock.call_args.kwargs["payload"]["status"] == "error"


def test_publish_gmail_reconcile_completed_skips_duplicate() -> None:
    with patch("event_spine.gmail_telemetry.publish_os_event") as mock:
        result = __import__("types").SimpleNamespace(
            processing_state="skipped_duplicate",
            case_id="case_x",
            signal_id="sig_x",
            stage_outputs={},
        )
        publish_gmail_reconcile_completed(
            __import__("types").SimpleNamespace(mailbox_memory_database_url="postgresql://test"),
            result,
        )
    mock.assert_not_called()


def test_publish_gmail_reconcile_completed_emits() -> None:
    with patch("event_spine.gmail_telemetry.publish_os_event", return_value="osevt_rec") as mock:
        result = __import__("types").SimpleNamespace(
            processing_state="reconciled",
            case_id="case_x",
            signal_id="sig_x",
            stage_outputs={"agent_engagement_snapshot": {"engagement_id": "eng_x"}},
        )
        settings = __import__("types").SimpleNamespace(mailbox_memory_database_url="postgresql://test")
        out = publish_gmail_reconcile_completed(settings, result, trigger_message_id="msg-1")
    assert out == "osevt_rec"
    assert mock.call_args.kwargs["event_type"] == "gmail.reconcile.completed"
    assert mock.call_args.kwargs["engagement_id"] == "eng_x"
