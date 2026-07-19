"""Read-only export of feedback/adjudication events → sanitized analytics records."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from artifact_io import read_jsonl, write_csv, write_json, write_jsonl
from feedback_event_contract import (
    EVENT_TYPE_ADJUDICATION,
    EVENT_TYPE_FEEDBACK_CALIBRATION,
    build_feedback_analytics_record,
    validate_adjudication_event,
    validate_feedback_event,
)

_FEEDBACK_EVENT_TYPES = frozenset({EVENT_TYPE_FEEDBACK_CALIBRATION, EVENT_TYPE_ADJUDICATION})

_FORBIDDEN_EXPORT_TOP_KEYS = frozenset(
    {
        "body",
        "snippet",
        "raw_body",
        "prompt",
        "detail",
        "note",
        "summary_text",
        "payload",
        "tags",
        "target_refs",
        "operator_id",
        "submitted_by",
        "rating",
    }
)

_CSV_FIELDNAMES = (
    "analytics_group",
    "event_domain",
    "category_or_kind",
    "mutates_truth",
    "analytics_key",
    "observed_at",
    "event_id",
    "case_id",
    "source_signal_id",
    "decision_candidate_id",
    "policy_decision_id",
    "proposal_id",
)


@dataclass(slots=True)
class FeedbackAnalyticsExportSummary:
    input_count: int = 0
    exported_count: int = 0
    skipped_count: int = 0
    invalid_count: int = 0
    by_group: dict[str, int] = field(default_factory=dict)
    by_domain: dict[str, int] = field(default_factory=dict)
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def iter_jsonl_feedback_inputs(path: Path) -> Iterator[dict[str, Any]]:
    """Yield raw dict rows from a JSONL file (no mutation)."""
    for row in read_jsonl(path):
        if isinstance(row, dict):
            yield row


def iter_feedback_events_from_inputs(rows: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Yield normalized contract-shaped event dicts without mutating inputs."""
    for row in rows:
        normalized = normalize_feedback_event_input(row)
        if normalized is not None:
            yield normalized


def normalize_feedback_event_input(row: dict[str, Any]) -> dict[str, Any] | None:
    """
    Adapt mailbox-memory rows, bridge payloads, or contract dicts into FeedbackEvent/AdjudicationEvent shape.
    Returns a shallow copy; does not mutate ``row``.
    """
    if not isinstance(row, dict):
        return None

    event_class = str(row.get("event_class") or "").strip()
    if event_class in {"FeedbackEvent", "AdjudicationEvent"}:
        return _merge_row_shell(dict(row), row)

    event_type = str(row.get("event_type") or "").strip()
    if event_type in _FEEDBACK_EVENT_TYPES:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            return None
        return _merge_row_shell(dict(payload), row)

    if row.get("adjudication_kind") or row.get("calibration_category") or row.get("rating"):
        merged = dict(row)
        if row.get("adjudication_kind"):
            merged.setdefault("event_class", "AdjudicationEvent")
        else:
            merged.setdefault("event_class", "FeedbackEvent")
        return merged

    return None


def _merge_row_shell(event: dict[str, Any], shell: dict[str, Any]) -> dict[str, Any]:
    for key in ("event_id", "case_id", "occurred_at", "trace_id"):
        if not str(event.get(key) or "").strip() and str(shell.get(key) or "").strip():
            event[key] = shell[key]
    return event


def validate_feedback_event_input(event: dict[str, Any]) -> list[str]:
    """Return validation errors; empty list means exportable."""
    if str(event.get("event_class") or "") == "AdjudicationEvent" or event.get("adjudication_kind"):
        return validate_adjudication_event(event)
    return validate_feedback_event(event)


def build_sanitized_analytics_export_record(
    event: dict[str, Any],
    *,
    observed_at: str = "",
) -> dict[str, Any]:
    """
    Projection-safe export row: analytics slice + optional timestamp + event_id.
    Never includes detail/body/snippet/prompt/payload.
    """
    analytics = build_feedback_analytics_record(event)
    refs = dict(analytics.get("correlation_refs") or {})
    occurred = str(observed_at or event.get("occurred_at") or event.get("submitted_at") or "").strip()
    export: dict[str, Any] = {
        "analytics_group": analytics["analytics_group"],
        "event_domain": analytics["event_domain"],
        "category_or_kind": analytics["category_or_kind"],
        "mutates_truth": bool(analytics.get("mutates_truth")),
        "correlation_refs": refs,
        "analytics_key": analytics["analytics_key"],
    }
    if occurred:
        export["observed_at"] = occurred
    event_id = str(refs.get("event_id") or refs.get("feedback_event_id") or refs.get("adjudication_event_id") or "").strip()
    if event_id:
        export["event_id"] = event_id
    return export


def export_feedback_analytics_records(
    rows: Iterable[dict[str, Any]],
    *,
    skip_invalid: bool = True,
) -> tuple[list[dict[str, Any]], FeedbackAnalyticsExportSummary]:
    """
    Transform input rows into sanitized analytics export records.
    Inputs are not mutated. Invalid rows are skipped when ``skip_invalid`` is true.
    """
    summary = FeedbackAnalyticsExportSummary()
    exported: list[dict[str, Any]] = []

    for raw in rows:
        summary.input_count += 1
        normalized = normalize_feedback_event_input(raw) if isinstance(raw, dict) else None
        if normalized is None:
            summary.skipped_count += 1
            summary.skip_reasons["unrecognized_shape"] = summary.skip_reasons.get("unrecognized_shape", 0) + 1
            continue

        errs = validate_feedback_event_input(normalized)
        if errs:
            summary.invalid_count += 1
            if skip_invalid:
                summary.skipped_count += 1
                summary.skip_reasons["validation_failed"] = summary.skip_reasons.get("validation_failed", 0) + 1
                continue
            raise ValueError(f"invalid feedback event: {errs}")

        record = build_sanitized_analytics_export_record(normalized)
        if _contains_forbidden_export_content(record):
            summary.skipped_count += 1
            summary.skip_reasons["forbidden_content"] = summary.skip_reasons.get("forbidden_content", 0) + 1
            continue

        exported.append(record)
        summary.exported_count += 1
        group = str(record.get("analytics_group") or "unknown")
        domain = str(record.get("event_domain") or "unknown")
        summary.by_group[group] = summary.by_group.get(group, 0) + 1
        summary.by_domain[domain] = summary.by_domain.get(domain, 0) + 1

    return exported, summary


def export_feedback_analytics_from_jsonl(
    input_path: Path,
    *,
    skip_invalid: bool = True,
) -> tuple[list[dict[str, Any]], FeedbackAnalyticsExportSummary]:
    return export_feedback_analytics_records(iter_jsonl_feedback_inputs(input_path), skip_invalid=skip_invalid)


def iter_feedback_events_from_store(store: Any, *, limit: int = 5000) -> Iterator[dict[str, Any]]:
    """Read-only: fetch mailbox_memory event rows for feedback/adjudication types."""
    if not hasattr(store, "fetch_events"):
        return iter(())
    rows = store.fetch_events(
        event_types=(EVENT_TYPE_FEEDBACK_CALIBRATION, EVENT_TYPE_ADJUDICATION),
        limit=limit,
    )
    return iter_feedback_events_from_inputs(rows)


def export_feedback_analytics_from_store(
    store: Any,
    *,
    limit: int = 5000,
    skip_invalid: bool = True,
) -> tuple[list[dict[str, Any]], FeedbackAnalyticsExportSummary]:
    """Read-only export from a mailbox memory store (in-memory or Postgres)."""
    shell_rows: list[dict[str, Any]] = []
    if hasattr(store, "fetch_events"):
        shell_rows = list(
            store.fetch_events(
                event_types=(EVENT_TYPE_FEEDBACK_CALIBRATION, EVENT_TYPE_ADJUDICATION),
                limit=limit,
            )
        )
    return export_feedback_analytics_records(shell_rows, skip_invalid=skip_invalid)


def flatten_analytics_record_for_csv(record: dict[str, Any]) -> dict[str, str]:
    refs = record.get("correlation_refs") if isinstance(record.get("correlation_refs"), dict) else {}
    return {
        "analytics_group": str(record.get("analytics_group") or ""),
        "event_domain": str(record.get("event_domain") or ""),
        "category_or_kind": str(record.get("category_or_kind") or ""),
        "mutates_truth": str(bool(record.get("mutates_truth"))).lower(),
        "analytics_key": str(record.get("analytics_key") or ""),
        "observed_at": str(record.get("observed_at") or ""),
        "event_id": str(record.get("event_id") or refs.get("event_id") or ""),
        "case_id": str(refs.get("case_id") or ""),
        "source_signal_id": str(refs.get("source_signal_id") or refs.get("signal_id") or ""),
        "decision_candidate_id": str(refs.get("decision_candidate_id") or ""),
        "policy_decision_id": str(refs.get("policy_decision_id") or ""),
        "proposal_id": str(refs.get("proposal_id") or refs.get("action_proposal_id") or ""),
    }


def write_feedback_analytics_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    write_jsonl(path, records)


def write_feedback_analytics_csv(path: Path, records: list[dict[str, Any]]) -> None:
    write_csv(path, list(_CSV_FIELDNAMES), [flatten_analytics_record_for_csv(r) for r in records])


def _contains_forbidden_export_content(record: dict[str, Any]) -> bool:
    """Detect accidental leakage of free-text / payload keys into export records."""
    for key in record:
        if key in _FORBIDDEN_EXPORT_TOP_KEYS:
            return True
    refs = record.get("correlation_refs")
    if isinstance(refs, dict):
        for key in refs:
            if key in _FORBIDDEN_EXPORT_TOP_KEYS:
                return True
    serialized = json.dumps(record, ensure_ascii=False, default=str)
    for token in ('"detail"', '"body"', '"snippet"', '"prompt"', '"payload"'):
        if token in serialized:
            return True
    return False


def run_export_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export sanitized feedback analytics records from JSONL (read-only, no DB by default).",
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Input JSONL: FeedbackEvent/AdjudicationEvent dicts or mailbox_memory event rows.",
    )
    parser.add_argument("--output-jsonl", type=Path, help="Write sanitized analytics JSONL.")
    parser.add_argument("--output-csv", type=Path, help="Write flattened CSV (safe scalar fields only).")
    parser.add_argument("--summary", type=Path, help="Write export summary JSON.")
    parser.add_argument(
        "--fail-on-invalid",
        action="store_true",
        help="Exit non-zero when any input row fails validation (default: skip).",
    )
    args = parser.parse_args(argv)

    records, summary = export_feedback_analytics_from_jsonl(args.input, skip_invalid=not args.fail_on_invalid)
    if args.fail_on_invalid and summary.invalid_count:
        print(f"invalid rows: {summary.invalid_count}", file=sys.stderr)
        return 2

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
    return run_export_cli()


if __name__ == "__main__":
    raise SystemExit(main())
