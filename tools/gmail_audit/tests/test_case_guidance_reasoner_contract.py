"""Contract tests for case guidance normalization and seams (no live LLM)."""

from __future__ import annotations

import pytest

from case_intelligence import _normalize_case_guidance, merge_case_guidance_into_intelligence
from case_guidance_reasoner import (
    build_skipped_case_guidance,
    fallback_case_guidance,
    parse_and_validate_case_guidance,
)


def test_normalize_enums_repair_invalid() -> None:
    cg = _normalize_case_guidance(
        {
            "operational_status": "not_an_enum",
            "waiting_for": "nope",
            "momentum": "x",
            "business_readiness": "x",
            "operator_attention_class": "x",
            "reason_summary_pl": "Test.",
            "confidence": 99.0,
            "source_mode": "llm_reasoned",
        },
        source_mode="llm_reasoned",
    )
    assert cg["operational_status"] == "watching"
    assert cg["waiting_for"] == "unknown"
    assert cg["momentum"] == "steady"
    assert cg["business_readiness"] == "not_ready"
    assert cg["operator_attention_class"] == "watch"
    assert cg["confidence"] == 1.0


def test_skipped_defaults_match_spec() -> None:
    cg = build_skipped_case_guidance(reason="x", base_intelligence={})
    assert cg["source_mode"] == "skipped"
    assert cg["operational_status"] == "watching"
    assert cg["waiting_for"] == "unknown"
    assert "Brak pewnej interpretacji" in cg["reason_summary_pl"]
    assert cg["confidence"] == 0.0
    assert cg["evidence_refs"] == []
    assert cg["conflict_refs"] == []
    assert cg["unsupported_claims"]


def test_fallback_sets_fallback_mode() -> None:
    cg = fallback_case_guidance(reason="boom", base_intelligence={})
    assert cg["source_mode"] == "fallback"


def test_parse_and_validate_roundtrip() -> None:
    raw = """{"operational_status":"waiting","waiting_for":"client","reason_summary_pl":"Klient nie odpowiedział.","blocker_summary_pl":"","momentum":"slowing","stagnation_flag":false,"stagnation_reason_pl":"","business_readiness":"needs_data","operator_attention_class":"act_soon","next_step_hint_pl":"Warto przypomnieć się klientowi.","confidence":0.72}"""
    cg = parse_and_validate_case_guidance(raw)
    assert cg["source_mode"] == "llm_reasoned"
    assert cg["waiting_for"] == "client"
    assert 0.0 <= cg["confidence"] <= 1.0
    assert cg["evidence_refs"] == []


def test_parse_and_validate_preserves_evidence_ledger() -> None:
    raw = """{"operational_status":"waiting","waiting_for":"schedule","reason_summary_pl":"Klient proponuje termin.","blocker_summary_pl":"Brak potwierdzenia w kalendarzu.","momentum":"steady","stagnation_flag":false,"stagnation_reason_pl":"","business_readiness":"needs_data","operator_attention_class":"act_soon","next_step_hint_pl":"Sprawdz kalendarz i potwierdz termin.","confidence":0.72,"evidence_refs":[{"source_id":"msg-1","excerpt":"wtorek 10:00"}],"assumptions":["Termin jest propozycja klienta"],"unsupported_claims":["Nie ma potwierdzonego eventu"],"conflict_refs":[{"source_id":"cal-1","field_name":"start_at","excerpt":"cal_snip"}]}"""
    cg = parse_and_validate_case_guidance(raw)
    assert cg["evidence_refs"][0]["source_id"] == "msg-1"
    assert "excerpt" not in cg["evidence_refs"][0]
    assert cg["evidence_refs"][0].get("trust_level") == "low"
    assert "wtorek 10:00" not in str(cg["evidence_refs"])
    assert cg["assumptions"] == ["Termin jest propozycja klienta"]
    assert cg["unsupported_claims"] == ["Nie ma potwierdzonego eventu"]
    assert cg["conflict_refs"][0]["field_name"] == "start_at"
    assert "excerpt" not in cg["conflict_refs"][0]
    assert "cal_snip" not in str(cg["conflict_refs"])


def test_merge_keeps_primary_next_action_and_lifecycle() -> None:
    base = {
        "case_understanding": {"case_id": "c1", "review_flags": []},
        "operator_brief": {"brief_pl": "old"},
        "next_best_action": {
            "primary_next_action": {
                "action_type": "answer_customer",
                "title_pl": "Odpowiedz klientowi",
                "reason_pl": "r",
                "urgency_level": "normal",
                "confidence": 0.5,
                "whether_human_review_required": False,
                "suggested_channel": "mail",
                "optional_draft_pointer": "",
            },
            "secondary_actions": [],
        },
        "missing_info": {"summary_pl": "", "critical": [], "important": [], "helpful": []},
        "risk_assessment": {"summary_pl": "", "risks": []},
        "merge_split_suggestions": {"summary_pl": "", "merge_candidates": [], "split_suspicions": []},
        "desk_composition": {
            "should_surface": True,
            "presence_mode": "standard",
            "surface_zone": "desk",
            "day_bucket": "dzisiaj",
            "title_pl": "t",
            "body_short_pl": "s",
            "body_reason_pl": "old reason",
            "assistant_suggestion_pl": "Odpowiedz klientowi",
            "visibility_score": 0.5,
            "lifecycle_intent": "update",
            "review_required": False,
            "trace_summary": "",
        },
        "lifecycle_revision": {
            "lifecycle_intent": "update",
            "target_presence_mode": "standard",
            "target_surface_zone": "desk",
            "reason_pl": "lr",
            "should_create": False,
            "should_update": True,
        },
        "feedback_learning_memory": {
            "explicit_signals": [],
            "implicit_signals": [],
            "preference_biases": [],
            "suppression_hints": [],
            "tone_hint_pl": "",
            "emphasis_hint_pl": "",
        },
    }
    merged = merge_case_guidance_into_intelligence(
        base,
        {
            "operational_status": "follow_up_needed",
            "waiting_for": "client",
            "reason_summary_pl": "Trzeba wrócić do klienta.",
            "blocker_summary_pl": "",
            "momentum": "steady",
            "stagnation_flag": False,
            "stagnation_reason_pl": "",
            "business_readiness": "needs_data",
            "operator_attention_class": "act_soon",
            "next_step_hint_pl": "Warto przypomnieć się mailowo.",
            "confidence": 0.8,
            "source_mode": "llm_reasoned",
        },
    )
    assert merged["desk_composition"]["surface_zone"] == "desk"
    assert merged["desk_composition"]["presence_mode"] == "standard"
    assert merged["lifecycle_revision"]["lifecycle_intent"] == "update"
    assert merged["next_best_action"]["primary_next_action"]["title_pl"] == "Odpowiedz klientowi"
    assert "System sugeruje" in merged["operator_brief"]["brief_pl"]
