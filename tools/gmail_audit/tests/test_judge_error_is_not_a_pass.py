"""STRUCTURED-INPUT-AND-CAPABILITY-BASELINE-CLOSEOUT-01 Phase 8 — measurement integrity:
an unresolved Understanding judge must never be scored as a quality pass.

Observed live on MI-04 in targeted probe run2: understanding.status == JUDGE_ERROR
(a Groq TPD rate limit, i.e. pure infrastructure) yet the case was reported as
CLEAN_PASS, because _final_outcome's "scored and passed is False" guard cannot fire
for a component whose scored flag is False.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval_final_rescore import _final_outcome  # noqa: E402


def _judge_error_status(status: str = "JUDGE_ERROR") -> dict:
    # exactly the shape _stage_status() emits for an unresolved judge
    return {
        "status": status,
        "eligible": True,
        "scored": False,
        "passed": False,
        "reason": "understanding_judge_unresolved",
    }


_NOT_APPLICABLE = {"status": "NOT_APPLICABLE", "eligible": False, "scored": False, "passed": None}


def test_judge_error_is_classified_as_harness_not_clean_pass():
    outcome = _final_outcome(
        {"primary_outcome": "CLEAN_PASS"},
        {"extraction": _NOT_APPLICABLE, "understanding": _judge_error_status(), "draft": _NOT_APPLICABLE},
    )
    assert outcome == "HARNESS"


def test_judge_unavailable_is_also_not_a_clean_pass():
    outcome = _final_outcome(
        {"primary_outcome": "CLEAN_PASS"},
        {"understanding": _judge_error_status("JUDGE_UNAVAILABLE")},
    )
    assert outcome == "HARNESS"


def test_judge_error_does_not_mask_a_real_capability_failure():
    # a genuinely-scored failing component still reports CAPABILITY, not HARNESS-by-accident:
    # capture gaps / judge errors are infrastructure, a scored failure is capability
    outcome = _final_outcome(
        {"primary_outcome": "CLEAN_PASS"},
        {"draft": {"status": "SCORED", "eligible": True, "scored": True, "passed": False}},
    )
    assert outcome == "CAPABILITY"


def test_a_fully_scored_passing_case_is_still_clean_pass():
    # the guard must not depress genuinely clean cases
    outcome = _final_outcome(
        {"primary_outcome": "CLEAN_PASS"},
        {
            "extraction": {"status": "SCORED", "eligible": True, "scored": True, "passed": True},
            "understanding": {"status": "SCORED", "eligible": True, "scored": True, "passed": True},
            "draft": _NOT_APPLICABLE,
        },
    )
    assert outcome == "CLEAN_PASS"
