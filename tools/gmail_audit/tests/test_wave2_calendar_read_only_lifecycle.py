from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from calendar_runtime import CalendarRuntime, build_calendar_event_action_proposal
from execution_runtime import approve_action_proposal, create_action_proposal, execute_action_proposal
from mailbox_memory_store import InMemoryMailboxMemoryStore
from reply_drafter import _draft_case_state


def _store_with_case() -> InMemoryMailboxMemoryStore:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case(
        {
            "case_id": "case-cal-1",
            "case_key": "case-cal-1",
            "subject": "Serwis pompy",
            "status": "open",
            "case_family": "lead_opportunity",
            "customer_name": "Anna Klient",
            "customer_email": "anna@example.com",
            "updated_at": "2026-07-31T08:00:00+00:00",
        }
    )
    return store


def _settings() -> SimpleNamespace:
    return SimpleNamespace(google_calendar_id="primary")


def _event(event_id: str = "evt-1", *, status: str = "confirmed", summary: str = "Wizyta Anna Klient") -> dict:
    return {
        "id": event_id,
        "status": status,
        "summary": summary,
        "attendees": [{"email": "anna@example.com"}],
        "start": {"dateTime": "2026-08-03T09:00:00+02:00"},
        "end": {"dateTime": "2026-08-03T10:00:00+02:00"},
    }


def test_calendar_ingest_uses_registered_calendar_source_kind_and_is_idempotent() -> None:
    store = _store_with_case()
    client = MagicMock()
    client.list_events.return_value = [_event(summary="Pierwszy termin")]

    result = CalendarRuntime(settings=_settings(), store=store, client=client).ingest_events(dry_run=False)

    assert result["ok"] is True
    assert result["events"][0]["case_id"] == "case-cal-1"
    assert len(store.calendar_events) == 1
    assert list(store.signals.values())[-1]["source_kind"] == "calendar"

    client.list_events.return_value = [_event(summary="Zmieniony termin")]
    CalendarRuntime(settings=_settings(), store=store, client=client).ingest_events(dry_run=False)

    assert len(store.calendar_events) == 1
    assert store.calendar_events["evt-1"]["summary"] == "Zmieniony termin"
    context = CalendarRuntime(settings=_settings(), store=store, client=client).context_for_case("case-cal-1")
    assert context["visit_lifecycle"] == "scheduled_visit"
    assert context["calendar_risk"] == "calendar_event_exists"


def test_calendar_cancel_observed_externally_removes_scheduled_visit_projection() -> None:
    store = _store_with_case()
    client = MagicMock()
    runtime = CalendarRuntime(settings=_settings(), store=store, client=client)

    client.list_events.return_value = [_event()]
    runtime.ingest_events(dry_run=False)
    client.list_events.return_value = [_event(status="cancelled")]
    runtime.ingest_events(dry_run=False)

    context = runtime.context_for_case("case-cal-1")
    assert context["events"] == []
    assert context["has_calendar_event"] is False
    assert context["visit_lifecycle"] == "no_calendar_event"


def test_missing_calendar_event_id_is_rejected_before_signal_append() -> None:
    store = _store_with_case()
    client = MagicMock()
    client.list_events.return_value = [_event(event_id="")]

    result = CalendarRuntime(settings=_settings(), store=store, client=client).ingest_events(dry_run=False)

    assert result["ok"] is True
    assert result["errors"] == 1
    assert store.calendar_events == {}
    assert store.signals == {}


def test_proposed_visit_fact_does_not_confirm_scheduled_visit_without_calendar_event_id() -> None:
    context_bundle = {
        "case_context_pack": {
            "active_facts": [
                {"fact_key": "scheduled_visit", "normalized_value": "Date: 2026-08-03"},
                {"fact_key": "proposed_visit", "normalized_value": "2026-08-03"},
            ]
        }
    }
    assert _draft_case_state({}, {}, context_bundle)["visit_confirmed"] is False

    context_bundle["case_context_pack"]["active_facts"][0]["metadata"] = {"calendar_event_id": "evt-1"}
    assert _draft_case_state({}, {}, context_bundle)["visit_confirmed"] is True


def test_calendar_write_action_is_fail_closed_even_after_owner_approval() -> None:
    store = _store_with_case()
    proposal = create_action_proposal(
        store,
        {
            "case_id": "case-cal-1",
            "action_type": "create_calendar_event",
            "payload": {"summary": "Nie wolno zapisac"},
            "requires_review": True,
        },
    )
    approve_action_proposal(store, proposal.proposal_id, approved_by="konrad", reason="test")

    result = execute_action_proposal(store, proposal.proposal_id, executed_by="konrad", dry_run=False, calendar_client=MagicMock())

    assert result.execution_status == "blocked"
    assert result.error_code == "policy_blocked"
    assert "calendar_write_disabled_read_only" in result.policy_result["policy_basis"]


def test_dead_create_calendar_event_proposal_builder_is_tombstoned() -> None:
    store = _store_with_case()
    try:
        build_calendar_event_action_proposal(store=store, case_id="case-cal-1", payload={})
    except RuntimeError as exc:
        assert "calendar_event_action_proposal_disabled_read_only" in str(exc)
    else:
        raise AssertionError("create_calendar_event proposal builder must be fail-closed")
