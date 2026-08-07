"""AI-OS 5.2 — correction ledger query contract."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from correction_ledger import fetch_correction_ledger


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_args, **_kwargs):
        return self

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


def test_fetch_correction_ledger_returns_joined_rows() -> None:
    conn = _FakeConn(
        [
            (
                "prop_1",
                "eng_1",
                "case_1",
                "2026-08-06T10:00:00Z",
                "draft_reply",
                '{"text":"hello"}',
                "reason",
                "agent_chat",
                "resp_1",
                "EXACT",
                "2026-08-06T10:01:00Z",
                0.9,
                "",
            )
        ]
    )
    items = fetch_correction_ledger(conn, case_id="case_1", limit=10)
    assert len(items) == 1
    assert items[0]["proposal_id"] == "prop_1"
    assert items[0]["response_type"] == "EXACT"
