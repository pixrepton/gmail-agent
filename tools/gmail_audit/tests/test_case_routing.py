from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_routing import route_from_classification, route_gmail_message
from mail_classification import classify_message


def test_zus_message_routes_to_accounting_with_requires_action() -> None:
    body = (
        "DzieĹ„ dobry,\n\nZUS za kwiecieĹ„: 2 237,12 zĹ‚\nPIT-4: 2 430,00 zĹ‚\n\n"
        "Markas Sp.z o.o.\nMarta JeleĹ„\nTel. 602-433-538"
    )
    routing = route_gmail_message(
        subject="ZUS",
        snippet="",
        sender='"Marta JeleĹ„" <marta.jelen@markasbiuro.pl>',
        labels=["INBOX"],
        body=body,
        has_attachment=False,
    )
    assert routing.export_case_type == "ksiegowosc_podatki"
    assert routing.case_family == "accounting"
    assert routing.requires_action is True
    assert routing.upsert_allowed is True


def test_noise_classification_skips_upsert() -> None:
    routing = route_gmail_message(
        subject="Newsletter promocja rabat",
        snippet="wypisz siÄ™ z listy",
        sender="newsletter@noreply.example.com",
        labels=["INBOX"],
        body="Black Friday outlet marketing webinar",
        has_attachment=False,
    )
    assert routing.export_case_type == "noise"
    assert routing.upsert_allowed is False


def test_unknown_low_value_reference_only() -> None:
    routing = route_from_classification(
        {
            "case_type": "unknown_low_value",
            "priority_label": "pomijany",
            "is_task": False,
            "priority_reasons": [],
        }
    )
    assert routing.export_case_type == "unknown_low_value"
    assert routing.case_family == "reference_only"
    assert routing.requires_action is False
    assert routing.upsert_allowed is True


def test_manual_task_routing() -> None:
    from case_routing import classify_mailbox_row, operator_priority_to_label

    routing = classify_mailbox_row("internal_task", "manual", "internal_task")
    assert routing.case_family == "operations"
    assert routing.source_kind == "manual"
    assert routing.requires_action is True
    assert routing.desk_eligible is True  # default normalny â†’ P2

    routing_p1 = classify_mailbox_row(
        "operations",
        "manual",
        "operations",
        classification={"priority_label": operator_priority_to_label("pilne")},
    )
    assert routing_p1.desk_eligible is True


def test_enrich_preserves_existing_routing_on_stamp() -> None:
    from case_routing import enrich_case_row_before_upsert

    row = {
        "case_id": "case-stamp",
        "case_family": "accounting",
        "metadata": {
            "requires_action": True,
            "source_kind": "gmail_inbound",
            "export_case_type": "ksiegowosc_podatki",
            "priority_label": "P2 - waĹĽne",
        },
    }
    enriched, routing = enrich_case_row_before_upsert(row, source_kind="gmail")
    assert enriched["metadata"]["export_case_type"] == "ksiegowosc_podatki"
    assert routing.requires_action is True
    assert enriched["metadata"]["source_kind"] == "gmail_inbound"


def test_enrich_orchestrator_status_overrides_existing_requires_action() -> None:
    from case_routing import enrich_case_row_before_upsert

    row = {
        "case_id": "cieplo-enrich",
        "case_family": "lead_opportunity",
        "metadata": {
            "requires_action": True,
            "source_kind": "gmail_inbound",
            "export_case_type": "lead_oferta",
            "priority_label": "P2 - waĹĽne",
        },
    }
    enriched, routing = enrich_case_row_before_upsert(
        row,
        source_kind="cieplo_orchestrated",
        orchestrator_status="ok",
    )
    assert routing.requires_action is False
    assert routing.source_kind == "cieplo_orchestrated"
    assert enriched["metadata"]["orchestrator_status"] == "ok"
    assert enriched["metadata"]["requires_action"] is False


def test_zus_export_fixture_case_id() -> None:
    routing = route_gmail_message(
        subject="ZUS",
        snippet="",
        sender="accounting@example.test",
        labels=["INBOX"],
        body="Syntetyczny komunikat ksiegowy: ZUS, PIT, termin platnosci.",
        has_attachment=False,
    )
    assert routing.case_family == "accounting"


def test_export_script_uses_shared_classify_message() -> None:
    scripts_dir = Path(__file__).resolve().parents[4] / "scripts"
    export_path = scripts_dir / "export_bootstrap_emails.py"
    source = export_path.read_text(encoding="utf-8")
    assert "from mail_classification import classify_message" in source
    assert "def classify_message(" not in source

