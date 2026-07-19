"""Tests for canonical_signal_bridge module."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from canonical_signal_bridge import bridge_canonical_signals


def test_bridge_returns_empty_when_feature_flag_off() -> None:
    store = mock.MagicMock()
    journal = mock.MagicMock()
    result = bridge_canonical_signals(store, journal, case_id="case-1", feature_flag=False)
    assert result == []
    journal.fetch_signals_for_case.assert_not_called()


def test_bridge_returns_signals_when_feature_flag_on() -> None:
    from signal_contract import build_canonical_signal

    store = mock.MagicMock()
    journal = mock.MagicMock()
    sig = build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"message_id": "m1"},
        observed_at="2026-07-01T10:00:00+00:00",
        signal_summary_pl="Nowa wiadomość",
        payload={"case_id": "case-1"},
        artifacts={},
    )
    journal.fetch_signals_for_case.return_value = [sig]

    result = bridge_canonical_signals(store, journal, case_id="case-1", feature_flag=True)

    assert len(result) == 1
    assert result[0]["signal_id"] == sig.signal_id
    assert result[0]["type"] == "gmail_message_observed"
    assert result[0]["summary"] == "Nowa wiadomość"
    assert result[0]["bridge_source"] == "canonical_signal_bridge"
    journal.fetch_signals_for_case.assert_called_once_with("case-1", limit=50)


def test_bridge_empty_signals_with_flag_on_returns_empty_list() -> None:
    store = mock.MagicMock()
    journal = mock.MagicMock()
    journal.fetch_signals_for_case.return_value = []

    result = bridge_canonical_signals(store, journal, case_id="empty-case", feature_flag=True)

    assert result == []
    journal.fetch_signals_for_case.assert_called_once()
