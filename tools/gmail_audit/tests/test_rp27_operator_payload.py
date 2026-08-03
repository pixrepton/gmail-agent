"""RP-27 / RC-10: operator draft and clarification answer payload channels."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_hitl_bridge import agent_hitl_payload_from_row
from agent_runtime.mcp_service import AgentMcpService, dispatch_mcp_tool
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.store import InMemoryOperatorEngagementStore
from hitl_gmail_send import execute_hitl_gmail_send
from llm_contracts.engagement_snapshot_v2 import (
    ActionItem,
    AgentMemory,
    EngagementSnapshotV2,
    GapItem,
    HitlGate,
    OperationalStatus,
)


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=12,
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="",
        kalk_top_agent_key="",
        kalk_top_timeout_sec=4,
        kalk_top_max_retries=3,
    )


def test_rp27_approve_persists_operator_draft_pl() -> None:
    store = InMemoryOperatorEngagementStore()
    snap = EngagementSnapshotV2(
        engagement_id="eng_rp27_draft",
        case_id="case_rp27",
        version=1,
        operational_status=OperationalStatus(code="pending_operator", steps_remaining=1, blocking=True),
        hitl_gate=HitlGate(required=True, reason="draft_reply"),
        actions=[ActionItem(id="draft_reply", enabled=True, payload_pl="stary draft")],
    )
    store.insert_snapshot(snap)
    svc = AgentMcpService(settings=_settings(), store=store)

    out = svc.approve_hitl_action(
        engagement_id="eng_rp27_draft",
        action_id="draft_reply",
        operator_id="op1",
        operator_draft_pl="poprawiony draft od operatora",
    )
    assert out.get("ok") is True, out
    assert out.get("operator_draft_applied") is True
    loaded = store.load_snapshot("eng_rp27_draft")
    assert loaded is not None
    assert loaded.hitl_gate.required is False
    assert loaded.actions[0].payload_pl == "poprawiony draft od operatora"


def test_rp27_clarification_requires_answer_and_persists() -> None:
    store = InMemoryOperatorEngagementStore()
    snap = EngagementSnapshotV2(
        engagement_id="eng_rp27_clar",
        case_id="case_rp27c",
        version=1,
        operational_status=OperationalStatus(code="pending_operator", steps_remaining=1, blocking=True),
        hitl_gate=HitlGate(required=True, reason="operator_clarification"),
        gaps=[GapItem(field="operator_decision", severity="blocking", ask_pl="Jaka moc pompy?")],
        actions=[],
        agent_memory=AgentMemory(),
    )
    store.insert_snapshot(snap)
    svc = AgentMcpService(settings=_settings(), store=store)

    missing = svc.approve_hitl_action(
        engagement_id="eng_rp27_clar",
        action_id="draft_reply",
        operator_id="op1",
    )
    assert missing.get("ok") is False
    assert "operator_answer" in str(missing.get("error") or "").lower() or "draft" in str(missing.get("error") or "").lower()

    ok = svc.approve_hitl_action(
        engagement_id="eng_rp27_clar",
        action_id="draft_reply",
        operator_id="op1",
        operator_answer_pl="12 kW",
    )
    assert ok.get("ok") is True, ok
    assert ok.get("clarification_answer_applied") is True
    loaded = store.load_snapshot("eng_rp27_clar")
    assert loaded is not None
    assert loaded.hitl_gate.required is False
    assert loaded.agent_memory.clarification_answers
    assert loaded.agent_memory.clarification_answers[0].answer_pl == "12 kW"
    assert loaded.agent_memory.clarification_answers[0].ask_pl == "Jaka moc pompy?"


def test_rp27_bridge_payload_and_send_use_operator_draft() -> None:
    row = {
        "engagement_id": "eng_rp27_send",
        "case_id": "case_s",
        "action_id": "draft_reply",
        "operator_id": "op1",
        "operator_draft_pl": "draft z bridge queue",
    }
    payload = agent_hitl_payload_from_row(row)
    assert payload["operator_draft_pl"] == "draft z bridge queue"

    snap = EngagementSnapshotV2(
        engagement_id="eng_rp27_send",
        case_id="case_s",
        version=1,
        operational_status=OperationalStatus(code="ready_for_quote", steps_remaining=0),
        hitl_gate=HitlGate(required=False, reason=""),
        actions=[ActionItem(id="draft_reply", enabled=True, payload_pl="stary")],
    )
    settings = MagicMock()
    out = execute_hitl_gmail_send(
        settings=settings,
        snapshot=snap,
        action_id="draft_reply",
        case_id="case_s",
        operator_id="op1",
        operator_draft_pl="draft z bridge queue",
    )
    expected = hashlib.sha256("draft z bridge queue".encode("utf-8")).hexdigest()[:16]
    assert out["draft_sha256"] == expected
    assert out.get("operator_draft_applied") is True


def test_rp27_dispatch_mcp_tool_forwards_operator_payloads() -> None:
    store = InMemoryOperatorEngagementStore()
    snap = EngagementSnapshotV2(
        engagement_id="eng_rp27_dispatch",
        case_id="case_rp27d",
        version=1,
        operational_status=OperationalStatus(code="pending_operator", steps_remaining=1, blocking=True),
        hitl_gate=HitlGate(required=True, reason="draft_reply"),
        actions=[ActionItem(id="draft_reply", enabled=True, payload_pl="stary")],
    )
    store.insert_snapshot(snap)
    svc = AgentMcpService(settings=_settings(), store=store)

    out = dispatch_mcp_tool(
        svc,
        "approve_hitl_action",
        {
            "engagement_id": "eng_rp27_dispatch",
            "action_id": "draft_reply",
            "operator_id": "op1",
            "operator_draft_pl": "draft przez MCP dispatch",
        },
    )
    assert out.get("ok") is True, out
    assert out.get("operator_draft_applied") is True
    loaded = store.load_snapshot("eng_rp27_dispatch")
    assert loaded is not None
    assert loaded.actions[0].payload_pl == "draft przez MCP dispatch"
