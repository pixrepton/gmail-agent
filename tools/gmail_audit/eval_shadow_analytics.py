"""Map eval shadow / human review outputs → feedback analytics groups (read-only, offline)."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from artifact_io import read_csv_rows, write_csv, write_json, write_jsonl
from feedback_event_contract import (
    FeedbackAnalyticsGroup,
    build_feedback_analytics_key,
    normalize_feedback_analytics_group,
)
from feedback_analytics_export import write_feedback_analytics_csv, write_feedback_analytics_jsonl

EVAL_SHADOW_EVENT_DOMAIN = "eval_shadow"

_FORBIDDEN_EVAL_EXPORT_KEYS = frozenset(
    {
        "body",
        "snippet",
        "raw_body",
        "prompt",
        "detail",
        "note",
        "summary_text",
        "payload",
        "subject",
        "sender",
        "reviewer_notes",
        "agent_business_interpretation",
        "agent_operator_note",
    }
)

_FAILURE_CLUSTER_TO_ANALYTICS_GROUP: dict[str, FeedbackAnalyticsGroup] = {
    "false_ignore": "routing_quality",
    "false_action": "routing_quality",
    "new_case_vs_update_mismatch": "case_link_quality",
    "action_buried_as_reference": "routing_quality",
    "reference_promoted_to_action": "routing_quality",
    "missed_review_gate": "policy_quality",
    "over_escalated_review": "policy_quality",
    "case_link_mismatch": "case_link_quality",
    "case_link_decision_mismatch": "case_link_quality",
    "reference_extraction_miss": "evidence_quality",
    "missing_information_gap": "evidence_quality",
    "business_action_mismatch": "decision_quality",
    "action_plan_mismatch": "decision_quality",
    "projection_mode_mismatch": "decision_quality",
    "missing_reply_draft": "draft_quality",
    "unnecessary_reply_draft": "draft_quality",
    "weak_operator_note": "draft_quality",
    "wrong_topic": "routing_quality",
    "wrong_routing": "routing_quality",
    "wrong_priority": "priority_quality",
    "wrong_case": "case_link_quality",
    "wrong_draft": "draft_quality",
    "bad_draft": "draft_quality",
    "policy_block": "policy_quality",
    "rejected_fact_claim": "evidence_quality",
}

_EVAL_MATCH_FIELD_TO_ANALYTICS_GROUP: dict[str, FeedbackAnalyticsGroup] = {
    "signal": "routing_quality",
    "business_area": "routing_quality",
    "case_family": "routing_quality",
    "priority": "priority_quality",
    "review_required": "policy_quality",
    "case_link_decision": "case_link_quality",
    "recommended_next_action": "decision_quality",
    "action_primary": "decision_quality",
    "projection_mode": "decision_quality",
    "reply_presence": "draft_quality",
    "reply_quality": "draft_quality",
    "reply_usefulness": "draft_quality",
    "operator_note_quality": "draft_quality",
}

_HINT_TOKEN_TO_ANALYTICS_GROUP: dict[str, FeedbackAnalyticsGroup] = {
    "routing": "routing_quality",
    "topic": "routing_quality",
    "signal": "routing_quality",
    "business_area": "routing_quality",
    "priority": "priority_quality",
    "sla": "priority_quality",
    "case_link": "case_link_quality",
    "case_key": "case_link_quality",
    "draft": "draft_quality",
    "reply": "draft_quality",
    "policy": "policy_quality",
    "review_gate": "policy_quality",
    "evidence": "evidence_quality",
    "reference": "evidence_quality",
    "missing_info": "evidence_quality",
    "fact": "evidence_quality",
    "decision": "decision_quality",
    "action": "decision_quality",
    "projection": "decision_quality",
}


@dataclass(slots=True)
class EvalShadowAnalyticsExportSummary:
    input_count: int = 0
    exported_count: int = 0
    skipped_count: int = 0
    by_group: dict[str, int] = field(default_factory=dict)
    by_signal_source: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def eval_failure_cluster_to_analytics_group(cluster: str) -> FeedbackAnalyticsGroup:
    key = str(cluster or "").strip().lower()
    if not key:
        return "unknown"
    return _FAILURE_CLUSTER_TO_ANALYTICS_GROUP.get(key, "operator_correction")


def eval_match_field_to_analytics_group(field: str) -> FeedbackAnalyticsGroup:
    key = str(field or "").strip().lower()
    if not key:
        return "unknown"
    return _EVAL_MATCH_FIELD_TO_ANALYTICS_GROUP.get(key, "operator_correction")


def eval_hint_token_to_analytics_group(token: str) -> FeedbackAnalyticsGroup:
    key = str(token or "").strip().lower()
    if not key:
        return "unknown"
    if key in _HINT_TOKEN_TO_ANALYTICS_GROUP:
        return _HINT_TOKEN_TO_ANALYTICS_GROUP[key]
    for prefix, group in _HINT_TOKEN_TO_ANALYTICS_GROUP.items():
        if key.startswith(prefix):
            return group
    return "operator_correction"


def extract_eval_shadow_correlation_refs(row: dict[str, Any]) -> dict[str, str]:
    """IDs only — no subject/sender/notes."""
    if not isinstance(row, dict):
        return {}
    refs: dict[str, str] = {}
    message_id = str(row.get("message_id") or "").strip()
    if message_id:
        refs["message_id"] = message_id
    for key in (
        "case_id",
        "source_signal_id",
        "signal_id",
        "decision_candidate_id",
        "policy_decision_id",
        "proposal_id",
        "action_proposal_id",
    ):
        value = str(row.get(key) or "").strip()
        if value:
            refs[key] = value
    case_key = str(row.get("expected_case_key_if_known") or row.get("agent_case_key") or "").strip()
    if case_key and "case_id" not in refs:
        refs["case_key"] = case_key
    proposal = str(refs.get("proposal_id") or refs.get("action_proposal_id") or "").strip()
    if proposal:
        refs["proposal_id"] = proposal
    return {k: v for k, v in refs.items() if k not in _FORBIDDEN_EVAL_EXPORT_KEYS and v}


def build_eval_shadow_analytics_record(
    *,
    analytics_group: str,
    category_or_kind: str,
    correlation_refs: dict[str, str] | None = None,
    observed_at: str = "",
    signal_source: str = "",
) -> dict[str, Any]:
    """
    Sanitized analytics record compatible with feedback export shape.
    Eval shadow is always offline; ``mutates_truth`` is always false.
    """
    group = normalize_feedback_analytics_group(analytics_group)
    kind = str(category_or_kind or "").strip() or "unspecified"
    refs = dict(correlation_refs or {})
    record: dict[str, Any] = {
        "analytics_group": group,
        "event_domain": EVAL_SHADOW_EVENT_DOMAIN,
        "category_or_kind": kind,
        "mutates_truth": False,
        "correlation_refs": refs,
        "analytics_key": build_feedback_analytics_key(
            analytics_group=group,
            event_domain=EVAL_SHADOW_EVENT_DOMAIN,
            category_or_kind=kind,
            correlation_refs=refs,
        ),
    }
    if signal_source:
        record["signal_source"] = signal_source
    if observed_at:
        record["observed_at"] = observed_at
    message_id = str(refs.get("message_id") or "").strip()
    if message_id:
        record["event_id"] = f"es_{message_id}_{kind}"[:128]
    return record


def _split_csvish(raw: str) -> list[str]:
    return [part.strip() for part in str(raw or "").replace(";", ",").split(",") if part.strip()]


def build_eval_shadow_analytics_records_from_review_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive analytics records from shadow-review CSV row (failure clusters + hint tokens only)."""
    if not isinstance(row, dict):
        return []
    refs = extract_eval_shadow_correlation_refs(row)
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def _append(group: str, kind: str, source: str) -> None:
        dedupe_key = (group, kind, str(refs.get("message_id") or ""))
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        rec = build_eval_shadow_analytics_record(
            analytics_group=group,
            category_or_kind=kind,
            correlation_refs=refs,
            signal_source=source,
        )
        if not _contains_forbidden_eval_export_content(rec):
            records.append(rec)

    for cluster in _split_csvish(str(row.get("reviewer_failure_cluster") or "")):
        group = eval_failure_cluster_to_analytics_group(cluster)
        _append(group, cluster, "reviewer_failure_cluster")

    for hint_field in ("prompt_change_hint", "threshold_change_hint"):
        for token in _split_csvish(str(row.get(hint_field) or "")):
            group = eval_hint_token_to_analytics_group(token)
            _append(group, token, hint_field)

    return records


def build_eval_shadow_analytics_records_from_eval_detail(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive analytics records from ``evaluate_annotations`` detail row (field mismatches)."""
    if not isinstance(detail, dict) or str(detail.get("status") or "") != "compared":
        return []
    refs: dict[str, str] = {}
    message_id = str(detail.get("message_id") or "").strip()
    if message_id:
        refs["message_id"] = message_id
    expected = detail.get("expected") if isinstance(detail.get("expected"), dict) else {}
    case_key = str(expected.get("case_key_if_known") or "").strip()
    if case_key:
        refs["case_key"] = case_key

    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    matches = detail.get("matches") if isinstance(detail.get("matches"), dict) else {}

    for field, matched in matches.items():
        if matched is not False:
            continue
        group = eval_match_field_to_analytics_group(str(field))
        kind = f"{field}_mismatch"
        dedupe_key = (group, kind, message_id)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        rec = build_eval_shadow_analytics_record(
            analytics_group=group,
            category_or_kind=kind,
            correlation_refs=refs,
            signal_source="eval_match",
        )
        if not _contains_forbidden_eval_export_content(rec):
            records.append(rec)

    for cluster in detail.get("failure_clusters") or []:
        if not isinstance(cluster, str):
            continue
        group = eval_failure_cluster_to_analytics_group(cluster)
        kind = cluster
        dedupe_key = (group, kind, message_id)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        rec = build_eval_shadow_analytics_record(
            analytics_group=group,
            category_or_kind=kind,
            correlation_refs=refs,
            signal_source="derived_failure_cluster",
        )
        if not _contains_forbidden_eval_export_content(rec):
            records.append(rec)

    return records


def export_eval_shadow_analytics_records(
    rows: Iterable[dict[str, Any]],
    *,
    from_eval_details: bool = False,
) -> tuple[list[dict[str, Any]], EvalShadowAnalyticsExportSummary]:
    summary = EvalShadowAnalyticsExportSummary()
    exported: list[dict[str, Any]] = []

    for row in rows:
        summary.input_count += 1
        if not isinstance(row, dict):
            summary.skipped_count += 1
            continue
        if from_eval_details:
            batch = build_eval_shadow_analytics_records_from_eval_detail(row)
        else:
            batch = build_eval_shadow_analytics_records_from_review_row(row)
        if not batch:
            summary.skipped_count += 1
            continue
        for rec in batch:
            exported.append(rec)
            summary.exported_count += 1
            group = str(rec.get("analytics_group") or "unknown")
            summary.by_group[group] = summary.by_group.get(group, 0) + 1
            source = str(rec.get("signal_source") or "unknown")
            summary.by_signal_source[source] = summary.by_signal_source.get(source, 0) + 1

    return exported, summary


def export_eval_shadow_analytics_from_csv(path: Path) -> tuple[list[dict[str, Any]], EvalShadowAnalyticsExportSummary]:
    return export_eval_shadow_analytics_records(read_csv_rows(path), from_eval_details=False)


def export_eval_shadow_analytics_from_eval_details(
    details: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], EvalShadowAnalyticsExportSummary]:
    return export_eval_shadow_analytics_records(details, from_eval_details=True)


def _contains_forbidden_eval_export_content(record: dict[str, Any]) -> bool:
    for key in record:
        if key in _FORBIDDEN_EVAL_EXPORT_KEYS:
            return True
    refs = record.get("correlation_refs")
    if isinstance(refs, dict):
        for key in refs:
            if key in _FORBIDDEN_EVAL_EXPORT_KEYS:
                return True
    serialized = json.dumps(record, ensure_ascii=False, default=str)
    for token in ('"subject"', '"sender"', '"reviewer_notes"', '"agent_operator_note"'):
        if token in serialized:
            return True
    return False


def run_eval_shadow_export_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export eval shadow review CSV or eval_details JSON to analytics records (read-only).",
    )
    parser.add_argument("--input-csv", type=Path, help="Shadow review / human_annotations CSV.")
    parser.add_argument("--input-eval-details", type=Path, help="eval_details.json (list of detail dicts).")
    parser.add_argument("--output-jsonl", type=Path, help="Write sanitized analytics JSONL.")
    parser.add_argument("--output-csv", type=Path, help="Write flattened CSV.")
    parser.add_argument("--summary", type=Path, help="Write export summary JSON.")
    args = parser.parse_args(argv)

    if not args.input_csv and not args.input_eval_details:
        parser.error("Provide --input-csv and/or --input-eval-details")

    records: list[dict[str, Any]] = []
    summary = EvalShadowAnalyticsExportSummary()

    if args.input_csv:
        batch, part = export_eval_shadow_analytics_from_csv(args.input_csv)
        records.extend(batch)
        summary.input_count += part.input_count
        summary.exported_count += part.exported_count
        summary.skipped_count += part.skipped_count
        for key, value in part.by_group.items():
            summary.by_group[key] = summary.by_group.get(key, 0) + value
        for key, value in part.by_signal_source.items():
            summary.by_signal_source[key] = summary.by_signal_source.get(key, 0) + value

    if args.input_eval_details:
        import json as _json

        payload = _json.loads(Path(args.input_eval_details).read_text(encoding="utf-8-sig"))
        details = payload if isinstance(payload, list) else []
        batch, part = export_eval_shadow_analytics_from_eval_details(details)
        records.extend(batch)
        summary.input_count += part.input_count
        summary.exported_count += part.exported_count
        summary.skipped_count += part.skipped_count
        for key, value in part.by_group.items():
            summary.by_group[key] = summary.by_group.get(key, 0) + value
        for key, value in part.by_signal_source.items():
            summary.by_signal_source[key] = summary.by_signal_source.get(key, 0) + value

    if args.output_jsonl:
        write_feedback_analytics_jsonl(args.output_jsonl, records)
    if args.output_csv:
        write_feedback_analytics_csv(args.output_csv, records)
    if args.summary:
        write_json(args.summary, summary.to_dict())
    elif not args.output_jsonl and not args.output_csv:
        print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))

    return 0


def main() -> int:
    return run_eval_shadow_export_cli()


if __name__ == "__main__":
    raise SystemExit(main())
