from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
        settings=SimpleNamespace(
            google_oauth_scopes=["https://www.googleapis.com/auth/gmail.readonly"],
            mailbox_memory_database_url="configured_for_explicit_override",
        ),
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
    settings = SimpleNamespace(
        google_oauth_scopes=[GMAIL_SEND_SCOPE],
        mailbox_memory_database_url="configured_for_explicit_override",
    )
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


def test_execute_hitl_gmail_send_fails_closed_without_mailbox_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_HITL_EXECUTE_SEND", "1")
    monkeypatch.delenv("AGENT_HITL_SEND_TO", raising=False)
    effect_started: list[bool] = []
    settings = SimpleNamespace(
        google_oauth_scopes=[GMAIL_SEND_SCOPE],
        mailbox_memory_database_url="",
        mailbox_memory_stage_mode="live",
    )

    with patch("google_gmail_api.send_raw_message") as send_raw:
        out = execute_hitl_gmail_send(
            settings=settings,
            snapshot=_snapshot_with_draft(),
            action_id="draft_reply",
            operator_id="konrad",
            on_effect_start=lambda: effect_started.append(True),
        )

    assert out == {
        "executed": False,
        "reason": "mailbox_memory_database_url_required",
        "effect_started": False,
        "decision_status": "failed_before_execution",
    }
    assert effect_started == []
    send_raw.assert_not_called()


def test_execute_hitl_gmail_send_does_not_report_unresolved_recipient_as_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_HITL_EXECUTE_SEND", "1")
    monkeypatch.delenv("AGENT_HITL_SEND_TO", raising=False)
    effect_started: list[bool] = []
    settings = SimpleNamespace(
        google_oauth_scopes=[GMAIL_SEND_SCOPE],
        mailbox_memory_database_url="postgresql://configured.invalid/mailbox",
        mailbox_memory_stage_mode="live",
    )
    runtime = MagicMock()
    runtime.get_context_pack.return_value = {"intake_output": {}, "facts": []}

    with (
        patch("mailbox_memory_runtime.build_mailbox_memory_runtime", return_value=runtime),
        patch("google_gmail_api.send_raw_message") as send_raw,
    ):
        out = execute_hitl_gmail_send(
            settings=settings,
            snapshot=_snapshot_with_draft(),
            action_id="draft_reply",
            on_effect_start=lambda: effect_started.append(True),
        )

    assert out["executed"] is False
    assert out["reason"] == "recipient_unresolved"
    assert out["effect_started"] is False
    assert out["decision_status"] == "failed_before_execution"
    assert effect_started == []
    send_raw.assert_not_called()
