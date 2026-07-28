"""CLOSEOUT-01 Phase 4 — general tests for the deterministic decision-class consistency
normalization in validate_business_reasoning_result.

These test the RULE (post_offer + collect_data -> escalate_review), not any benchmark
case's literal text. Host-testable: no container, no LLM, no I/O.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intake_schema import validate_business_reasoning_result  # noqa: E402


def _base(**over):
    obj = {
        "business_interpretation": "x",
        "business_area": "lead",
        "customer_state_guess": "new_lead",
        "recommended_next_action": "collect_data",
        "recommended_action_reason": "x",
        "missing_information": [],
        "risks": [],
        "urgency": "normal",
        "operator_note": "x",
        "confidence": {"business_confidence": 0.3, "action_confidence": 0.4},
    }
    obj.update(over)
    return obj


def test_post_offer_collect_data_is_normalized_to_escalate_review():
    out = validate_business_reasoning_result(_base(customer_state_guess="post_offer", recommended_next_action="collect_data"))
    assert out["recommended_next_action"] == "escalate_review"


def test_new_lead_collect_data_is_left_unchanged():
    out = validate_business_reasoning_result(_base(customer_state_guess="new_lead", recommended_next_action="collect_data"))
    assert out["recommended_next_action"] == "collect_data"


def test_active_case_collect_data_is_left_unchanged():
    out = validate_business_reasoning_result(_base(customer_state_guess="active_case", recommended_next_action="collect_data"))
    assert out["recommended_next_action"] == "collect_data"


def test_post_offer_escalate_review_is_left_unchanged():
    out = validate_business_reasoning_result(_base(customer_state_guess="post_offer", recommended_next_action="escalate_review"))
    assert out["recommended_next_action"] == "escalate_review"


def test_post_offer_reply_is_left_unchanged():
    # rule targets only the incoherent collect_data pairing, never a legitimate post_offer reply
    out = validate_business_reasoning_result(_base(customer_state_guess="post_offer", recommended_next_action="reply"))
    assert out["recommended_next_action"] == "reply"


def test_normalization_is_one_directional_never_reduces_escalation():
    # exhaustive: for every customer state, the rule must never turn escalate_review into
    # something less conservative
    states = ["new_lead", "active_case", "post_offer", "waiting_for_data", "supplier_thread", "finance_flow", "unclear"]
    for st in states:
        out = validate_business_reasoning_result(_base(customer_state_guess=st, recommended_next_action="escalate_review"))
        assert out["recommended_next_action"] == "escalate_review"


def test_post_offer_collect_data_reply_recommended_recomputed():
    # after normalization to escalate_review, reply_recommended must not be forced True by the
    # (now-replaced) collect_data value
    out = validate_business_reasoning_result(_base(customer_state_guess="post_offer", recommended_next_action="collect_data", reply_recommended=False))
    assert out["recommended_next_action"] == "escalate_review"
    assert out["reply_recommended"] is False
