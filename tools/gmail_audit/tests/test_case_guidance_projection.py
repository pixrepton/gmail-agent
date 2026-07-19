"""Projection tests: guidance fields on v2 patches without touching primary next action."""

from __future__ import annotations

from dash_projection_v2 import build_v2_shadow_projection


def _minimal_intake() -> dict:
    return {
        "schema_version": "1.0",
        "source": {"channel": "gmail", "mailbox": "m", "observed_at": "2026-01-01T00:00:00"},
        "message": {
            "message_id": "mid1",
            "date": "2026-01-01",
            "sender": "a@b.c",
            "subject": "Sub",
            "snippet": "sn",
            "has_attachments": False,
        },
        "thread": {"thread_id": "tid", "thread_position": "latest", "is_reply_or_forward": False},
        "business_area": "sales",
        "primary_signal": {"code": "x", "name": "X", "description": "d", "business_significance": "b"},
        "case_assessment": {
            "case_family": "lead_opportunity",
            "is_new_case": True,
            "state_detected": "new",
            "state_change": {"detected": False},
        },
        "decision": {"action": "create_case", "action_rationale": "r"},
        "priority": "medium",
        "confidence": {
            "signal_confidence": 0.8,
            "case_link_confidence": 0.7,
            "decision_confidence": 0.7,
            "extraction_confidence": 0.7,
        },
        "review": {"required": False, "flags": []},
        "reason": "reason",
        "extracted_data": {"entities": {}, "dates": [], "amounts": [], "references": {}, "deadlines": []},
    }


def test_guidance_fields_on_case_patch_and_subset_on_note_patch() -> None:
    intake = _minimal_intake()
    intel = {
        "case_understanding": {
            "case_id": "case_x",
            "business_priority": "medium",
            "attention_reason": "a",
            "blockers": [],
            "review_required": False,
            "review_flags": [],
        },
        "operator_brief": {"brief_pl": "brief"},
        "next_best_action": {
            "primary_next_action": {
                "action_type": "prepare_offer",
                "title_pl": "Przygotuj ofertę",
                "reason_pl": "offer reason",
                "urgency_level": "normal",
                "confidence": 0.6,
                "whether_human_review_required": False,
                "suggested_channel": "internal",
                "optional_draft_pointer": "",
            },
            "secondary_actions": [],
        },
        "missing_info": {"summary_pl": "m", "critical": [], "important": [], "helpful": []},
        "risk_assessment": {"summary_pl": "r", "risks": []},
        "merge_split_suggestions": {"summary_pl": "", "merge_candidates": [], "split_suspicions": []},
        "desk_composition": {
            "should_surface": True,
            "presence_mode": "advisory",
            "surface_zone": "desk",
            "day_bucket": "dzisiaj",
            "title_pl": "t",
            "body_short_pl": "b",
            "body_reason_pl": "br",
            "assistant_suggestion_pl": "as",
            "visibility_score": 0.6,
            "lifecycle_intent": "create",
            "review_required": False,
            "trace_summary": "",
        },
        "lifecycle_revision": {
            "lifecycle_intent": "create",
            "target_presence_mode": "advisory",
            "target_surface_zone": "desk",
            "reason_pl": "lr",
            "should_create": True,
            "should_update": False,
        },
        "feedback_learning_memory": {
            "explicit_signals": [],
            "implicit_signals": [],
            "preference_biases": [],
            "suppression_hints": [],
            "tone_hint_pl": "",
            "emphasis_hint_pl": "",
        },
        "case_guidance": {
            "operational_status": "ready",
            "waiting_for": "none",
            "reason_summary_pl": "Sprawa dojrzała do oferty.",
            "blocker_summary_pl": "",
            "momentum": "growing",
            "stagnation_flag": False,
            "stagnation_reason_pl": "",
            "business_readiness": "ready_for_offer",
            "operator_attention_class": "act_soon",
            "next_step_hint_pl": "Można przygotować ofertę.",
            "confidence": 0.77,
            "source_mode": "llm_reasoned",
            "evidence_refs": [{"source_id": "mid1", "excerpt": "NEVER_LEAK_THIS", "trust_level": "high", "can_answer_customer": True}],
            "assumptions": [],
            "unsupported_claims": [],
            "conflict_refs": [],
        },
        "attachment_intelligence": {},
        "thread_memory": {},
        "review_routing": {},
        "automation_policy": {},
    }
    out = build_v2_shadow_projection(
        intake,
        run_id="r1",
        stage_outputs={
            "case_link_result": {"decision": "no_link", "selected_case_key": "", "confidence": 0.0},
            "action_plan_result": {"primary_action": "prepare_reply", "safe_for_live_push": False},
            "business_reasoning_result": {},
            "case_intelligence_result": intel,
        },
    )
    cp = out["case_patch"]
    assert cp["primary_next_action_title_pl"] == "Przygotuj ofertę"
    assert cp["operational_status"] == "ready"
    assert cp["guidance_reason_summary_pl"] == "Sprawa dojrzała do oferty."
    assert cp["momentum"] == "growing"
    assert abs(float(cp["guidance_confidence"]) - 0.77) < 1e-9
    cg_block = cp.get("case_guidance") or {}
    refs = cg_block.get("evidence_refs") or []
    assert refs
    assert "NEVER_LEAK_THIS" not in str(refs)
    assert "excerpt" not in str(refs).lower()
    assert refs[0].get("trust_level") == "low"
    assert refs[0].get("can_answer_customer") is False

    dnp = out["desk_note_patch"]
    assert dnp["primary_next_action_title_pl"] == "Przygotuj ofertę"
    assert dnp["operational_status"] == "ready"
    assert "guidance_reason_summary_pl" in dnp
    assert "momentum" not in dnp
    assert dnp["surface_zone"] == "desk"
