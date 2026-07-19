from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from context_quality_contract import normalize_context_quality, operator_feed_context_quality_view


def test_decision_ready_is_downgraded_when_evidence_is_weak() -> None:
    out = normalize_context_quality(
        {
            "ready_for_decision": True,
            "action_readiness": "decision_ready",
            "weak_evidence_count": 2,
        }
    )

    assert out["ready_for_decision"] is False
    assert out["action_readiness"] == "review_only"
    assert "weak_or_missing_evidence" in out["not_ready_reasons"]


def test_blocking_context_forces_not_ready_and_blocks_operator_review_possible() -> None:
    out = normalize_context_quality(
        {
            "ready_for_decision": True,
            "operator_review_possible": True,
            "action_readiness": "decision_ready",
            "has_blocking_gaps": True,
        }
    )

    assert out["ready_for_decision"] is False
    assert out["operator_review_possible"] is False
    assert out["ready_for_operator_review"] is False
    assert out["action_readiness"] == "not_ready"
    assert "blocking_gaps" in out["not_ready_reasons"]


def test_projection_view_exports_only_core_safe_keys() -> None:
    out = operator_feed_context_quality_view(
        {
            "ready_for_decision": False,
            "operator_review_possible": True,
            "action_readiness": "review_only",
            "raw_response": {"private": True},
            "body": "private",
            "source_diversity_count": 3,
            "thread_has_unanswered_question": True,
            "not_ready_reasons": ["weak_or_missing_evidence", "client@example.invalid"],
        }
    )

    assert "raw_response" not in out
    assert "body" not in out
    assert "source_diversity_count" not in out
    assert "thread_has_unanswered_question" not in out
    assert out["not_ready_reasons"][0] == "weak_or_missing_evidence"
    assert "example" not in repr(out)


def test_invalid_action_readiness_falls_back_conservatively() -> None:
    out = normalize_context_quality({"ready_for_decision": False, "action_readiness": "ship_it"})

    assert out["action_readiness"] == "review_only"
    assert out["ready_for_decision"] is False
