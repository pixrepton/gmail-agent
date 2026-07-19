"""Shadow review export and evaluation helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from artifact_contracts import REVIEW_TEMPLATE_FIELDS, RUN_ARTIFACT_FILENAMES
from artifact_io import read_csv_rows, write_csv, write_json, write_text
from dash_preview import resolve_case_key_metadata
from intake_policy import (
    ACTION_DECISIONS,
    NEW_CASE_ACTIONS,
    REFERENCE_DECISIONS,
    UPDATE_ACTIONS,
)


def write_shadow_review_template(
    run_dir: Path,
    outputs: list[dict[str, Any]],
    *,
    validation_rows: list[dict[str, Any]] | None = None,
    stage_records: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a CSV template for human shadow review."""
    template_path = run_dir / RUN_ARTIFACT_FILENAMES["shadow_review_template"]
    validation_by_message_id = {
        str(row.get("message_id") or "").strip(): row
        for row in (validation_rows or [])
        if isinstance(row, dict) and str(row.get("message_id") or "").strip()
    }
    stage_records_by_message_id = _stage_records_by_message_id(stage_records or [])
    rows = [
        build_review_row(
            item,
            validation_row=validation_by_message_id.get(str(item["message"]["message_id"] or "").strip()),
            stage_record=stage_records_by_message_id.get(str(item["message"]["message_id"] or "").strip()),
        )
        for item in outputs
    ]
    write_csv(template_path, REVIEW_TEMPLATE_FIELDS, rows)

    human_annotations_path = run_dir / RUN_ARTIFACT_FILENAMES["human_annotations"]
    if not human_annotations_path.exists():
        write_csv(human_annotations_path, REVIEW_TEMPLATE_FIELDS, rows)

    return template_path


def build_review_row(
    intake_output: dict[str, Any],
    *,
    validation_row: dict[str, Any] | None = None,
    stage_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one shadow-review CSV row from validated intake output."""
    case_key_info = resolve_case_key_metadata(intake_output)
    guardrail_flags = (validation_row or {}).get("guardrail_flags") or []
    if not isinstance(guardrail_flags, list):
        guardrail_flags = []
    stage_actual = _extract_stage_actuals(intake_output, stage_record)
    return {
        "message_id": intake_output["message"]["message_id"],
        "subject": intake_output["message"]["subject"],
        "sender": intake_output["message"]["sender"],
        "agent_preclassification_lane": stage_actual["preclassification_lane"],
        "agent_primary_signal_code": intake_output["primary_signal"]["code"],
        "agent_business_area": intake_output["business_area"],
        "agent_case_family": intake_output["case_assessment"]["case_family"],
        "agent_decision_action": intake_output["decision"]["action"],
        "agent_priority": intake_output["priority"],
        "agent_review_required": str(bool(intake_output["review"]["required"])).lower(),
        "agent_review_flags": ",".join(intake_output["review"]["flags"]),
        "agent_signal_confidence": _format_float(intake_output["confidence"]["signal_confidence"]),
        "agent_case_link_confidence": _format_float(intake_output["confidence"]["case_link_confidence"]),
        "agent_decision_confidence": _format_float(intake_output["confidence"]["decision_confidence"]),
        "agent_case_key": str(case_key_info.get("case_key") or ""),
        "agent_case_key_source": str(case_key_info.get("case_key_source") or ""),
        "agent_final_output_origin": str((validation_row or {}).get("final_output_origin") or ""),
        "agent_guardrail_flags": ",".join(str(item).strip() for item in guardrail_flags if str(item).strip()),
        "agent_case_link_decision": stage_actual["case_link_decision"],
        "agent_case_link_selected_key": stage_actual["case_link_selected_key"],
        "agent_case_link_stage_confidence": _format_float(stage_actual["case_link_stage_confidence"]),
        "agent_business_interpretation": stage_actual["business_interpretation"],
        "agent_recommended_next_action": stage_actual["recommended_next_action"],
        "agent_missing_information": ",".join(sorted(stage_actual["missing_information"])),
        "agent_operator_note": stage_actual["operator_note"],
        "agent_reply_draft_available": str(stage_actual["reply_draft_available"]).lower(),
        "agent_reply_recommended_variant": stage_actual["reply_recommended_variant"],
        "agent_reply_do_not_send_reasons": ",".join(sorted(stage_actual["reply_do_not_send_reasons"])),
        "agent_action_primary": stage_actual["action_primary"],
        "agent_action_projection_mode": stage_actual["projection_mode"],
        "agent_action_safe_for_live_push": str(stage_actual["action_safe_for_live_push"]).lower(),
        "expected_primary_signal_code": "",
        "expected_business_area": "",
        "expected_case_family": "",
        "expected_decision_action": "",
        "expected_priority": "",
        "expected_review_required": "",
        "expected_case_key_if_known": "",
        "expected_key_references": "",
        "expected_case_link_decision": "",
        "expected_recommended_next_action": "",
        "expected_missing_information": "",
        "expected_operator_note_quality": "",
        "expected_reply_should_exist": "",
        "expected_reply_quality": "",
        "expected_reply_usefulness": "",
        "expected_action_primary": "",
        "expected_projection_mode": "",
        "reviewer_failure_cluster": "",
        "prompt_change_hint": "",
        "threshold_change_hint": "",
        "reviewer_notes": "",
    }


def summarize_validation_results(validation_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return compact validation-path metrics for eval and reporting."""
    summary = {
        "compared_validation_rows": 0,
        "raw_valid": 0,
        "normalized_valid": 0,
        "repaired_valid": 0,
        "guardrailed_review": 0,
        "invalid": 0,
        "guardrail_applied": 0,
    }

    for row in validation_rows:
        if not isinstance(row, dict):
            continue
        summary["compared_validation_rows"] += 1
        origin = str(row.get("final_output_origin") or "").strip()
        if origin in summary:
            summary[origin] += 1
        elif not origin:
            summary["invalid"] += 1
        summary["guardrail_applied"] += int(bool(row.get("guardrail_applied")))

    total = summary["compared_validation_rows"]
    if total > 0:
        summary["origin_distribution"] = {
            "raw_valid": _ratio(summary["raw_valid"], total),
            "normalized_valid": _ratio(summary["normalized_valid"], total),
            "repaired_valid": _ratio(summary["repaired_valid"], total),
            "guardrailed_review": _ratio(summary["guardrailed_review"], total),
            "invalid": _ratio(summary["invalid"], total),
        }
    else:
        summary["origin_distribution"] = {}

    return summary


def evaluate_annotations(
    outputs: list[dict[str, Any]],
    annotations_path: str | Path,
    *,
    stage_records: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compare human annotations with agent outputs and return summary plus details."""
    annotations = _load_annotations(annotations_path)
    by_message_id = {item["message"]["message_id"]: item for item in outputs}
    stage_records_by_message_id = _stage_records_by_message_id(stage_records or [])

    details: list[dict[str, Any]] = []
    exact_signal = 0
    exact_decision = 0
    exact_area = 0
    exact_case_family = 0
    exact_priority = 0
    exact_review = 0
    false_positive_review = 0
    false_negative_review = 0
    missing_outputs = 0
    case_key_exact = 0
    case_key_compared = 0
    reference_hits = 0
    reference_expected = 0
    case_link_exact = 0
    case_link_compared = 0
    business_action_exact = 0
    business_action_compared = 0
    action_primary_exact = 0
    action_primary_compared = 0
    projection_mode_exact = 0
    projection_mode_compared = 0
    reply_presence_exact = 0
    reply_presence_compared = 0
    reply_quality_exact = 0
    reply_quality_compared = 0
    reply_usefulness_exact = 0
    reply_usefulness_compared = 0
    operator_note_quality_exact = 0
    operator_note_quality_compared = 0
    missing_info_hits = 0
    missing_info_expected = 0
    missing_info_overcalled = 0
    compared = 0
    failure_clusters: dict[str, int] = {}
    prompt_change_candidates: dict[str, int] = {}
    threshold_change_candidates: dict[str, int] = {}

    for row in annotations:
        message_id = row.get("message_id", "").strip()
        if not message_id:
            continue

        output = by_message_id.get(message_id)
        if output is None:
            missing_outputs += 1
            details.append(
                {
                    "message_id": message_id,
                    "status": "missing_output",
                    "expected_decision_action": row.get("expected_decision_action", "").strip(),
                }
            )
            continue

        compared += 1
        stage_actual = _extract_stage_actuals(output, stage_records_by_message_id.get(message_id))
        agent_review_required = str(bool(output["review"]["required"])).lower()
        expected_review_required = _normalize_boolish(row.get("expected_review_required"))

        signal_match = row.get("expected_primary_signal_code", "").strip() == output["primary_signal"]["code"]
        decision_match = row.get("expected_decision_action", "").strip() == output["decision"]["action"]
        area_match = row.get("expected_business_area", "").strip() == output["business_area"]
        case_family_match = row.get("expected_case_family", "").strip() == output["case_assessment"]["case_family"]
        priority_match = row.get("expected_priority", "").strip() == output["priority"]
        review_match = expected_review_required == agent_review_required if expected_review_required else None

        exact_signal += int(signal_match)
        exact_decision += int(decision_match)
        exact_area += int(area_match)
        exact_case_family += int(case_family_match)
        exact_priority += int(priority_match)
        exact_review += int(bool(review_match))

        if expected_review_required == "false" and agent_review_required == "true":
            false_positive_review += 1
        elif expected_review_required == "true" and agent_review_required == "false":
            false_negative_review += 1

        expected_case_key = row.get("expected_case_key_if_known", "").strip()
        actual_case_key = _extract_actual_case_key(output)
        case_key_match = None
        if expected_case_key:
            case_key_compared += 1
            case_key_match = expected_case_key == actual_case_key
            case_key_exact += int(case_key_match)

        expected_references = _split_csvish(row.get("expected_key_references", ""))
        actual_references = _extract_actual_references(output)
        matched_references = sorted(expected_references.intersection(actual_references))
        reference_expected += len(expected_references)
        reference_hits += len(matched_references)

        expected_case_link_decision = row.get("expected_case_link_decision", "").strip()
        case_link_decision_match = None
        if expected_case_link_decision:
            case_link_compared += 1
            case_link_decision_match = expected_case_link_decision == stage_actual["case_link_decision"]
            case_link_exact += int(case_link_decision_match)

        expected_business_action = row.get("expected_recommended_next_action", "").strip()
        business_action_match = None
        if expected_business_action:
            business_action_compared += 1
            business_action_match = expected_business_action == stage_actual["recommended_next_action"]
            business_action_exact += int(business_action_match)

        expected_action_primary = row.get("expected_action_primary", "").strip()
        action_primary_match = None
        if expected_action_primary:
            action_primary_compared += 1
            action_primary_match = expected_action_primary == stage_actual["action_primary"]
            action_primary_exact += int(action_primary_match)

        expected_projection_mode = row.get("expected_projection_mode", "").strip()
        projection_mode_match = None
        if expected_projection_mode:
            projection_mode_compared += 1
            projection_mode_match = expected_projection_mode == stage_actual["projection_mode"]
            projection_mode_exact += int(projection_mode_match)

        expected_missing_information = _split_csvish(row.get("expected_missing_information", ""))
        actual_missing_information = set(stage_actual["missing_information"])
        matched_missing_information = sorted(expected_missing_information.intersection(actual_missing_information))
        missing_info_expected += len(expected_missing_information)
        missing_info_hits += len(matched_missing_information)
        if expected_missing_information:
            missing_info_overcalled += len(actual_missing_information - expected_missing_information)

        expected_operator_note_quality = _normalize_quality_choice(
            row.get("expected_operator_note_quality", ""),
            allowed={"weak", "usable", "strong"},
        )
        actual_operator_note_quality = _classify_operator_note_quality(stage_actual["operator_note"])
        operator_note_quality_match = None
        if expected_operator_note_quality:
            operator_note_quality_compared += 1
            operator_note_quality_match = expected_operator_note_quality == actual_operator_note_quality
            operator_note_quality_exact += int(operator_note_quality_match)

        expected_reply_should_exist = _normalize_boolish(row.get("expected_reply_should_exist"))
        reply_presence_match = None
        if expected_reply_should_exist:
            reply_presence_compared += 1
            reply_presence_match = expected_reply_should_exist == str(stage_actual["reply_draft_available"]).lower()
            reply_presence_exact += int(reply_presence_match)

        expected_reply_quality = _normalize_quality_choice(
            row.get("expected_reply_quality", ""),
            allowed={"none", "weak", "usable", "strong"},
        )
        actual_reply_quality = _classify_reply_quality(stage_actual["reply_result"])
        reply_quality_match = None
        if expected_reply_quality:
            reply_quality_compared += 1
            reply_quality_match = expected_reply_quality == actual_reply_quality
            reply_quality_exact += int(reply_quality_match)

        expected_reply_usefulness = _normalize_quality_choice(
            row.get("expected_reply_usefulness", ""),
            allowed={"none", "not_useful", "partial", "useful"},
        )
        actual_reply_usefulness = _classify_reply_usefulness(
            stage_actual["reply_result"],
            stage_actual["business_result"],
        )
        reply_usefulness_match = None
        if expected_reply_usefulness:
            reply_usefulness_compared += 1
            reply_usefulness_match = expected_reply_usefulness == actual_reply_usefulness
            reply_usefulness_exact += int(reply_usefulness_match)

        clusters = _derive_failure_clusters(
            expected_action=row.get("expected_decision_action", "").strip(),
            actual_action=output["decision"]["action"],
            expected_review_required=expected_review_required,
            actual_review_required=agent_review_required,
            expected_case_key=expected_case_key,
            actual_case_key=actual_case_key,
            expected_references=expected_references,
            actual_references=actual_references,
            expected_case_link_decision=expected_case_link_decision,
            actual_case_link_decision=stage_actual["case_link_decision"],
            expected_business_action=expected_business_action,
            actual_business_action=stage_actual["recommended_next_action"],
            expected_action_primary=expected_action_primary,
            actual_action_primary=stage_actual["action_primary"],
            expected_projection_mode=expected_projection_mode,
            actual_projection_mode=stage_actual["projection_mode"],
            expected_missing_information=expected_missing_information,
            actual_missing_information=actual_missing_information,
            expected_reply_should_exist=expected_reply_should_exist,
            actual_reply_should_exist=stage_actual["reply_draft_available"],
            expected_operator_note_quality=expected_operator_note_quality,
            actual_operator_note_quality=actual_operator_note_quality,
            reviewer_failure_cluster=row.get("reviewer_failure_cluster", "").strip(),
        )
        for cluster in clusters:
            failure_clusters[cluster] = failure_clusters.get(cluster, 0) + 1
        for hint in _split_csvish(row.get("prompt_change_hint", "")):
            prompt_change_candidates[hint] = prompt_change_candidates.get(hint, 0) + 1
        for hint in _split_csvish(row.get("threshold_change_hint", "")):
            threshold_change_candidates[hint] = threshold_change_candidates.get(hint, 0) + 1

        details.append(
            {
                "message_id": message_id,
                "status": "compared",
                "matches": {
                    "signal": signal_match,
                    "decision": decision_match,
                    "business_area": area_match,
                    "case_family": case_family_match,
                    "priority": priority_match,
                    "review_required": review_match,
                    "case_link_decision": case_link_decision_match,
                    "recommended_next_action": business_action_match,
                    "action_primary": action_primary_match,
                    "projection_mode": projection_mode_match,
                    "operator_note_quality": operator_note_quality_match,
                    "reply_presence": reply_presence_match,
                    "reply_quality": reply_quality_match,
                    "reply_usefulness": reply_usefulness_match,
                },
                "expected": {
                    "primary_signal_code": row.get("expected_primary_signal_code", "").strip(),
                    "business_area": row.get("expected_business_area", "").strip(),
                    "case_family": row.get("expected_case_family", "").strip(),
                    "decision_action": row.get("expected_decision_action", "").strip(),
                    "priority": row.get("expected_priority", "").strip(),
                    "review_required": expected_review_required,
                    "case_key_if_known": expected_case_key,
                    "key_references": row.get("expected_key_references", "").strip(),
                    "case_link_decision": expected_case_link_decision,
                    "recommended_next_action": expected_business_action,
                    "missing_information": sorted(expected_missing_information),
                    "operator_note_quality": expected_operator_note_quality,
                    "reply_should_exist": expected_reply_should_exist,
                    "reply_quality": expected_reply_quality,
                    "reply_usefulness": expected_reply_usefulness,
                    "action_primary": expected_action_primary,
                    "projection_mode": expected_projection_mode,
                    "reviewer_failure_cluster": row.get("reviewer_failure_cluster", "").strip(),
                    "prompt_change_hint": row.get("prompt_change_hint", "").strip(),
                    "threshold_change_hint": row.get("threshold_change_hint", "").strip(),
                    "reviewer_notes": row.get("reviewer_notes", "").strip(),
                },
                "actual": {
                    "primary_signal_code": output["primary_signal"]["code"],
                    "business_area": output["business_area"],
                    "case_family": output["case_assessment"]["case_family"],
                    "decision_action": output["decision"]["action"],
                    "priority": output["priority"],
                    "review_required": agent_review_required,
                    "review_flags": output["review"]["flags"],
                    "signal_confidence": output["confidence"]["signal_confidence"],
                    "case_link_confidence": output["confidence"]["case_link_confidence"],
                    "decision_confidence": output["confidence"]["decision_confidence"],
                    "case_key": actual_case_key,
                    "references": sorted(actual_references),
                    "preclassification_lane": stage_actual["preclassification_lane"],
                    "case_link_decision": stage_actual["case_link_decision"],
                    "recommended_next_action": stage_actual["recommended_next_action"],
                    "missing_information": sorted(actual_missing_information),
                    "operator_note": stage_actual["operator_note"],
                    "operator_note_quality": actual_operator_note_quality,
                    "reply_draft_available": stage_actual["reply_draft_available"],
                    "reply_quality": actual_reply_quality,
                    "reply_usefulness": actual_reply_usefulness,
                    "action_primary": stage_actual["action_primary"],
                    "projection_mode": stage_actual["projection_mode"],
                },
                "case_key_match": case_key_match,
                "reference_hits": matched_references,
                "missing_information_hits": matched_missing_information,
                "failure_clusters": clusters,
            }
        )

    summary = {
        "compared_items": compared,
        "missing_outputs": missing_outputs,
        "exact_signal_matches": exact_signal,
        "exact_decision_matches": exact_decision,
        "exact_business_area_matches": exact_area,
        "exact_case_family_matches": exact_case_family,
        "exact_priority_matches": exact_priority,
        "exact_review_requirement_matches": exact_review,
        "false_positive_review": false_positive_review,
        "false_negative_review": false_negative_review,
        "case_key_compared": case_key_compared,
        "case_key_exact_matches": case_key_exact,
        "reference_expected_count": reference_expected,
        "reference_hit_count": reference_hits,
        "case_link_decision_compared": case_link_compared,
        "case_link_decision_exact_matches": case_link_exact,
        "recommended_next_action_compared": business_action_compared,
        "recommended_next_action_exact_matches": business_action_exact,
        "action_primary_compared": action_primary_compared,
        "action_primary_exact_matches": action_primary_exact,
        "projection_mode_compared": projection_mode_compared,
        "projection_mode_exact_matches": projection_mode_exact,
        "reply_presence_compared": reply_presence_compared,
        "reply_presence_exact_matches": reply_presence_exact,
        "reply_quality_compared": reply_quality_compared,
        "reply_quality_exact_matches": reply_quality_exact,
        "reply_usefulness_compared": reply_usefulness_compared,
        "reply_usefulness_exact_matches": reply_usefulness_exact,
        "operator_note_quality_compared": operator_note_quality_compared,
        "operator_note_quality_exact_matches": operator_note_quality_exact,
        "missing_information_expected_count": missing_info_expected,
        "missing_information_hit_count": missing_info_hits,
        "missing_information_overcalled_count": missing_info_overcalled,
        "signal_accuracy": _ratio(exact_signal, compared),
        "decision_accuracy": _ratio(exact_decision, compared),
        "business_area_accuracy": _ratio(exact_area, compared),
        "case_family_accuracy": _ratio(exact_case_family, compared),
        "priority_accuracy": _ratio(exact_priority, compared),
        "review_accuracy": _ratio(exact_review, compared),
        "case_key_accuracy": _ratio(case_key_exact, case_key_compared),
        "reference_recall": _ratio(reference_hits, reference_expected),
        "case_link_decision_accuracy": _ratio(case_link_exact, case_link_compared),
        "recommended_next_action_accuracy": _ratio(business_action_exact, business_action_compared),
        "action_primary_accuracy": _ratio(action_primary_exact, action_primary_compared),
        "projection_mode_accuracy": _ratio(projection_mode_exact, projection_mode_compared),
        "reply_presence_accuracy": _ratio(reply_presence_exact, reply_presence_compared),
        "reply_quality_accuracy": _ratio(reply_quality_exact, reply_quality_compared),
        "reply_usefulness_accuracy": _ratio(reply_usefulness_exact, reply_usefulness_compared),
        "operator_note_quality_accuracy": _ratio(operator_note_quality_exact, operator_note_quality_compared),
        "missing_information_recall": _ratio(missing_info_hits, missing_info_expected),
        "failure_clusters": dict(sorted(failure_clusters.items(), key=lambda item: (-item[1], item[0]))),
        "prompt_change_candidates": dict(sorted(prompt_change_candidates.items(), key=lambda item: (-item[1], item[0]))),
        "threshold_change_candidates": dict(sorted(threshold_change_candidates.items(), key=lambda item: (-item[1], item[0]))),
        "calibration_notes": _build_calibration_notes(
            failure_clusters=failure_clusters,
            prompt_change_candidates=prompt_change_candidates,
            threshold_change_candidates=threshold_change_candidates,
        ),
        "over_review": false_positive_review,
        "under_review": false_negative_review,
    }
    if stage_records:
        summary.update(_summarize_stage_consistency(stage_records))

    return summary, details


def write_eval_summary(run_dir: Path, summary: dict[str, Any]) -> Path:
    """Persist evaluation summary to the run directory."""
    path = run_dir / RUN_ARTIFACT_FILENAMES["eval_summary"]
    write_json(path, summary)
    return path


def write_eval_details(run_dir: Path, details: list[dict[str, Any]]) -> Path:
    """Persist evaluation details to the run directory."""
    path = run_dir / RUN_ARTIFACT_FILENAMES["eval_details"]
    write_json(path, details)
    return path


def write_eval_markdown_report(run_dir: Path, summary: dict[str, Any]) -> Path:
    """Write a human-readable markdown summary for shadow-mode review loops."""
    lines = [
        "# Eval Summary",
        "",
        f"- compared_items: {summary['compared_items']}",
        f"- decision_accuracy: {summary['decision_accuracy']:.4f}",
        f"- signal_accuracy: {summary['signal_accuracy']:.4f}",
        f"- review_accuracy: {summary['review_accuracy']:.4f}",
        f"- business_area_accuracy: {summary['business_area_accuracy']:.4f}",
        f"- case_key_accuracy: {summary['case_key_accuracy']:.4f}",
        f"- reference_recall: {summary['reference_recall']:.4f}",
        "",
        "## Business And Action Quality",
        f"- case_link_decision_accuracy: {summary.get('case_link_decision_accuracy', 0.0):.4f}",
        f"- recommended_next_action_accuracy: {summary.get('recommended_next_action_accuracy', 0.0):.4f}",
        f"- action_primary_accuracy: {summary.get('action_primary_accuracy', 0.0):.4f}",
        f"- projection_mode_accuracy: {summary.get('projection_mode_accuracy', 0.0):.4f}",
        f"- operator_note_quality_accuracy: {summary.get('operator_note_quality_accuracy', 0.0):.4f}",
        f"- missing_information_recall: {summary.get('missing_information_recall', 0.0):.4f}",
        "",
        "## Reply Draft Quality",
        f"- reply_presence_accuracy: {summary.get('reply_presence_accuracy', 0.0):.4f}",
        f"- reply_quality_accuracy: {summary.get('reply_quality_accuracy', 0.0):.4f}",
        f"- reply_usefulness_accuracy: {summary.get('reply_usefulness_accuracy', 0.0):.4f}",
        "",
        "## Failure Clusters",
    ]

    failure_clusters = summary.get("failure_clusters") or {}
    if failure_clusters:
        for name, count in failure_clusters.items():
            lines.append(f"- {name}: {count}")
    else:
        lines.append("- none")

    lines.extend(["", "## Calibration Notes"])
    notes = summary.get("calibration_notes") or []
    if notes:
        for note in notes:
            lines.append(f"- {note}")
    else:
        lines.append("- no calibration notes generated")

    lines.extend(["", "## Prompt Change Candidates"])
    prompt_candidates = summary.get("prompt_change_candidates") or {}
    if prompt_candidates:
        for name, count in prompt_candidates.items():
            lines.append(f"- {name}: {count}")
    else:
        lines.append("- none")

    lines.extend(["", "## Threshold Change Candidates"])
    threshold_candidates = summary.get("threshold_change_candidates") or {}
    if threshold_candidates:
        for name, count in threshold_candidates.items():
            lines.append(f"- {name}: {count}")
    else:
        lines.append("- none")

    validation_summary = summary.get("validation_summary") or {}
    if validation_summary:
        lines.extend(["", "## Validation Origins"])
        origin_distribution = validation_summary.get("origin_distribution") or {}
        for name in ("raw_valid", "normalized_valid", "repaired_valid", "guardrailed_review", "invalid"):
            if name in origin_distribution:
                lines.append(f"- {name}: {origin_distribution[name]:.4f}")
        lines.append(f"- guardrail_applied: {validation_summary.get('guardrail_applied', 0)}")

    if "stage_consistency_compared" in summary:
        lines.extend(
            [
                "",
                "## Stage Consistency",
                f"- stage_consistency_compared: {summary['stage_consistency_compared']}",
                f"- intake_vs_business_alignment: {summary['intake_vs_business_alignment']:.4f}",
                f"- business_vs_action_alignment: {summary['business_vs_action_alignment']:.4f}",
                f"- action_vs_projection_alignment: {summary['action_vs_projection_alignment']:.4f}",
                f"- false_safe_action: {summary['false_safe_action']}",
                f"- weak_action_recommendation: {summary['weak_action_recommendation']}",
                f"- draft_usefulness: {summary['draft_usefulness']}",
                f"- case_link_instability: {summary['case_link_instability']}",
                f"- missing_information_coverage: {summary['missing_information_coverage']:.4f}",
            ]
        )

    path = run_dir / RUN_ARTIFACT_FILENAMES["eval_markdown"]
    write_text(path, "\n".join(lines) + "\n")
    return path


def _load_annotations(path: str | Path) -> list[dict[str, str]]:
    return read_csv_rows(Path(path))


def _stage_records_by_message_id(stage_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(record.get("message_id") or "").strip(): record
        for record in stage_records
        if isinstance(record, dict) and str(record.get("message_id") or "").strip()
    }


def _extract_stage_actuals(
    intake_output: dict[str, Any],
    stage_record: dict[str, Any] | None,
) -> dict[str, Any]:
    stage_record = stage_record or {}
    case_link_result = stage_record.get("case_link_result") or {}
    business_result = stage_record.get("business_reasoning_result") or {}
    reply_result = stage_record.get("reply_draft_result") or {}
    action_plan_result = stage_record.get("action_plan_result") or {}
    preclassification_result = stage_record.get("preclassification_result") or {}
    preview = stage_record.get("projection_preview") or {}

    recommended_next_action = str(business_result.get("recommended_next_action") or "").strip()
    action_primary = str(action_plan_result.get("primary_action") or "").strip()
    projection_mode = str(action_plan_result.get("daszek_projection_mode") or "").strip()
    if not projection_mode:
        preview_metadata = preview.get("metadata") if isinstance(preview.get("metadata"), dict) else {}
        projection_mode = str(preview_metadata.get("projection_mode") or "").strip()

    return {
        "preclassification_lane": str(preclassification_result.get("lane") or "intake_llm"),
        "case_link_result": case_link_result,
        "case_link_decision": str(case_link_result.get("decision") or "").strip(),
        "case_link_selected_key": str(case_link_result.get("selected_case_key") or "").strip(),
        "case_link_stage_confidence": float(case_link_result.get("confidence") or 0.0),
        "business_result": business_result,
        "business_interpretation": str(
            business_result.get("business_summary_short")
            or business_result.get("business_interpretation")
            or ""
        ).strip(),
        "recommended_next_action": recommended_next_action,
        "missing_information": _normalize_string_items(business_result.get("missing_information")),
        "operator_note": str(business_result.get("operator_note") or "").strip(),
        "reply_result": reply_result,
        "reply_draft_available": bool(reply_result.get("draft_enabled")),
        "reply_recommended_variant": str(reply_result.get("recommended_variant") or "").strip(),
        "reply_do_not_send_reasons": _normalize_string_items(reply_result.get("do_not_send_reasons")),
        "action_plan_result": action_plan_result,
        "action_primary": action_primary,
        "projection_mode": projection_mode,
        "action_safe_for_live_push": bool(action_plan_result.get("safe_for_live_push")),
    }


def _format_float(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return ""


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _extract_actual_case_key(output: dict[str, Any]) -> str:
    case_key_info = resolve_case_key_metadata(output)
    return str(case_key_info.get("case_key") or "").strip()


def _extract_actual_references(output: dict[str, Any]) -> set[str]:
    references = output.get("extracted_data", {}).get("references") or {}
    found: set[str] = set()
    for value in references.values():
        if not isinstance(value, list):
            continue
        for item in value:
            text = str(item).strip()
            if text:
                found.add(text)
    return found


def _summarize_stage_consistency(stage_records: list[dict[str, Any]]) -> dict[str, Any]:
    compared = 0
    intake_vs_business = 0
    business_vs_action = 0
    action_vs_projection = 0
    false_safe_action = 0
    weak_action_recommendation = 0
    draft_usefulness = 0
    case_link_instability = 0
    missing_information_covered = 0
    missing_information_expected = 0

    for record in stage_records:
        if not isinstance(record, dict):
            continue
        intake = record.get("intake_result_final") or {}
        business = record.get("business_reasoning_result") or {}
        action = record.get("action_plan_result") or {}
        preview = record.get("projection_preview") or {}
        reply = record.get("reply_draft_result") or {}
        case_link = record.get("case_link_result") or {}
        if not intake:
            continue

        compared += 1
        intake_action = str(intake.get("decision", {}).get("action") or "")
        business_action = str(business.get("recommended_next_action") or "")
        primary_action = str(action.get("primary_action") or "")
        projection_mode = str(action.get("daszek_projection_mode") or "")

        if _intake_vs_business_aligned(intake_action, business_action):
            intake_vs_business += 1
        if _business_vs_action_aligned(business_action, primary_action):
            business_vs_action += 1
        if _projection_aligned(projection_mode, preview.get("decision_action"), preview.get("ignored")):
            action_vs_projection += 1
        if bool(action.get("safe_for_live_push")) and (bool(intake.get("review_required")) or primary_action in {"create_review", "prepare_reply", "hold"}):
            false_safe_action += 1
        if primary_action == "hold" or not _business_vs_action_aligned(business_action, primary_action):
            weak_action_recommendation += 1
        if _classify_reply_usefulness(reply, business) == "useful":
            draft_usefulness += 1
        if str(case_link.get("decision") or "") in {"weak_link", "competing_links"}:
            case_link_instability += 1
        if business_action in {"reply", "collect_data", "create_task"}:
            missing_information_expected += 1
            if business.get("missing_information"):
                missing_information_covered += 1

    return {
        "stage_consistency_compared": compared,
        "intake_vs_business_alignment": _ratio(intake_vs_business, compared),
        "business_vs_action_alignment": _ratio(business_vs_action, compared),
        "action_vs_projection_alignment": _ratio(action_vs_projection, compared),
        "false_safe_action": false_safe_action,
        "weak_action_recommendation": weak_action_recommendation,
        "draft_usefulness": draft_usefulness,
        "case_link_instability": case_link_instability,
        "missing_information_coverage": _ratio(missing_information_covered, missing_information_expected),
    }


def _intake_vs_business_aligned(intake_action: str, business_action: str) -> bool:
    if not business_action:
        return False
    if intake_action == "ignore":
        return business_action in {"ignore", "wait"}
    if intake_action in {"append_to_existing_case", "update_case_state"}:
        return business_action in {"update_case", "reply", "collect_data", "wait"}
    if intake_action in {"create_case", "create_case_and_task", "create_task"}:
        return business_action in {"create_task", "reply", "collect_data", "call"}
    if intake_action in {"mark_reference", "mark_watchlist"}:
        return business_action in {"wait", "ignore", "reply"}
    if intake_action == "review":
        return business_action in {"escalate_review", "reply", "collect_data"}
    return False


def _business_vs_action_aligned(business_action: str, primary_action: str) -> bool:
    mapping = {
        "reply": {"prepare_reply", "create_review"},
        "collect_data": {"prepare_reply", "create_review"},
        "call": {"create_task", "create_review"},
        "create_task": {"create_task"},
        "update_case": {"update_case"},
        "wait": {"hold", "ignore"},
        "ignore": {"ignore"},
        "escalate_review": {"create_review", "hold"},
    }
    return primary_action in mapping.get(business_action, set())


def _projection_aligned(projection_mode: str, preview_action: Any, preview_ignored: Any) -> bool:
    if projection_mode == "ignore":
        return bool(preview_ignored)
    if projection_mode == "review":
        return str(preview_action or "") == "review"
    if projection_mode == "case_update":
        return str(preview_action or "") in {"append_to_existing_case", "update_case_state"}
    if projection_mode == "reference":
        return str(preview_action or "") in {"mark_reference", "mark_watchlist"}
    if projection_mode == "task":
        return str(preview_action or "") in {"create_task", "create_case", "create_case_and_task"}
    return False


def _split_csvish(raw_value: str) -> set[str]:
    return {part.strip() for part in raw_value.replace(";", ",").split(",") if part.strip()}


def _normalize_string_items(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return sorted({str(item).strip() for item in values if str(item).strip()})


def _normalize_boolish(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "1"}:
        return "true"
    if text in {"false", "no", "0"}:
        return "false"
    return ""


def _normalize_quality_choice(value: Any, *, allowed: set[str]) -> str:
    text = str(value or "").strip().lower()
    if text in allowed:
        return text
    return ""


def _classify_operator_note_quality(note: str) -> str:
    text = str(note or "").strip()
    if not text:
        return "weak"
    lowered = text.lower()
    action_tokens = sum(
        1
        for token in ("review", "verify", "confirm", "reply", "call", "collect", "update", "escalate")
        if token in lowered
    )
    if len(text) >= 90 or action_tokens >= 2:
        return "strong"
    if len(text) >= 30 or action_tokens >= 1:
        return "usable"
    return "weak"


def _classify_reply_quality(reply_result: dict[str, Any]) -> str:
    if not isinstance(reply_result, dict) or not reply_result.get("draft_enabled"):
        return "none"
    drafts = reply_result.get("drafts") or []
    if not drafts:
        return "none"
    first = drafts[0] if isinstance(drafts[0], dict) else {}
    body = str(first.get("body") or "").strip()
    subject = str(first.get("subject_suggestion") or "").strip()
    if len(body) >= 140 and subject:
        return "strong"
    if len(body) >= 60:
        return "usable"
    return "weak"


def _classify_reply_usefulness(reply_result: dict[str, Any], business_result: dict[str, Any]) -> str:
    if not isinstance(reply_result, dict) or not reply_result.get("draft_enabled"):
        return "none"
    business_action = str((business_result or {}).get("recommended_next_action") or "").strip()
    missing_information = _normalize_string_items((business_result or {}).get("missing_information"))
    drafts = reply_result.get("drafts") or []
    body = str(((drafts[0] if drafts and isinstance(drafts[0], dict) else {}).get("body") or "")).lower()
    if business_action in {"reply", "collect_data"}:
        if missing_information and any(item.lower() in body for item in missing_information[:3]):
            return "useful"
        return "partial"
    if business_action == "call":
        return "partial"
    return "not_useful"


def _derive_failure_clusters(
    *,
    expected_action: str,
    actual_action: str,
    expected_review_required: str,
    actual_review_required: str,
    expected_case_key: str,
    actual_case_key: str,
    expected_references: set[str],
    actual_references: set[str],
    expected_case_link_decision: str,
    actual_case_link_decision: str,
    expected_business_action: str,
    actual_business_action: str,
    expected_action_primary: str,
    actual_action_primary: str,
    expected_projection_mode: str,
    actual_projection_mode: str,
    expected_missing_information: set[str],
    actual_missing_information: set[str],
    expected_reply_should_exist: str,
    actual_reply_should_exist: bool,
    expected_operator_note_quality: str,
    actual_operator_note_quality: str,
    reviewer_failure_cluster: str,
) -> list[str]:
    clusters: list[str] = []
    if reviewer_failure_cluster:
        clusters.extend(sorted(_split_csvish(reviewer_failure_cluster)))

    if expected_action and expected_action != actual_action:
        if actual_action == "ignore":
            clusters.append("false_ignore")
        elif expected_action == "ignore":
            clusters.append("false_action")
        if ({expected_action, actual_action} & NEW_CASE_ACTIONS) and ({expected_action, actual_action} & UPDATE_ACTIONS):
            clusters.append("new_case_vs_update_mismatch")
        if expected_action in ACTION_DECISIONS and actual_action in REFERENCE_DECISIONS:
            clusters.append("action_buried_as_reference")
        if expected_action in REFERENCE_DECISIONS and actual_action in ACTION_DECISIONS:
            clusters.append("reference_promoted_to_action")

    if expected_review_required == "true" and actual_review_required == "false":
        clusters.append("missed_review_gate")
    elif expected_review_required == "false" and actual_review_required == "true":
        clusters.append("over_escalated_review")

    if expected_case_key and expected_case_key != actual_case_key:
        clusters.append("case_link_mismatch")
    if expected_case_link_decision and expected_case_link_decision != actual_case_link_decision:
        clusters.append("case_link_decision_mismatch")

    if expected_references and not expected_references.issubset(actual_references):
        clusters.append("reference_extraction_miss")
    if expected_missing_information and not expected_missing_information.issubset(actual_missing_information):
        clusters.append("missing_information_gap")
    if expected_business_action and expected_business_action != actual_business_action:
        clusters.append("business_action_mismatch")
    if expected_action_primary and expected_action_primary != actual_action_primary:
        clusters.append("action_plan_mismatch")
    if expected_projection_mode and expected_projection_mode != actual_projection_mode:
        clusters.append("projection_mode_mismatch")
    if expected_reply_should_exist == "true" and not actual_reply_should_exist:
        clusters.append("missing_reply_draft")
    elif expected_reply_should_exist == "false" and actual_reply_should_exist:
        clusters.append("unnecessary_reply_draft")
    if expected_operator_note_quality and expected_operator_note_quality != actual_operator_note_quality:
        clusters.append("weak_operator_note")

    return sorted(set(clusters))


def _build_calibration_notes(
    *,
    failure_clusters: dict[str, int],
    prompt_change_candidates: dict[str, int],
    threshold_change_candidates: dict[str, int],
) -> list[str]:
    notes: list[str] = []
    if failure_clusters.get("false_ignore", 0) > 0:
        notes.append("Review ignore thresholds and make prompt language stricter around business references and deadlines.")
    if failure_clusters.get("new_case_vs_update_mismatch", 0) > 0:
        notes.append("Strengthen prompt examples and guardrails for new-case vs existing-case decisions.")
    if failure_clusters.get("case_link_mismatch", 0) > 0 or failure_clusters.get("case_link_decision_mismatch", 0) > 0:
        notes.append("Review case-link heuristics and thresholds before widening case-update confidence.")
    if failure_clusters.get("reference_extraction_miss", 0) > 0:
        notes.append("Improve extraction examples for invoice, order, shipment, and transaction references.")
    if failure_clusters.get("business_action_mismatch", 0) > 0:
        notes.append("Tighten business_reasoner instructions around recommended_next_action and missing-information priorities.")
    if failure_clusters.get("action_plan_mismatch", 0) > 0 or failure_clusters.get("projection_mode_mismatch", 0) > 0:
        notes.append("Inspect action-planner mappings so primary action, projection mode, and preview stay aligned.")
    if failure_clusters.get("missing_information_gap", 0) > 0:
        notes.append("Expand missing-info rules and examples so the business layer captures the real operator blockers.")
    if failure_clusters.get("missing_reply_draft", 0) > 0 or failure_clusters.get("unnecessary_reply_draft", 0) > 0:
        notes.append("Adjust should_draft_reply thresholds and reply prompt guidance to better match operator usefulness.")
    if failure_clusters.get("weak_operator_note", 0) > 0:
        notes.append("Strengthen operator-note guidance so notes are short, actionable, and concrete.")
    if prompt_change_candidates:
        notes.append("Human reviewers suggested prompt changes; inspect prompt_change_candidates in eval_summary.json.")
    if threshold_change_candidates:
        notes.append("Human reviewers suggested threshold changes; inspect threshold_change_candidates in eval_summary.json.")
    return notes
