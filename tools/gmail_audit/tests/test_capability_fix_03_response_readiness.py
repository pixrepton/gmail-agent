from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_intelligence import build_case_intelligence
from understanding_output import build_understanding_output


def _snapshot(case_id: str, subject: str, body: str) -> dict:
    return {
        "summary_text": f"{subject} {body}",
        "source_message": {
            "message_id": f"capfix03_{case_id}",
            "subject": subject,
            "body": body,
            "sender": "client@example.invalid",
            "date": "2026-08-09T00:00:00Z",
        },
    }


def _intake(
    *,
    case_id: str,
    business_area: str,
    case_family: str,
    priority: str = "medium",
    action: str = "create_case",
    review_required: bool = False,
    review_flags: list[str] | None = None,
    reason: str = "",
    extracted_data: dict | None = None,
) -> dict:
    return {
        "message_id": f"capfix03_{case_id}",
        "business_area": business_area,
        "priority": priority,
        "reason": reason,
        "decision": {"action": action, "action_rationale": reason or "case should progress"},
        "review": {"required": review_required, "flags": review_flags or []},
        "review_required": review_required,
        "thread": {"thread_id": f"thread_{case_id}"},
        "case_assessment": {
            "case_family": case_family,
            "is_new_case": action in {"create_case", "create_case_and_task"},
            "state_detected": "new_inquiry" if business_area == "sales" else "open",
            "state_change": {"detected": False},
        },
        "confidence": {
            "case_link_confidence": 0.2,
            "decision_confidence": 0.82,
            "extraction_confidence": 0.82,
        },
        "extracted_data": extracted_data or {},
    }


def _business(
    *,
    area: str,
    interpretation: str,
    missing: list[str],
    next_action: str = "collect_data",
    urgency: str = "normal",
) -> dict:
    return {
        "business_area": area,
        "business_interpretation": interpretation,
        "business_summary_short": interpretation[:160],
        "customer_state_guess": "active_case" if area == "service" else "new_lead",
        "recommended_next_action": next_action,
        "recommended_action_reason": interpretation,
        "missing_information": missing,
        "risks": [],
        "urgency": urgency,
        "confidence": {"business_confidence": 0.82, "action_confidence": 0.78},
    }


def _run(spec: dict) -> tuple[dict, dict]:
    snap = _snapshot(spec["case_id"], spec["subject"], spec["body"])
    ci = build_case_intelligence(
        snapshot=snap,
        intake_result=spec["intake"],
        case_link_result=spec.get("case_link", {"decision": "no_link", "confidence": 0.0}),
        business_result=spec["business"],
        reply_result=spec.get("reply", {}),
        action_plan_result=spec.get("action_plan", {"primary_action": "create_task", "confidence": 0.75}),
        thread_memory=spec.get("thread_memory", {}),
        case_context_pack=spec.get("case_context_pack", {}),
        attachment_intelligence=spec.get("attachment_intelligence", {}),
    )
    uo = build_understanding_output(
        snapshot=snap,
        intake_result=spec["intake"],
        case_link_result=spec.get("case_link", {"decision": "no_link", "confidence": 0.0}),
        business_result=spec["business"],
        intelligence=ci,
        thread_memory=spec.get("thread_memory", {}),
        case_context_pack=spec.get("case_context_pack", {}),
        attachment_intelligence=spec.get("attachment_intelligence", {}),
    )
    return ci, uo


TARGET_CASES = [
    {
        "case_id": "INT-01",
        "subject": "Zapytanie o pompe ciepla - dom 150m2 Wroclaw",
        "body": "Buduje dom jednorodzinny 150m2 pod Wroclawiem i chce kupic pompe ciepla.",
        "intake": _intake(
            case_id="INT-01",
            business_area="sales",
            case_family="lead_opportunity",
            action="review",
            review_required=True,
            reason="Lead 150m2 dom jednorodzinny Wroclaw pompa ciepla.",
            extracted_data={"heated_area_m2": 150, "building_type": "dom", "raw_geographic_signal": "Wroclaw"},
        ),
        "business": _business(
            area="lead",
            interpretation="Nowy actionable lead: dom 150m2 we Wroclawiu, zakup pompy ciepla.",
            missing=["dokladny adres instalacji", "OZC", "preferowana marka", "pelny harmonogram"],
            next_action="escalate_review",
        ),
        "action_plan": {"primary_action": "create_review", "confidence": 0.72},
        "later_markers": ["adres", "OZC", "marka", "harmonogram"],
        "next_step_marker": "Lead:",
    },
    {
        "case_id": "INT-04",
        "subject": "Reklamacja - pompa ciepla halasuje",
        "body": "Zglaszam reklamacje, jednostka zewnetrzna glosno stuka od tygodnia.",
        "intake": _intake(
            case_id="INT-04",
            business_area="service",
            case_family="service",
            priority="high",
            action="create_case",
            reason="Service complaint, glosny halas pompy.",
        ),
        "business": _business(
            area="service",
            interpretation="Reklamacja serwisowa: ciagly halas po montazu.",
            missing=["numer telefonu", "adres instalacji", "model i numer seryjny", "numer umowy"],
            next_action="escalate_review",
            urgency="high",
        ),
        "later_markers": ["telefon", "adres", "model", "umowy"],
        "next_step_marker": "Serwis:",
    },
    {
        "case_id": "NEW-03",
        "subject": "Wycena pompy ciepla - wymiana gazu",
        "body": "Dom 150m2, Wroclaw, ogrzewanie gazowe, budzet okolo 45000 PLN.",
        "intake": _intake(
            case_id="NEW-03",
            business_area="sales",
            case_family="lead_opportunity",
            action="review",
            review_required=True,
            reason="Lead 150m2 Wroclaw gaz budzet 45000 PLN.",
            extracted_data={
                "heated_area_m2": 150,
                "current_heating_source": "gaz",
                "budget_pln_estimated": 45000,
                "raw_geographic_signal": "Wroclaw",
            },
        ),
        "business": _business(
            area="lead",
            interpretation="Nowy actionable lead z metrazem, gazem, budzetem i lokalizacja.",
            missing=["pelny adres", "OZC", "rodzaj odbiornikow", "CWU", "rok budowy"],
            next_action="escalate_review",
        ),
        "action_plan": {"primary_action": "create_review", "confidence": 0.72},
        "later_markers": ["adres", "OZC", "odbiornik", "CWU", "rok"],
        "next_step_marker": "Lead:",
    },
    {
        "case_id": "SVC-01",
        "subject": "Reklamacja - instalacja nie dziala poprawnie",
        "body": "Instalacja pompy ciepla nie dziala poprawnie, temperatura spada od tygodnia.",
        "intake": _intake(
            case_id="SVC-01",
            business_area="service",
            case_family="service",
            priority="high",
            action="create_case",
            reason="Service complaint: instalacja nie dziala poprawnie.",
        ),
        "business": _business(
            area="service",
            interpretation="Reklamacja po montazu, instalacja dziala nieprawidlowo.",
            missing=["dokladny adres instalacji", "numer telefonu", "model pompy", "szczegolowy opis objawow"],
            next_action="escalate_review",
            urgency="high",
        ),
        "later_markers": ["adres", "telefon", "model", "objaw"],
        "next_step_marker": "Serwis:",
    },
    {
        "case_id": "SVC-04",
        "subject": "Pytanie o dziwny dzwiek",
        "body": "Od czasu do czasu jednostka zewnetrzna lekko klika, ale dziala normalnie. Nie ma pospiechu.",
        "intake": _intake(
            case_id="SVC-04",
            business_area="service",
            case_family="service",
            priority="low",
            action="create_case",
            reason="Low urgency service question about sporadic sound.",
        ),
        "business": _business(
            area="service",
            interpretation="Niepilne pytanie serwisowe o sporadyczny dzwiek, bez awarii.",
            missing=["model i numer seryjny", "warunki wystepowania dzwieku", "potwierdzenie pilnosci"],
            next_action="collect_data",
            urgency="low",
        ),
        "later_markers": ["model", "dzwieku", "pilnosci"],
        "next_step_marker": "Serwis:",
        "expected_urgency": "low",
        "expected_action": "answer_customer",
    },
    {
        "case_id": "DOC-02",
        "subject": "Karta katalogowa urzadzenia",
        "body": "Przesylam karte katalogowa pieca, prosze sprawdzic kompatybilnosc z pompa ciepla.",
        "intake": _intake(
            case_id="DOC-02",
            business_area="sales",
            case_family="lead_opportunity",
            action="create_case_and_task",
            reason="Technical compatibility question with document.",
        ),
        "business": _business(
            area="lead",
            interpretation="Pytanie techniczne o kompatybilnosc istniejacego pieca i instalacji z pompa ciepla.",
            missing=["adres instalacji", "numer telefonu", "powierzchnia domu", "OZC", "termin realizacji"],
            next_action="collect_data",
        ),
        "later_markers": ["adres", "telefon", "powierzchnia", "OZC"],
        "next_step_marker": "Techniczne:",
    },
    {
        "case_id": "MI-01",
        "subject": "Dwie sprawy",
        "body": (
            "Po pierwsze: czy Aquarea dziala z grzejnikami? Po drugie: w drugim domu, "
            "adres ul. Kwiatowa 5, pompa przestala grzac wode uzytkowa."
        ),
        "intake": _intake(
            case_id="MI-01",
            business_area="service",
            case_family="service",
            action="review",
            review_required=True,
            reason="Multi-intent: technical question plus service problem at another address.",
        ),
        "business": _business(
            area="service",
            interpretation="Dwie intencje: pytanie techniczne o Aquarea i nowy problem serwisowy CWU.",
            missing=["numer telefonu", "model pompy", "szczegolowy opis objawow", "dostepnosc na wizyte"],
            next_action="escalate_review",
            urgency="normal",
        ),
        "action_plan": {"primary_action": "create_review", "confidence": 0.72},
        "later_markers": ["telefon", "model", "objaw", "wizyte"],
        "next_step_marker": "Multi-intent:",
    },
]


@pytest.mark.parametrize("spec", TARGET_CASES, ids=[item["case_id"] for item in TARGET_CASES])
def test_cl03_cases_are_response_ready_with_later_gaps_preserved(spec: dict) -> None:
    ci, uo = _run(spec)

    assert uo["missing_critical_fields"] == []
    assert ci["case_understanding"]["blockers"] == []
    assert ci["case_understanding"]["review_required"] is False
    assert ci["next_best_action"]["primary_next_action"]["whether_human_review_required"] is False
    if spec.get("expected_action"):
        assert ci["next_best_action"]["primary_next_action"]["action_type"] == spec["expected_action"]
    else:
        assert ci["next_best_action"]["primary_next_action"]["action_type"] in {"answer_customer", "escalate_internal"}
    if spec.get("expected_urgency"):
        assert ci["next_best_action"]["primary_next_action"]["urgency_level"] == spec["expected_urgency"]

    later_text = " ".join((uo["missing_information"].get("important") or []) + (uo["missing_information"].get("helpful") or []))
    for marker in spec["later_markers"]:
        assert marker.lower() in later_text.lower()

    next_step = uo["next_best_action_recommendation"]["title_pl"]
    assert spec["next_step_marker"] in next_step
    if spec["case_id"] == "SVC-04":
        assert "low urgency" in next_step
        assert "eskal" not in next_step.lower()
    assert "call_kalk_top_quote" not in next_step
    assert "policz wycen" not in next_step.lower()


def test_true_quote_blocker_remains_not_quote_ready() -> None:
    spec = {
        "case_id": "NEG-QUOTE",
        "subject": "Wycena",
        "body": "Prosze o wycene pompy ciepla.",
        "intake": _intake(
            case_id="NEG-QUOTE",
            business_area="sales",
            case_family="lead_opportunity",
            action="create_case",
            reason="Vague quote request.",
            extracted_data={},
        ),
        "business": _business(
            area="lead",
            interpretation="Niepelne zapytanie ofertowe bez danych budynku.",
            missing=["metraz budynku", "lokalizacja", "typ budynku", "obecne ogrzewanie"],
            next_action="collect_data",
        ),
        "action_plan": {"primary_action": "prepare_reply", "confidence": 0.72},
    }
    ci, uo = _run(spec)

    assert uo["missing_critical_fields"]
    assert ci["next_best_action"]["primary_next_action"]["action_type"] == "ask_for_missing_data"
    assert "quote-ready" not in uo["next_best_action_recommendation"]["title_pl"]


def test_service_safety_acknowledges_without_invented_diagnosis_or_visit() -> None:
    spec = {
        "case_id": "NEG-SERVICE",
        "subject": "Awaria pompy ciepla",
        "body": "Pompa nie grzeje od wczoraj wieczorem.",
        "intake": _intake(
            case_id="NEG-SERVICE",
            business_area="service",
            case_family="service",
            priority="high",
            action="create_case",
            reason="Serious service issue without identifiers.",
        ),
        "business": _business(
            area="service",
            interpretation="Poważny problem serwisowy bez danych identyfikacyjnych.",
            missing=["adres instalacji", "numer telefonu", "model pompy", "kody bledow"],
            next_action="escalate_review",
            urgency="high",
        ),
    }
    ci, uo = _run(spec)

    assert uo["missing_critical_fields"] == []
    assert ci["next_best_action"]["primary_next_action"]["action_type"] == "escalate_internal"
    next_step = uo["next_best_action_recommendation"]["title_pl"].lower()
    assert "nie wymyslaj diagnozy ani wizyty" in next_step


def test_true_uncertainty_conflict_stays_blocking() -> None:
    spec = {
        "case_id": "NEG-CONFLICT",
        "subject": "Re: ustalenia",
        "body": "Prosze kontynuowac.",
        "intake": _intake(
            case_id="NEG-CONFLICT",
            business_area="sales",
            case_family="lead_opportunity",
            action="create_case",
            reason="Potentially conflicting facts.",
            extracted_data={"heated_area_m2": 150, "raw_geographic_signal": "Jaworzno"},
        ),
        "business": _business(
            area="lead",
            interpretation="Lead ma sprzeczne dane powierzchni.",
            missing=["sprzeczny metraz wymaga potwierdzenia"],
            next_action="collect_data",
        ),
    }
    ci, uo = _run(spec)

    assert any("sprzeczny" in item.lower() for item in uo["missing_critical_fields"])
    assert ci["case_understanding"]["blockers"]


def test_multi_intent_one_part_actionable_one_part_clarification_preserved() -> None:
    spec = next(item for item in TARGET_CASES if item["case_id"] == "MI-01")
    _, uo = _run(spec)

    intent = uo["current_customer_intent"].lower()
    assert "aquarea" in intent
    assert "serwis" in intent or "problem" in intent or "wode" in intent
    later = " ".join((uo["missing_information"].get("important") or []) + (uo["missing_information"].get("helpful") or [])).lower()
    assert "model" in later and "telefon" in later


def test_technical_document_does_not_fake_compatibility() -> None:
    spec = next(item for item in TARGET_CASES if item["case_id"] == "DOC-02")
    _, uo = _run(spec)

    next_step = uo["next_best_action_recommendation"]["title_pl"].lower()
    assert "bounded compatibility assessment" in next_step
    assert "pelnej kompatybilnosci" not in next_step or "nie udawaj" in next_step
    assert uo["missing_critical_fields"] == []
