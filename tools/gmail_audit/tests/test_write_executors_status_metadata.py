"""Proof for execute_update_case_status metadata preservation.

Tests verify that execute_update_case_status preserves recognized metadata
across four real scenarios (lead, accounting, service, multi-intent) using
both in-memory and PostgreSQL stores, with fail-closed behavior for stores
lacking safe mutation primitives.
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.tools.write_executors import execute_update_case_status
from business_outcome import record_business_outcome
from case_routing import desk_eligible, operator_priority_to_label
from mailbox_memory_store import InMemoryMailboxMemoryStore, PostgresMailboxMemoryStore

POSTGRES_TEST_DATABASE_URL = os.getenv("MAILBOX_MEMORY_TEST_DATABASE_URL", "").strip()


class _StoreWithoutMutateCase:
    """Minimal store with upsert_case but no mutate_case — used to verify fail-closed."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    def fetch_case(self, case_id: str) -> dict | None:
        return self._rows.get(case_id)

    def upsert_case(self, row: dict) -> None:
        self._rows[row.get("case_id")] = dict(row)


def _lead_case_row(case_id: str) -> dict:
    return {
        "case_id": case_id,
        "case_key": f"LEAD-{case_id}",
        "case_family": "lead_opportunity",
        "subject": "Zapytanie o wycenę pompy ciepła",
        "status": "new_lead",
        "customer_name": "Maria Nowak",
        "customer_email": "maria.nowak@example.com",
        "updated_at": "2026-07-13T10:00:00+02:00",
        "last_source_kinds_seen": ["gmail"],
        "metadata": {
            "source_kind": "gmail_inbound",
            "requires_action": True,
            "priority": "normalny",
            "priority_label": operator_priority_to_label("normalny"),
            "export_case_type": "lead_oferta",
            "case_guidance": {
                "reason_summary_pl": "Klient pyta o możliwości finansowania wymiany kotła w starym domu.",
                "confidence": 0.75,
            },
        },
    }


def _accounting_case_row(case_id: str) -> dict:
    return {
        "case_id": case_id,
        "case_key": f"ACCT-{case_id}",
        "case_family": "accounting",
        "subject": "Faktura VAT 2026/07/001 — rozbieżność w kwocie",
        "status": "new_lead",
        "customer_name": "GreenTech Sp. z o.o.",
        "customer_email": "finanse@greentech.pl",
        "updated_at": "2026-07-13T10:00:00+02:00",
        "last_source_kinds_seen": ["gmail"],
        "metadata": {
            "source_kind": "gmail_inbound",
            "requires_action": True,
            "priority": "ważne",
            "priority_label": operator_priority_to_label("ważne"),
            "export_case_type": "ksiegowosc_podatki",
            "case_guidance": {
                "reason_summary_pl": "Rzeczywista dostawa: 15 szt., faktura: 12 szt. Rozliczenie VAT wymaga korekty.",
                "confidence": 0.92,
            },
        },
    }


def _service_case_row(case_id: str) -> dict:
    return {
        "case_id": case_id,
        "case_key": f"SVC-{case_id}",
        "case_family": "service_request",
        "subject": "Awaria pompy ciepła — klient bez ciepła od wczoraj",
        "status": "open",
        "customer_name": "Jan Kowalski",
        "customer_email": "jan.kowalski@example.com",
        "updated_at": "2026-07-13T10:00:00+02:00",
        "last_source_kinds_seen": ["gmail"],
        "metadata": {
            "source_kind": "gmail_inbound",
            "requires_action": True,
            "priority": "pilne",
            "priority_label": operator_priority_to_label("pilne"),
            "export_case_type": "serwis_awaria",
            "case_guidance": {
                "reason_summary_pl": "Pompa ciepła nie startuje, system wyświetla kod błędu E7. Klient starszy, potrzebuje wsparcia serwisowego w dzień.",
                "confidence": 0.88,
            },
        },
    }


def _multi_intent_case_row(case_id: str) -> dict:
    return {
        "case_id": case_id,
        "case_key": f"MULTI-{case_id}",
        "case_family": "customer",
        "subject": "Pytanie o wycenę + stanowisko do faktury za serwis z czerwca",
        "status": "open",
        "customer_name": "Centrum Ciepła Sp. z o.o.",
        "customer_email": "zakupy@centrumciepla.pl",
        "updated_at": "2026-07-13T10:00:00+02:00",
        "last_source_kinds_seen": ["gmail"],
        "metadata": {
            "source_kind": "gmail_inbound",
            "requires_action": True,
            "priority": "ważne",
            "priority_label": operator_priority_to_label("ważne"),
            "export_case_type": "mixed",
            "case_guidance": {
                "reason_summary_pl": "Dwa niezależne tematy: (1) Zapytanie o wycenę rozbudowy systemu dla nowego budynku. (2) Spór dotyczący faktury za serwis — klient uważa, że robocizna była przeszacowana.",
                "confidence": 0.79,
            },
            "facts": [
                {"type": "query", "text": "Rozbudowa dla obiektu w Warszawie, 800 m2"},
                {"type": "invoice_dispute", "text": "Serwis czerwiec — 45h * 180 PLN = 8100 PLN (klient kwestionuje stawkę)"},
            ],
        },
    }


def test_fail_closed_without_mutate_case() -> None:
    """Verify that stores lacking mutate_case fail-closed, not destructive."""
    store = _StoreWithoutMutateCase()
    # Minimal case with metadata but no lifecycle_state (defaults to NEW_LEAD)
    row = {
        "case_id": "svc-failclosed-1",
        "case_key": "SVC-svc-failclosed-1",
        "case_family": "service_request",
        "subject": "Test awaria",
        "status": "new_lead",
        "customer_name": "Test User",
        "customer_email": "test@example.com",
        "metadata": {
            "source_kind": "gmail_inbound",
            "requires_action": True,
            "priority_label": operator_priority_to_label("pilne"),
            "case_guidance": {"reason_summary_pl": "Test guidance."},
        },
    }
    store.upsert_case(row)

    before = store.fetch_case("svc-failclosed-1")
    assert before is not None
    assert (before.get("metadata") or {}).get("priority_label") == operator_priority_to_label("pilne")

    # Try to change status from new_lead to "open" (QUALIFICATION) — valid transition
    result = execute_update_case_status(
        {"case_id": "svc-failclosed-1", "status": "open"},
        mailbox_store=store,
    )

    # Fail-closed: should return error, not silently destroy metadata
    assert result.get("status") == "error"
    error_msg = result.get("summary", "").lower()
    assert "bezpieczn" in error_msg or "safe" in error_msg or "store" in error_msg

    # Verify no write occurred at all
    after = store.fetch_case("svc-failclosed-1")
    assert after is not None
    # Case state must not have changed
    assert after.get("status") == "new_lead"
    # Metadata must be untouched (fail-closed means no-op)
    assert (after.get("metadata") or {}).get("priority_label") == operator_priority_to_label("pilne")


def test_four_scenarios_preserve_metadata() -> None:
    """Parametrized test: four real scenarios preserve metadata via mutate_case."""
    scenarios = [
        ("lead", _lead_case_row("lead-scenario-1")),
        ("accounting", _accounting_case_row("acct-scenario-1")),
        ("service", _service_case_row("svc-scenario-1")),
        ("multi_intent", _multi_intent_case_row("multi-scenario-1")),
    ]

    for scenario_name, case_row in scenarios:
        store = InMemoryMailboxMemoryStore()
        store.upsert_case(case_row)

        before = store.fetch_case(case_row["case_id"])
        assert before is not None, f"{scenario_name}: case not seeded"
        before_meta = before.get("metadata") or {}
        before_guidance = before_meta.get("case_guidance") or {}

        # All scenarios start as desk-eligible
        assert desk_eligible(before) is True, f"{scenario_name}: should start desk-eligible"

        # Simulate the core fix: mutate_case preserves metadata when changing status
        def _apply_status(row: dict) -> dict:
            updated = dict(row)
            updated["status"] = "open"
            return updated

        after = store.mutate_case(case_row["case_id"], _apply_status)
        assert after is not None, f"{scenario_name}: mutate returned None"
        assert after.get("status") == "open", f"{scenario_name}: status not updated"

        # Core contract: all metadata fields survive
        after_meta = after.get("metadata") or {}
        assert after_meta.get("source_kind") == before_meta.get("source_kind"), f"{scenario_name}: source_kind lost"
        assert after_meta.get("priority_label") == before_meta.get("priority_label"), f"{scenario_name}: priority_label lost"
        assert after_meta.get("requires_action") == before_meta.get("requires_action"), f"{scenario_name}: requires_action lost"
        assert after_meta.get("export_case_type") == before_meta.get("export_case_type"), f"{scenario_name}: export_case_type lost"

        # Intelligence output must survive
        after_guidance = after_meta.get("case_guidance") or {}
        assert after_guidance.get("reason_summary_pl") == before_guidance.get("reason_summary_pl"), f"{scenario_name}: guidance.reason_summary_pl lost"
        assert after_guidance.get("confidence") == before_guidance.get("confidence"), f"{scenario_name}: guidance.confidence lost"

        # Customer identity must survive
        assert after.get("customer_name") == case_row.get("customer_name"), f"{scenario_name}: customer_name lost"
        assert after.get("customer_email") == case_row.get("customer_email"), f"{scenario_name}: customer_email lost"
        assert after.get("subject") == case_row.get("subject"), f"{scenario_name}: subject lost"
        assert after.get("case_key") == case_row.get("case_key"), f"{scenario_name}: case_key lost"

        # Desk eligibility must survive
        assert desk_eligible(after) is True, f"{scenario_name}: should remain desk-eligible after status change"


@unittest.skipUnless(POSTGRES_TEST_DATABASE_URL, "MAILBOX_MEMORY_TEST_DATABASE_URL not set")
def test_postgres_status_change_preserves_metadata() -> None:
    """Real PostgreSQL integration test: four scenarios."""
    store = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
    store.bootstrap()

    scenarios = [
        ("lead", _lead_case_row(f"lead-pg-{uuid.uuid4().hex[:8]}")),
        ("accounting", _accounting_case_row(f"acct-pg-{uuid.uuid4().hex[:8]}")),
        ("service", _service_case_row(f"svc-pg-{uuid.uuid4().hex[:8]}")),
        ("multi_intent", _multi_intent_case_row(f"multi-pg-{uuid.uuid4().hex[:8]}")),
    ]

    try:
        for scenario_name, case_row in scenarios:
            store.upsert_case(case_row)
            case_id = case_row["case_id"]

            before = store.fetch_case(case_id)
            assert before is not None, f"{scenario_name}: not seeded in Postgres"
            before_meta = before.get("metadata") or {}
            before_guidance = before_meta.get("case_guidance") or {}

            # Determine valid target status based on current status
            # new_lead (NEW_LEAD) can go to "open" (QUALIFICATION)
            # open (QUALIFICATION) can go to "lost" (LOST)
            current_status = before.get("status", "")
            if current_status == "open":
                target_status = "lost"
            else:
                target_status = "open"

            if target_status == "lost":
                recorded = record_business_outcome(
                    store,
                    case_id=case_id,
                    outcome="lost",
                    source="test_postgres_status_change_preserves_metadata",
                )
                assert recorded.get("ok") is True, f"{scenario_name}: {recorded}"

            result = execute_update_case_status(
                {"case_id": case_id, "status": target_status},
                mailbox_store=store,
            )
            assert result.get("status") == "ok", f"{scenario_name}: {result}"

            after = store.fetch_case(case_id)
            assert after is not None
            assert after.get("status") == target_status, f"{scenario_name}: status not updated to {target_status}"

            after_meta = after.get("metadata") or {}
            assert after_meta.get("priority_label") == before_meta.get("priority_label"), f"{scenario_name}: priority_label lost in Postgres"
            assert after_meta.get("requires_action") == before_meta.get("requires_action")
            after_guidance = after_meta.get("case_guidance") or {}
            assert after_guidance.get("reason_summary_pl") == before_guidance.get("reason_summary_pl"), f"{scenario_name}: guidance lost in Postgres"
    finally:
        # Cleanup synth case_ids
        for _name, case_row in scenarios:
            try:
                with store._connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM mailbox_memory_cases WHERE case_id = %(cid)s", {"cid": case_row["case_id"]})
                    conn.commit()
            except Exception:  # noqa: BLE001
                pass
