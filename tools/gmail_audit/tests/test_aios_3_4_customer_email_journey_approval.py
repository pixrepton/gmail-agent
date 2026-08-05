"""AI-OS Roadmap 3.4 — deterministic customer-email journey to HITL approval."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fixture_helpers import run_fixture

from agent_runtime.draft_lineage_transport import (
    build_upstream_draft_transport,
    materialize_transferred_draft_action,
)
from agent_runtime.mcp_service import AgentMcpService
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import InMemoryOperatorEngagementStore, build_initial_snapshot
from correlation_registry.service import CorrelationRegistryService
from correlation_registry.store import InMemoryCorrelationRegistryStore
from mailbox_memory_runtime import MailboxMemoryRuntime
from mailbox_memory_store import InMemoryMailboxMemoryStore
from outbound_receipt import build_ready_for_manual_send_receipt

FIXTURE_NAME = "post_offer_question"


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


def test_customer_email_fixture_journey_reaches_ready_for_manual_send(tmp_path: Path) -> None:
    bundle = run_fixture(FIXTURE_NAME)
    snapshot = bundle["snapshot"]
    intake_result = bundle["intake_result"]
    case_link_result = bundle["case_link_result"]
    reply_result = bundle["expected"]["reply_draft"]
    case_intelligence = bundle["case_intelligence"]

    registry_store = InMemoryCorrelationRegistryStore()
    registry_store.bootstrap()
    registry = CorrelationRegistryService(registry_store)
    runtime = MailboxMemoryRuntime(
        store=InMemoryMailboxMemoryStore(),
        blob_root=tmp_path / "blobs",
        stage_mode="live",
        correlation_registry=registry,
    )
    runtime.bootstrap()

    ingest = runtime.ingest_message(
        snapshot=snapshot,
        intake_result=intake_result,
        case_link_result=case_link_result,
    )
    assert ingest.enabled is True
    case_id = ingest.case_id
    source_message = snapshot.get("source_message") or {}
    signal_id = str(source_message.get("message_id") or "fixture-post-offer-001")

    lookup = registry.lookup_by_case_id(case_id)
    assert lookup is not None
    engagement_id = str(lookup.get("engagement_id") or "").strip()
    assert engagement_id

    op_store = InMemoryOperatorEngagementStore()
    understanding = build_initial_snapshot(
        case_id=case_id,
        engagement_id=engagement_id,
        trace_id=signal_id,
    )
    summary_pl = str(
        case_intelligence.get("case_summary_pl")
        or bundle["business_result"].get("business_interpretation")
        or "Klient pyta o szczegoly po ofercie."
    )
    understanding = apply_snapshot_delta(
        understanding,
        {
            "operational_status": {
                "code": "pending_operator",
                "steps_remaining": 2,
                "blocking": True,
            },
            "agent_memory": {
                "reasoning_trace": [{"turn": 1, "summary_pl": summary_pl}],
            },
        },
    )

    transport = build_upstream_draft_transport(
        reply_result=reply_result,
        case_id=case_id,
        source_signal_id=signal_id,
    )
    assert transport is not None
    draft_action = materialize_transferred_draft_action(transport)
    with_draft = apply_snapshot_delta(
        understanding,
        {
            "actions": [draft_action],
            "hitl_gate": {"required": True, "reason": "draft_ready_for_approval"},
            "operational_status": {"code": "pending_operator", "steps_remaining": 1, "blocking": True},
        },
    )
    op_store.insert_snapshot(with_draft)

    action = with_draft.actions[0]
    svc = AgentMcpService(settings=_settings(), store=op_store)
    approval = svc.approve_hitl_action(
        engagement_id=engagement_id,
        action_id=str(action.id or "draft_reply"),
        operator_id="op_aios_34",
        expected_body_hash=action.body_hash,
        expected_revision=action.revision,
    )
    assert approval.get("ok") is True, approval

    final = op_store.load_snapshot(engagement_id)
    assert final is not None
    assert final.hitl_gate.required is False
    assert final.communication_receipt.state == "ready_for_manual_send"
    assert final.communication_receipt.draft_id == action.draft_id
    assert final.communication_receipt.body_hash == action.body_hash
    assert final.communication_receipt.gmail_message_id == ""

    approved_action = next((item for item in final.actions if item.id == action.id), None)
    assert approved_action is not None
    assert approved_action.enabled is True
    assert approved_action.draft_id == action.draft_id
    assert approved_action.revision == action.revision
    assert approved_action.body_hash == action.body_hash

    assert ingest.case_id == case_id
    assert any(event.get("event_type") == "message_received" for event in runtime.store.fetch_events_for_case(case_id))


def test_approval_does_not_emit_communication_sent_receipt() -> None:
    """Approval closes HITL only; delivery receipt belongs to outbound intake (3.3)."""
    receipt = build_ready_for_manual_send_receipt(draft_id="draft_x", body_hash="hash_x")
    assert receipt["state"] == "ready_for_manual_send"
    assert receipt["gmail_message_id"] == ""
