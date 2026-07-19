"""Chaos: Daszek 503 — push feed nie blokuje workera gdy Daszek nie odpowiada."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

TOOL_DIR = Path(__file__).resolve().parent.parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from daszek_client import DaszekClientError
from daszek_v3_feed_runtime import _push_feed_snapshot_sync


def test_daszek_down_does_not_crash_push() -> None:
    """Gdy Daszek zwraca 503, _push_feed_snapshot_sync lapie blad i kontynuuje."""
    mock_client = MagicMock()
    mock_client.post_v3_operational_feed_snapshot.side_effect = DaszekClientError(
        "503 Service Unavailable"
    )

    run_state: dict = {"daszek_client": mock_client, "summary": {}}
    settings = MagicMock()

    # Nie powinno rzucic wyjatku
    _push_feed_snapshot_sync(
        run_state=run_state,
        settings=settings,
        snapshot={"snapshot_id": "test-snap-001"},
        trigger_message_id="test-msg-001",
    )

    # Blad zalogowany, worker nie crashuje
    assert run_state.get("summary", {}).get("operational_feed_push_failed", 0) >= 1
    mock_client.post_v3_operational_feed_snapshot.assert_called_once()


def test_daszek_down_no_client_skips_gracefully() -> None:
    """Gdy daszek_client nie jest ustawiony, push skip loguje i wraca bez bledu."""
    result = _push_feed_snapshot_sync(
        run_state={"summary": {}},
        settings=MagicMock(),
        snapshot={"snapshot_id": "test-snap-002"},
        trigger_message_id="test-msg-002",
    )
    # Funkcja zwraca None, nie rzuca wyjatku
    assert result is None
