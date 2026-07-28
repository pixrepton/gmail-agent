"""INTELLIGENCE-QUALITY-BASELINE-LIFT-01 Phase 6 — BusinessReasoning regression guard.

Counter-cases proving that the CLOSEOUT-01 safety normalization
(`post_offer + collect_data -> escalate_review`) does NOT ban data collection in
post-offer situations in general — it canonicalizes only the ONE genuinely incoherent
enum pair, and never over-escalates.

Semantic finding (contract enums, `intake_schema.CUSTOMER_STATE_GUESSES`): the contract
defines a DISTINCT state `waiting_for_data` for "we are collecting/awaiting customer
data". `post_offer` specifically denotes "the offer has been delivered; the operative
next step is a customer decision / negotiation / approval." Therefore the coherent form
of *post-offer / finalization* data collection is `(waiting_for_data, collect_data)` —
which the rule leaves untouched — while `(post_offer, collect_data)` is contract-
incoherent (if data were being collected the state would be `waiting_for_data`). The
canonicalization to the module's existing safe default `escalate_review` is therefore a
consistency fix, not over-escalation. The rule is kept as-is; this file guards its scope.
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


def test_waiting_for_data_collect_data_is_preserved():
    # The coherent representation of post-offer / finalization data collection. MUST NOT be
    # escalated — this is the counter-case that proves post-offer situations can still collect
    # data (they are classified waiting_for_data, not post_offer).
    out = validate_business_reasoning_result(
        _base(customer_state_guess="waiting_for_data", recommended_next_action="collect_data")
    )
    assert out["recommended_next_action"] == "collect_data"


def test_post_offer_update_case_is_preserved():
    # A post-offer data/scope update flows through update_case and is NOT touched by the rule.
    out = validate_business_reasoning_result(
        _base(customer_state_guess="post_offer", recommended_next_action="update_case")
    )
    assert out["recommended_next_action"] == "update_case"


def test_post_offer_call_and_wait_are_preserved():
    for action in ("call", "wait", "create_task"):
        out = validate_business_reasoning_result(
            _base(customer_state_guess="post_offer", recommended_next_action=action)
        )
        assert out["recommended_next_action"] == action


def test_rule_fires_only_for_exact_post_offer_collect_data_pair():
    # Exhaustive scope check: across every (state, action) combination the ONLY rewrite is
    # (post_offer, collect_data) -> escalate_review. Everything else is identity. This proves
    # no over-escalation and that post_offer is not a blanket collect_data ban.
    states = ["new_lead", "active_case", "post_offer", "waiting_for_data", "supplier_thread", "finance_flow", "unclear"]
    actions = ["reply", "call", "collect_data", "create_task", "update_case", "wait", "ignore", "escalate_review"]
    rewrites = []
    for st in states:
        for ac in actions:
            out = validate_business_reasoning_result(_base(customer_state_guess=st, recommended_next_action=ac))
            if out["recommended_next_action"] != ac:
                rewrites.append((st, ac, out["recommended_next_action"]))
    assert rewrites == [("post_offer", "collect_data", "escalate_review")]
