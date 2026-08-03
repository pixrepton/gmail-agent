"""RP-31: business outcome substrate + truthful win-rate."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.business_pulse import get_revenue_forecast, get_win_rate
from business_outcome import classify_case_outcome, record_business_outcome


class _QueueCursor:
    def __init__(self, steps):
        self._steps = list(steps)
        self._current = ({}, [])

    def execute(self, query, *args):
        if self._steps:
            self._current = self._steps.pop(0)
        return self

    def fetchone(self):
        return self._current[0]

    def fetchall(self):
        return self._current[1]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _QueueStore:
    def __init__(self, steps):
        self._steps = steps
        self.cases: dict[str, dict] = {}

    def _connect(self):
        conn = SimpleNamespace()
        conn.cursor = lambda: _QueueCursor(self._steps)
        return conn

    def mutate_case(self, case_id: str, mutator, *, create_if_missing: bool = False) -> dict:
        row = dict(self.cases.get(case_id) or {"case_id": case_id, "metadata": {}})
        if case_id not in self.cases and not create_if_missing:
            raise LookupError(case_id)
        updated = mutator(row)
        self.cases[case_id] = updated
        return updated


def test_classify_completed_as_won_not_fictional_won_status() -> None:
    assert classify_case_outcome(status="completed") == "won"
    assert classify_case_outcome(status="won") == "open"
    assert classify_case_outcome(status="lost") == "lost"


def test_get_win_rate_uses_lifecycle_completed() -> None:
    store = _QueueStore(
        [
            (
                {},
                [
                    ("completed", "won"),
                    ("lost", ""),
                    ("active", ""),
                ],
            )
        ]
    )
    result = get_win_rate(store, object())
    assert result["ok"] is True
    assert result["win_rate"]["won"] == 1
    assert result["win_rate"]["lost"] == 1
    assert result["win_rate"]["rate_pct"] == 50.0


def test_record_business_outcome_persists_metadata() -> None:
    store = _QueueStore([])
    store.cases["case_out"] = {"case_id": "case_out", "status": "active", "metadata": {}}
    result = record_business_outcome(store, case_id="case_out", outcome="won", source="operator_test")
    assert result["ok"] is True
    assert store.cases["case_out"]["metadata"]["resolution_outcome"] == "won"
    assert store.cases["case_out"]["status"] == "completed"


def test_revenue_forecast_rejects_missing_decided_win_rate() -> None:
    store = _QueueStore(
        [
            ({}, [("active", "")]),  # win_rate: no decided cases
            ((3,), [(3,)]),  # pipeline total
            ({}, [("active", 3)]),  # by_status
            ({}, []),  # top rows
            ((1,), [(1,)]),  # offers_in_progress
        ]
    )
    result = get_revenue_forecast(store, object())
    assert result["ok"] is True
    assert result["forecast"]["total_forecast_pln"] is None
    assert "win_rate_unavailable" in result["forecast"]["method"]
