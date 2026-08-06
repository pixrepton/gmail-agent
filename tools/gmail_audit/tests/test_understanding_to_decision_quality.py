"""IQ-01 — Understanding → Decision quality (bounded unit proof)."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.recommended_next_step_quality import (
    DECISION_STATE_APPROVE,
    DECISION_STATE_CLOSE,
    DECISION_STATE_COMPLETE_INFO,
    DECISION_STATE_CONSCIOUSLY_DO_NOTHING,
    DECISION_STATE_EXECUTE,
    DECISION_STATE_REPLY,
    DECISION_STATE_WAIT,
    apply_nba_quality_to_understanding,
    classify_decision_state,
    evaluate_understanding_to_decision_quality,
    is_meaningful_follow_up_delta,
    is_vague_next_step,
    separate_gaps_vs_risks,
    sharpen_recommended_next_step,
)


def test_fu06_unresolved_question_forces_reply_not_vague() -> None:
    """FU-06 style: continuity delta + unresolved question → reply, not escalate."""
    out = evaluate_understanding_to_decision_quality(
        {
            "case_id": "FU-06",
            "situation_summary": {"case_family": "lead_opportunity", "business_area": "sales"},
            "missing_critical_fields": [],
            "open_loops": [],
            "risks": [
                {
                    "risk_type": "interpretation_risk",
                    "summary_pl": "Klient ma niezałatwione pytanie wymagające odpowiedzi przed dalszym ruchem.",
                }
            ],
            "thread_delta": {
                "operator_visible_delta_summary": (
                    "Wczesniej ustalone dane pozostaja wazne i wiazace dla biezacej sprawy "
                    "(scope: ogrzewanie); nie zostaly zmienione ani odwolane przez biezaca wiadomosc."
                ),
                "changes": [],
            },
            "next_best_action_recommendation": {
                "title_pl": "Wymagana ręczna ocena",
                "reason_pl": "escalate_internal",
                "action_type": "review_required",
            },
            "operator_explanation": {"essence_pl": "Re: Wycena - dodatkowe pytanie"},
        },
        case_id="FU-06",
        case_kind="zapytanie_klienta",
        expected_decision_state=DECISION_STATE_REPLY,
        require_follow_up_delta=True,
    )
    assert out["verdict"] == "PASS"
    assert out["decision_state"] == DECISION_STATE_REPLY
    assert out["meaningful_follow_up_delta"] is True
    assert not is_vague_next_step(out["sharpened_next_step_pl"])
    assert "Follow-up" in out["sharpened_next_step_pl"] or "odpowiedz" in out[
        "sharpened_next_step_pl"
    ].lower()


def test_fu07_follow_up_change_maps_to_reply() -> None:
    sharpened = sharpen_recommended_next_step(
        title_pl="Wymagana ręczna ocena",
        reason_pl="Business reasoning could not be confirmed safely.",
        case_kind="wycena_oferta",
        what_changed_pl="Klient dodał: jeszcze jedna rzecz do wyceny — bufor 300l",
        risks=[],
        open_loops=[],
        thread_delta={"changes": [{"change_type": "new_request", "summary_pl": "bufor 300l"}]},
    )
    assert "Follow-up" in sharpened
    assert not is_vague_next_step(sharpened)
    state = classify_decision_state(
        sharpened_pl=sharpened,
        case_kind="wycena_oferta",
        missing_critical_fields=[],
    )
    assert state == DECISION_STATE_REPLY


def test_gaps_vs_risks_rejects_question_in_gaps() -> None:
    sep = separate_gaps_vs_risks(
        missing_critical_fields=["Jakie jest ciśnienie w instalacji?"],
        risks=[{"summary_pl": "Lead loss risk"}],
        open_loops=[],
    )
    assert sep["separated"] is False
    assert sep["questionish_gaps"]


def test_gaps_vs_risks_clean_separation() -> None:
    sep = separate_gaps_vs_risks(
        missing_critical_fields=["OZC", "miasto"],
        risks=[{"summary_pl": "Lead jest aktywny, ale bez krytycznych danych może utknąć."}],
        open_loops=["czekamy na OZC od klienta"],
    )
    assert sep["separated"] is True
    assert sep["overlap"] == []


def test_continuity_delta_alone_is_not_meaningful() -> None:
    assert (
        is_meaningful_follow_up_delta(
            what_changed_pl=(
                "Wczesniej ustalone dane pozostaja wazne i wiazace dla biezacej sprawy "
                "(typ budynku: dom); nie zostaly zmienione ani odwolane przez biezaca wiadomosc."
            ),
            thread_delta={"changes": []},
            risks=[],
            open_loops=[],
        )
        is False
    )


def test_decision_states_cover_roadmap_set() -> None:
    assert (
        classify_decision_state(
            sharpened_pl="Oferta: policz wycenę gdy narzędzie dostępne (call_kalk_top_quote)",
            case_kind="wycena_oferta",
            policy_allowed=True,
        )
        == DECISION_STATE_EXECUTE
    )
    assert (
        classify_decision_state(
            sharpened_pl="Draft gotowy — zatwierdź w HITL approve",
            draft_ready=True,
        )
        == DECISION_STATE_APPROVE
    )
    assert (
        classify_decision_state(
            sharpened_pl="Oferta: dopytaj TYLKO o: OZC; potem draft",
            missing_critical_fields=["OZC"],
        )
        == DECISION_STATE_COMPLETE_INFO
    )
    assert (
        classify_decision_state(
            sharpened_pl="Czekaj na odpowiedź klienta (waiting_client)",
            lifecycle_hint="waiting_client",
        )
        == DECISION_STATE_WAIT
    )
    assert (
        classify_decision_state(
            sharpened_pl="Zamknij sprawę — terminal",
            lifecycle_hint="closed",
        )
        == DECISION_STATE_CLOSE
    )
    assert (
        classify_decision_state(
            sharpened_pl="Administracja: nie prowadź HVAC — report_gaps_and_stop",
            case_kind="ksiegowosc",
        )
        == DECISION_STATE_CONSCIOUSLY_DO_NOTHING
    )


def test_apply_nba_quality_attaches_decision_state() -> None:
    out = apply_nba_quality_to_understanding(
        {
            "situation_summary": {"case_family": "wycena_oferta", "business_area": "sales"},
            "missing_critical_fields": [],
            "risks": [
                {"summary_pl": "Klient ma niezałatwione pytanie wymagające odpowiedzi."}
            ],
            "operator_explanation": {"essence_pl": "Lead follow-up"},
            "thread_delta": {"operator_visible_delta_summary": "", "changes": []},
            "next_best_action_recommendation": {
                "title_pl": "Wymagana ręczna ocena",
                "reason_pl": "escalate_internal",
                "kind": "recommendation",
            },
        },
        case_kind="wycena_oferta",
    )
    nba = out["next_best_action_recommendation"]
    assert nba["quality"]["decision_state"] == DECISION_STATE_REPLY
    assert nba["quality"]["meaningful_follow_up_delta"] is True
    assert nba["quality"]["gaps_vs_risks_separated"] is True
    assert "ręczna ocena" not in nba["title_pl"].lower()
