from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_intelligence.orchestrator import build_case_intelligence
from intake_shared_downstream import trust_case_link_from_context_pack
from understanding_output import _pending_outcome_gaps_pl, _prior_known_state_rows


def _snapshot(message_id: str = "m-current", body: str = "") -> dict:
    return {"source_message": {"message_id": message_id, "subject": "Re: oferta", "body": body}}


def _pack(case_id: str = "case_recovery_FU-01", facts: list[dict] | None = None) -> dict:
    return {
        "case_id": case_id,
        "active_facts": facts
        if facts is not None
        else [
            {
                "fact_key": "heated_area_m2",
                "value": 120,
                "status": "active",
                "source_ref": "prior-message",
            },
            {
                "fact_key": "offer_sent",
                "value": True,
                "status": "active",
                "source_ref": "prior-offer",
            },
        ],
    }


def test_trusted_context_pack_upgrades_empty_case_link_to_linked() -> None:
    result = trust_case_link_from_context_pack(
        case_link_result={"decision": "no_link", "confidence": 0.0, "candidates": []},
        mailbox_memory_result={"case_id": "case_recovery_FU-01"},
        context_pack=_pack(),
        snapshot=_snapshot(),
        intake_result={},
    )

    assert result["decision"] == "linked"
    assert result["case_id"] == "case_recovery_FU-01"
    assert result["selected_case_id"] == "case_recovery_FU-01"
    assert result["source"] == "context_candidate"
    assert "trusted_case_context_pack" in result["reasons"]


def test_trusted_context_pack_requires_prior_evidence_not_current_signal_only() -> None:
    result = trust_case_link_from_context_pack(
        case_link_result={"decision": "no_link", "confidence": 0.0, "candidates": []},
        mailbox_memory_result={"case_id": "case_new"},
        context_pack=_pack(
            "case_new",
            facts=[{"fact_key": "heated_area_m2", "value": 120, "status": "active", "source_ref": "m-current"}],
        ),
        snapshot=_snapshot("m-current"),
        intake_result={},
    )

    assert result["decision"] == "no_link"
    assert result.get("case_id", "") == ""


def test_trusted_context_pack_rejects_mismatched_case_and_weak_link() -> None:
    mismatched = trust_case_link_from_context_pack(
        case_link_result={"decision": "no_link", "confidence": 0.0, "candidates": []},
        mailbox_memory_result={"case_id": "case_other"},
        context_pack=_pack("case_recovery_FU-01"),
        snapshot=_snapshot(),
        intake_result={},
    )
    weak = trust_case_link_from_context_pack(
        case_link_result={"decision": "weak_link", "confidence": 0.55, "selected_case_key": "CASE-X"},
        mailbox_memory_result={"case_id": "case_recovery_FU-01"},
        context_pack=_pack("case_recovery_FU-01"),
        snapshot=_snapshot(),
        intake_result={},
    )

    assert mismatched["decision"] == "no_link"
    assert weak["decision"] == "weak_link"


def test_case_intelligence_trusts_known_facts_without_dropping_unrelated_gaps() -> None:
    case_link = trust_case_link_from_context_pack(
        case_link_result={"decision": "no_link", "confidence": 0.0, "candidates": []},
        mailbox_memory_result={"case_id": "case_recovery_FU-01"},
        context_pack=_pack(),
        snapshot=_snapshot(),
        intake_result={},
    )
    intelligence = build_case_intelligence(
        snapshot=_snapshot(),
        intake_result={
            "business_area": "sales",
            "priority": "medium",
            "case_assessment": {"case_family": "lead_opportunity"},
            "decision": {"action": "append_to_existing_case"},
        },
        case_link_result=case_link,
        business_result={
            "missing_information": [
                "Potwierdzenie, ktorej oferty dotyczy wiadomosc",
                "powierzchnia budynku",
                "numer telefonu kontaktowego",
                "adres instalacji",
            ],
            "business_interpretation": "Follow-up do istniejacej oferty.",
            "recommended_next_action": "reply",
            "urgency": "normal",
            "confidence": {"business_confidence": 0.8, "action_confidence": 0.8},
        },
        reply_result={},
        action_plan_result={"primary_action": "prepare_reply"},
        case_context_pack=_pack(),
    )
    flat = (
        intelligence["missing_info"]["critical"]
        + intelligence["missing_info"]["important"]
        + intelligence["missing_info"]["helpful"]
    )
    text = " | ".join(flat).lower()

    assert intelligence["case_understanding"]["case_id"] == "case_recovery_FU-01"
    assert intelligence["execution_metadata"]["input_case_link_decision"] == "linked"
    assert "oferty" not in text
    assert "powierzchn" not in text
    assert "telefon" in text
    assert "adres" in text


def test_known_heating_source_does_not_answer_removal_scope_gap() -> None:
    case_link = trust_case_link_from_context_pack(
        case_link_result={"decision": "no_link", "confidence": 0.0, "candidates": []},
        mailbox_memory_result={"case_id": "case_recovery_MI-02"},
        context_pack=_pack(
            "case_recovery_MI-02",
            facts=[
                {"fact_key": "current_heating_source", "value": "stary piec", "status": "active", "source_ref": "prior"},
                {"fact_key": "offer_sent", "value": True, "status": "active", "source_ref": "prior"},
            ],
        ),
        snapshot=_snapshot(),
        intake_result={},
    )
    intelligence = build_case_intelligence(
        snapshot=_snapshot(),
        intake_result={
            "business_area": "sales",
            "priority": "medium",
            "case_assessment": {"case_family": "lead_opportunity"},
            "decision": {"action": "append_to_existing_case"},
        },
        case_link_result=case_link,
        business_result={
            "missing_information": [
                "czy wywoz starego pieca jest objety cena oferty",
                "identyfikator oryginalnej oferty / numer oferty",
            ],
            "business_interpretation": "Follow-up do oferty.",
            "recommended_next_action": "reply",
            "urgency": "normal",
        },
        reply_result={},
        action_plan_result={"primary_action": "prepare_reply"},
        case_context_pack=_pack(
            "case_recovery_MI-02",
            facts=[
                {"fact_key": "current_heating_source", "value": "stary piec", "status": "active", "source_ref": "prior"},
                {"fact_key": "offer_sent", "value": True, "status": "active", "source_ref": "prior"},
            ],
        ),
    )
    flat = (
        intelligence["missing_info"]["critical"]
        + intelligence["missing_info"]["important"]
        + intelligence["missing_info"]["helpful"]
    )
    text = " | ".join(flat).lower()

    assert "wywoz starego pieca" in text
    assert "identyfikator" not in text


def test_prior_known_state_rejects_superseded_and_conflicting_facts() -> None:
    rows = _prior_known_state_rows(
        {
            "active_facts": [
                {"fact_key": "heated_area_m2", "value": 120, "status": "superseded", "source_ref": "old"},
                {"fact_key": "city", "value": "Krakow", "status": "active", "source_ref": "p1"},
                {"fact_key": "city", "value": "Warszawa", "status": "active", "source_ref": "p2"},
                {"fact_key": "scope", "value": "ogrzewanie", "status": "active", "source_ref": "p3"},
            ],
            "conflicting_facts": [{"fact_key": "city"}],
        }
    )
    keys = {row["fact_key"] for row in rows}

    assert "heated_area_m2" not in keys
    assert "city" not in keys
    assert "scope" in keys


def test_pending_outcome_gaps_are_suppressed_only_by_current_resolution_signal() -> None:
    doc_rows = [{"fact_key": "requested_document", "value": "dowod_osobisty"}]
    visit_rows = [{"fact_key": "agreed_visit_date", "value": "2026-07-23T00:00:00+02:00"}]

    assert not _pending_outcome_gaps_pl(
        doc_rows,
        attachment_intelligence={"attachments": [{"file_name": "id_scan.pdf"}]},
    )
    assert _pending_outcome_gaps_pl(doc_rows, attachment_intelligence={"attachments": []})
    assert not _pending_outcome_gaps_pl(
        visit_rows,
        snapshot=_snapshot(body="Czy mozemy przelozyc czwartkowa wizje lokalna na piatek?"),
    )
    assert _pending_outcome_gaps_pl(visit_rows, snapshot=_snapshot(body="Potwierdzam oferte."))
