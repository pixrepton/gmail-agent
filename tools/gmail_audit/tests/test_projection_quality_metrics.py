from __future__ import annotations

from pathlib import Path
import sys


TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from projection_quality_metrics import build_projection_quality_metrics


def test_projection_quality_metrics_are_read_only_and_count_feedback() -> None:
    envelope = {
        "schema_version": "projection_envelope.v1",
        "gap_blocks": [{"gap_key": "g1"}, {"gap_key": "g2"}],
        "conflict_blocks": [{"conflict_key": "c1"}],
        "evidence_blocks": [{"source_id": "mail-1"}, {"summary": "no source"}],
        "evidence_ignored": [{"reason": "missing source"}],
        "warnings": [{"warning": "unsupported"}],
        "read_only": True,
        "action_allowed": False,
    }
    answer = {
        "schema_version": "conversation_answer_envelope.v1",
        "evidence": [{"source_id": "mail-1"}],
        "gaps": [{"gap_key": "g1"}],
        "conflicts": [],
        "warnings": [],
    }
    feedback = [
        {"kind": "accepted"},
        {"kind": "corrected", "edit_distance": 12},
        {"kind": "false_gap"},
        {"kind": "false_conflict"},
    ]

    metrics = build_projection_quality_metrics(
        envelope,
        skrzat_answer=answer,
        operator_feedback=feedback,
        generated_at="2026-05-19T18:00:00Z",
    )

    assert metrics["schema_version"] == "projection_quality_metrics.v1"
    assert metrics["read_only"] is True
    assert metrics["action_allowed"] is False
    assert metrics["projection_acceptance_rate"] == 0.25
    assert metrics["operator_edit_count"] == 1
    assert metrics["operator_edit_distance_total"] == 12
    assert metrics["false_gap_count"] == 1
    assert metrics["false_conflict_count"] == 1
    assert metrics["missing_evidence_count"] == 2
    assert metrics["skrzat_answer_has_evidence"] is True
    assert metrics["skrzat_answer_gap_count"] == 1
    assert metrics["skrzat_evidence_coverage_rate"] == 0.5
