from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from daszek_bridge_queue_drain import drain_bridge_rows


def test_drain_action_decision_approve_calls_executor() -> None:
    row = {
        "queue_id": "q_test_1",
        "domain": "action_decision",
        "schema_version": "daszek_bridge_queue.v1",
        "bridge_status": "pending",
        "proposal_id": "prop_1",
        "decision": "approve",
        "actor_id": "operator",
        "reason": "smoke",
    }
    completions: list[tuple[str, str, str]] = []

    def append_completion(queue_id: str, status: str, error: str = "") -> None:
        completions.append((queue_id, status, error))

    store = MagicMock()
    journal = MagicMock()
    runtime_context = MagicMock()

    with __import__("unittest.mock").mock.patch(
        "execution_runtime.approve_action_proposal",
        return_value=MagicMock(to_dict=lambda: {"ok": True, "proposal_id": "prop_1"}),
    ) as approve_mock:
        results = drain_bridge_rows(
            pending=[row],
            append_completion=append_completion,
            bridge_operator_feedback=MagicMock(),
            store=store,
            journal=journal,
            runtime_context=runtime_context,
            max_items=1,
            dry_run=False,
        )

    assert len(results) == 1
    assert results[0]["ok"] is True
    approve_mock.assert_called_once()
    assert completions == [("q_test_1", "completed", "")]
