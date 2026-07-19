from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import build_initial_snapshot
from hitl_gmail_send import GMAIL_SEND_SCOPE, execute_hitl_gmail_send
from llm_contracts.engagement_snapshot_v2 import ActionItem


def _snapshot_with_draft() -> object:
    snap = build_initial_snapshot(case_id="case_send", engagement_id="eng_send", trace_id="t1")
    return apply_snapshot_delta(
        snap,
        {
            "hitl_gate": {"required": False, "reason": ""},
            "actions": [
                ActionItem(id="draft_reply", enabled=True, payload_pl="Witaj, to jest draft.").model_dump(
                    mode="python"
                )
            ],
        },
    )


def test_execute_hitl_gmail_send_flag_off() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("AGENT_HITL_EXECUTE_SEND", None)
        out = execute_hitl_gmail_send(
            settings=SimpleNamespace(google_oauth_scopes=[]),
            snapshot=_snapshot_with_draft(),
            action_id="draft_reply",
        )
    assert out["executed"] is False
    assert out["reason"] == "queue_only_mvp"


def test_execute_hitl_gmail_send_bounded_dry_run_without_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_HITL_EXECUTE_SEND", "1")
    monkeypatch.setenv("AGENT_HITL_SEND_TO", "klient@example.com")
    out = execute_hitl_gmail_send(
        settings=SimpleNamespace(google_oauth_scopes=["https://www.googleapis.com/auth/gmail.readonly"]),
        snapshot=_snapshot_with_draft(),
        action_id="draft_reply",
        operator_id="konrad",
    )
    assert out["executed"] is True
    assert out["mode"] == "bounded_dry_run"
    assert out["reason"] == "gmail_send_scope_missing"


def test_execute_hitl_gmail_send_live_when_scope_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_HITL_EXECUTE_SEND", "1")
    monkeypatch.setenv("AGENT_HITL_SEND_TO", "klient@example.com")
    settings = SimpleNamespace(google_oauth_scopes=[GMAIL_SEND_SCOPE])
    with patch(
        "google_gmail_api.send_raw_message",
        return_value={"id": "msg_123", "threadId": "thr_456"},
    ):
        out = execute_hitl_gmail_send(
            settings=settings,
            snapshot=_snapshot_with_draft(),
            action_id="draft_reply",
            operator_id="konrad",
        )
    assert out["executed"] is True
    assert out["mode"] == "live"
    assert out["message_id"] == "msg_123"
