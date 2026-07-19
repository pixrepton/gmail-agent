from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_routing import case_row_requires_action, desk_eligible, route_gmail_message
from cieplo_orchestrator_hook import (
    CIEPLO_DESK_INFO_BRIEF_PL,
    apply_cieplo_orchestrator_result,
    maybe_apply_cieplo_hook_from_os_event,
    orchestrator_status_for_event,
)
from correlation_registry.store import InMemoryCorrelationRegistryStore
from daszek_v3_operational_feed import build_feed_and_api_case_dict, build_operational_feed_from_mailbox_store
from mailbox_memory_store import InMemoryMailboxMemoryStore


def _lead_case_row(*, case_id: str = "cieplo-lead-1") -> dict:
    return {
        "case_id": case_id,
        "case_key": case_id,
        "subject": "Lead Cieplo",
        "status": "open",
        "case_family": "lead_opportunity",
        "updated_at": "2026-07-07T10:00:00+00:00",
        "metadata": {
            "priority_label": "P2 - ważne",
            "requires_action": True,
            "source_kind": "gmail_inbound",
            "export_case_type": "lead_oferta",
        },
    }


def test_orchestrator_status_for_terminal_events() -> None:
    assert orchestrator_status_for_event("cieplo.workflow.done", {}) == "ok"
    assert orchestrator_status_for_event("cieplo.workflow.failed", {}) == "failed"
    assert (
        orchestrator_status_for_event(
            "cieplo.workflow.state_changed",
            {"to_state": "FAILED_RETRYABLE"},
        )
        == "failed"
    )
    assert orchestrator_status_for_event("cieplo.workflow.pdf_ready", {}) is None


def test_apply_success_marks_informational_case() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    store.upsert_case(_lead_case_row())

    result = apply_cieplo_orchestrator_result("cieplo-lead-1", "ok", mailbox_store=store)
    assert result["ok"] is True
    assert result["requires_action"] is False
    assert result["desk_eligible"] is True
    assert result["orchestrator_status"] == "ok"

    row = store.fetch_case("cieplo-lead-1") or {}
    meta = row.get("metadata") or {}
    assert meta["source_kind"] == "cieplo_orchestrated"
    assert meta["orchestrator_status"] == "ok"
    assert meta["requires_action"] is False
    assert case_row_requires_action(row) is False
    assert desk_eligible(row) is True


def test_apply_failed_requires_action() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    store.upsert_case(_lead_case_row(case_id="cieplo-fail-1"))

    result = apply_cieplo_orchestrator_result("cieplo-fail-1", "failed", mailbox_store=store)
    assert result["ok"] is True
    assert result["requires_action"] is True
    assert result["orchestrator_status"] == "failed"

    row = store.fetch_case("cieplo-fail-1") or {}
    assert case_row_requires_action(row) is True
    assert row["metadata"]["orchestrator_status"] == "failed"


def test_apply_timeout_maps_to_failed_orchestrator_status() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    store.upsert_case(_lead_case_row(case_id="cieplo-timeout-1"))

    result = apply_cieplo_orchestrator_result("cieplo-timeout-1", "timeout", mailbox_store=store)
    assert result["ok"] is True
    assert result["requires_action"] is True
    assert result["orchestrator_status"] == "failed"

    row = store.fetch_case("cieplo-timeout-1") or {}
    assert row["metadata"]["orchestrator_status"] == "failed"
    assert case_row_requires_action(row) is True


def test_success_case_not_in_do_zrobienia_bucket() -> None:
    """Sprawy UI: requires_action=false → Informacyjne, not Do zrobienia."""
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    store.upsert_case(_lead_case_row(case_id="cieplo-bucket-1"))
    apply_cieplo_orchestrator_result("cieplo-bucket-1", "ok", mailbox_store=store)

    row = store.fetch_case("cieplo-bucket-1") or {}
    do_zrobienia = [row] if case_row_requires_action(row) else []
    informacyjne = [] if case_row_requires_action(row) else [row]
    assert not do_zrobienia
    assert len(informacyjne) == 1


def test_dozorca_operational_lead_not_noise() -> None:
    routing = route_gmail_message(
        subject="Nowy lead — zapytanie o ofertę pompy ciepła",
        snippet="",
        sender="Dozorca <dozorca@cieplo.app>",
        labels=["INBOX"],
        body="Lead z Cieplo.app — proszę o ofertę i wycenę instalacji.",
        has_attachment=False,
    )
    assert routing.export_case_type == "lead_oferta"
    assert routing.upsert_allowed is True


def test_dozorca_admin_noise_skips_upsert() -> None:
    routing = route_gmail_message(
        subject="Subskrypcja admin newsletter",
        snippet="",
        sender="Dozorca <dozorca@cieplo.app>",
        labels=["INBOX"],
        body="Powiadomienie systemowe o subskrypcji",
        has_attachment=False,
    )
    assert routing.export_case_type == "noise"
    assert routing.upsert_allowed is False


def test_os_event_done_resolves_case_by_message_id() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    store.upsert_case(_lead_case_row(case_id="case-msg-1"))
    store.upsert_message(
        {
            "message_id": "msg-cieplo-1",
            "case_id": "case-msg-1",
            "thread_id": "thread-1",
            "subject": "Lead",
            "sender": "lead@example.com",
            "received_at": "2026-07-07T10:00:00+00:00",
        }
    )

    hook = maybe_apply_cieplo_hook_from_os_event(
        event_type="cieplo.workflow.done",
        correlation={"message_id": "msg-cieplo-1"},
        mailbox_store=store,
    )
    assert hook["ok"] is True
    assert hook["requires_action"] is False

    row = store.fetch_case("case-msg-1") or {}
    assert row["metadata"]["source_kind"] == "cieplo_orchestrated"


def test_os_event_resolves_case_via_workflow_registry_link() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    store.upsert_case(_lead_case_row(case_id="case-wf-1"))

    registry = InMemoryCorrelationRegistryStore()
    registry.bootstrap()
    identity_id = registry.create_identity(email="lead@example.com")
    engagement_id = registry.resolve_or_create_engagement(identity_id=identity_id)
    registry.upsert_link(
        engagement_id=engagement_id,
        link_type="cieplo_workflow",
        target_id="wf-123",
        source_repo="cieplo-orchestrator",
    )
    registry.upsert_link(
        engagement_id=engagement_id,
        link_type="mailbox_case",
        target_id="case-wf-1",
        source_repo="gmail-agent",
    )

    hook = maybe_apply_cieplo_hook_from_os_event(
        event_type="cieplo.workflow.done",
        correlation={"workflow_id": "wf-123"},
        mailbox_store=store,
        registry_store=registry,
    )
    assert hook["ok"] is True
    assert hook["case_id"] == "case-wf-1"
    row = store.fetch_case("case-wf-1") or {}
    assert row["metadata"]["orchestrator_status"] == "ok"


def test_feed_copy_for_cieplo_informational_card() -> None:
    case_row = _lead_case_row(case_id="cieplo-feed-1")
    case_row["metadata"] = {
        **case_row["metadata"],
        "source_kind": "cieplo_orchestrated",
        "orchestrator_status": "ok",
        "requires_action": False,
    }
    feed_row = build_feed_and_api_case_dict(case_row, {"case_id": "cieplo-feed-1"})
    assert feed_row["operator_brief_pl"] == CIEPLO_DESK_INFO_BRIEF_PL
    assert feed_row["summary"] == CIEPLO_DESK_INFO_BRIEF_PL
    assert feed_row["source_kind"] == "cieplo_orchestrated"
    assert feed_row["requires_action"] is False


def test_cieplo_informational_case_emits_desk_card_in_feed() -> None:
    store = InMemoryMailboxMemoryStore()
    case_row = _lead_case_row(case_id="cieplo-desk-1")
    case_row["metadata"] = {
        **case_row["metadata"],
        "source_kind": "cieplo_orchestrated",
        "orchestrator_status": "ok",
        "requires_action": False,
        "priority_label": "P4 - informacja",
    }
    store.upsert_case(case_row)
    store.upsert_snapshot(
        "cieplo-desk-1",
        {"snapshot_json": {"status": "open", "summary_text": "Lead Cieplo", "recommended_next_action": ""}},
    )

    snap = build_operational_feed_from_mailbox_store(store, case_limit=5, task_limit=3, snapshot_id="cieplo-desk")
    desk = snap.get("feed", {}).get("desk", [])
    desk_for_case = [d for d in desk if isinstance(d, dict) and d.get("case_id") == "cieplo-desk-1"]
    assert desk_for_case, "Cieplo informational case should surface on Biurko feed.desk"
    card = desk_for_case[0]
    assert CIEPLO_DESK_INFO_BRIEF_PL in str(card.get("summary") or "")
    assert CIEPLO_DESK_INFO_BRIEF_PL in str(card.get("why_on_desk") or card.get("reason") or "")


# Phase 5 proof token (gate): CIEPLo_ORCHESTRATOR_ROUTING_PROOF_OK
