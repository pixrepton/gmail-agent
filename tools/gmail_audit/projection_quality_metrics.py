"""Read-only quality metrics for Context Projection and Skrzat outputs.

This module does not write feedback, train a model, or approve actions. It only
summarizes projection/answer quality signals that can later feed a proof ladder.
"""

from __future__ import annotations

from typing import Any


SCHEMA_VERSION = "projection_quality_metrics.v1"


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _kind(item: Any) -> str:
    if isinstance(item, dict):
        raw = item.get("kind") or item.get("type") or item.get("decision") or item.get("label")
    else:
        raw = item
    return str(raw or "").strip().lower()


def _number(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _missing_source_count(blocks: list[Any]) -> int:
    total = 0
    for block in blocks:
        if not isinstance(block, dict):
            total += 1
            continue
        source_id = str(block.get("source_id") or block.get("evidence_ref") or "").strip()
        if not source_id:
            total += 1
    return total


def _feedback_counts(operator_feedback: Any) -> dict[str, int]:
    accepted = 0
    rejected = 0
    corrected = 0
    false_gap = 0
    false_conflict = 0
    edit_distance_total = 0
    feedback_items = _items(operator_feedback)
    for item in feedback_items:
        k = _kind(item)
        if k in {"accepted", "accept", "approved", "approve", "trafne"}:
            accepted += 1
        if k in {"rejected", "reject", "odrzucone"}:
            rejected += 1
        if k in {"corrected", "correction", "edited", "edit", "za_mocne", "za_slabe"}:
            corrected += 1
        if k in {"false_gap", "wrong_gap", "brak_falszywy"}:
            false_gap += 1
        if k in {"false_conflict", "wrong_conflict", "konflikt_falszywy"}:
            false_conflict += 1
        if isinstance(item, dict):
            edit_distance_total += _number(item.get("edit_distance"))
    total = len(feedback_items)
    acceptance_rate = round(accepted / total, 4) if total else None
    return {
        "operator_feedback_count": total,
        "operator_accept_count": accepted,
        "operator_reject_count": rejected,
        "operator_edit_count": corrected,
        "operator_edit_distance_total": edit_distance_total,
        "false_gap_count": false_gap,
        "false_conflict_count": false_conflict,
        "projection_acceptance_rate": acceptance_rate,
    }


def build_projection_quality_metrics(
    projection_envelope: dict[str, Any] | None,
    *,
    skrzat_answer: dict[str, Any] | None = None,
    operator_feedback: list[dict[str, Any]] | None = None,
    generated_at: str = "",
) -> dict[str, Any]:
    """Build local read-only quality counters for a projection and optional answer."""
    envelope = projection_envelope if isinstance(projection_envelope, dict) else {}
    answer = skrzat_answer if isinstance(skrzat_answer, dict) else {}
    evidence_blocks = _items(envelope.get("evidence_blocks"))
    evidence_ignored = _items(envelope.get("evidence_ignored"))
    feedback = _feedback_counts(operator_feedback or [])
    missing_evidence_count = len(evidence_ignored) + _missing_source_count(evidence_blocks)
    answer_evidence = _items(answer.get("evidence"))
    evidence_denom = max(len(evidence_blocks), len(answer_evidence), 1)
    skrzat_evidence_coverage_rate = round(len(answer_evidence) / evidence_denom, 4)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": str(generated_at or "").strip(),
        "read_only": True,
        "action_allowed": False,
        "projection_gap_count": len(_items(envelope.get("gap_blocks"))),
        "projection_conflict_count": len(_items(envelope.get("conflict_blocks"))),
        "projection_warning_count": len(_items(envelope.get("warnings"))),
        "projection_evidence_count": len(evidence_blocks),
        "evidence_ignored_count": len(evidence_ignored),
        "missing_evidence_count": missing_evidence_count,
        "skrzat_answer_has_evidence": bool(_items(answer.get("evidence"))),
        "skrzat_answer_gap_count": len(_items(answer.get("gaps"))),
        "skrzat_answer_conflict_count": len(_items(answer.get("conflicts"))),
        "skrzat_answer_warning_count": len(_items(answer.get("warnings"))),
        "skrzat_evidence_coverage_rate": skrzat_evidence_coverage_rate,
        **feedback,
    }


__all__ = ["SCHEMA_VERSION", "build_projection_quality_metrics"]
