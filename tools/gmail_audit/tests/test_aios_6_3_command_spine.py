"""AI-OS 6.3 — OperatorCommand spine returns receipt."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.operator_command_spine import run_operator_command_spine


def test_operator_command_spine_returns_receipt() -> None:
    fake_snapshot = SimpleNamespace(
        agent_memory=SimpleNamespace(materialize_proposals=[]),
        hitl_gate=SimpleNamespace(required=False),
        user_instruction="",
    )
    fake_resolution = SimpleNamespace(engagement_id="eng_1")
    with patch("mailbox_memory_runtime.build_mailbox_memory_runtime", return_value=None), patch(
        "agent_runtime.agent_reconcile.run_agent_reconcile_staging",
        return_value=(fake_snapshot, None, fake_resolution, []),
    ), patch(
        "agent_runtime.agent_reconcile.build_operator_engagement_store",
        return_value=MagicMock(),
    ):
        result = run_operator_command_spine(
            user_input="sprawdz pipeline",
            session_id="sess_1",
            case_id="case_1",
            opmem_context={},
            settings=SimpleNamespace(),
        )
    assert result["command_id"]
    assert result["receipt"]["receipt_kind"] == "operator_command"
    assert result["receipt"]["status"] in {"completed", "hitl_required", "failed"}
