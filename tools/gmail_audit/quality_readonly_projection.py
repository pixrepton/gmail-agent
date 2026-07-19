"""Read-only quality projection: merge feedback + eval shadow analytics into one safe payload."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from artifact_io import read_jsonl, write_json
from daszek_v3_operational_feed_contract import strip_forbidden_nested
from feedback_event_contract import normalize_feedback_analytics_group

QUALITY_READONLY_SCHEMA_VERSION = "quality_readonly_projection.v1"
QUALITY_READONLY_PROJECTION_TYPE = "quality_readonly"
DEFAULT_RECENT_RECORD_LIMIT = 25

_DEFAULT_NOT_PROVEN: tuple[str, ...] = (
    "not_live_node_b_proof",
    "not_gate_b_proof",
    "not_production_mailbox_export",
    "local_fixture_or_export_file_only",
)

_RECORD_ALLOWLIST = frozenset(
    {
        "analytics_group",
        "event_domain",
        "category_or_kind",
        "mutates_truth",
        "correlation_refs",
        "analytics_key",
        "observed_at",
        "event_id",
        "signal_source",
    }
)

_CORRELATION_SUMMARY_KEYS: tuple[str, ...] = (
    "case_id",
    "source_signal_id",
    "decision_candidate_id",
    "policy_decision_id",
    "proposal_id",
)

_FORBIDDEN_QUALITY_KEYS: frozenset[str] = frozenset(
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
        "tags",
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sanitize_analytics_record_for_projection(record: dict[str, Any]) -> dict[str, Any] | None:
    """Return a shallow copy with allowlisted fields only; None if record is unusable."""
    if not isinstance(record, dict):
        return None
    group = normalize_feedback_analytics_group(str(record.get("analytics_group") or "unknown"))
    domain = str(record.get("event_domain") or "unknown").strip() or "unknown"
    kind = str(record.get("category_or_kind") or "").strip() or "unspecified"
    refs_in = record.get("correlation_refs") if isinstance(record.get("correlation_refs"), dict) else {}
    refs: dict[str, str] = {}
    for key, value in refs_in.items():
        if key in _FORBIDDEN_QUALITY_KEYS:
            continue
        text = str(value or "").strip()
        if text:
            refs[key] = text[:128]
    proposal = str(refs.get("proposal_id") or refs.get("action_proposal_id") or "").strip()
    if proposal:
        refs["proposal_id"] = proposal
    out: dict[str, Any] = {
        "analytics_group": group,
        "event_domain": domain,
        "category_or_kind": kind,
        "mutates_truth": bool(record.get("mutates_truth")),
        "correlation_refs": refs,
        "analytics_key": str(record.get("analytics_key") or "")[:512],
    }
    observed = str(record.get("observed_at") or "").strip()
    if observed:
        out["observed_at"] = observed[:64]
    event_id = str(record.get("event_id") or refs.get("event_id") or "").strip()
    if event_id:
        out["event_id"] = event_id[:128]
    signal_source = str(record.get("signal_source") or "").strip()
    if signal_source and domain == "eval_shadow":
        out["signal_source"] = signal_source[:64]
    if _contains_forbidden_quality_content(out):
        return None
    return out


def _contains_forbidden_quality_content(record: dict[str, Any]) -> bool:
    serialized = json.dumps(record, ensure_ascii=False, default=str)
    for token in ('"body"', '"snippet"', '"prompt"', '"detail"', '"reviewer_notes"', '"payload"'):
        if token in serialized:
            return True
    return False


def build_quality_readonly_projection(
    feedback_records: Iterable[dict[str, Any]] | None = None,
    eval_shadow_records: Iterable[dict[str, Any]] | None = None,
    *,
    recent_limit: int = DEFAULT_RECENT_RECORD_LIMIT,
    generated_at: str | None = None,
    export_skipped_count: int = 0,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """
    Merge sanitized feedback and eval-shadow analytics records into one projection payload.
    Inputs are not mutated.
    """
    feedback_safe: list[dict[str, Any]] = []
    eval_safe: list[dict[str, Any]] = []
    skipped = 0

    for raw in list(feedback_records or []):
        rec = sanitize_analytics_record_for_projection(raw) if isinstance(raw, dict) else None
        if rec is None:
            skipped += 1
            continue
        if str(rec.get("event_domain") or "") == "eval_shadow":
            eval_safe.append(rec)
        else:
            feedback_safe.append(rec)

    for raw in list(eval_shadow_records or []):
        rec = sanitize_analytics_record_for_projection(raw) if isinstance(raw, dict) else None
        if rec is None:
            skipped += 1
            continue
        eval_safe.append(rec)

    merged = feedback_safe + eval_safe
    by_group: dict[str, int] = {}
    by_domain: dict[str, int] = {}
    mutates_true = 0
    mutates_false = 0
    correlation_summary = {key: 0 for key in _CORRELATION_SUMMARY_KEYS}

    for rec in merged:
        group = str(rec.get("analytics_group") or "unknown")
        domain = str(rec.get("event_domain") or "unknown")
        by_group[group] = by_group.get(group, 0) + 1
        by_domain[domain] = by_domain.get(domain, 0) + 1
        if rec.get("mutates_truth"):
            mutates_true += 1
        else:
            mutates_false += 1
        refs = rec.get("correlation_refs") if isinstance(rec.get("correlation_refs"), dict) else {}
        for key in _CORRELATION_SUMMARY_KEYS:
            if key == "source_signal_id":
                if str(refs.get("source_signal_id") or refs.get("signal_id") or "").strip():
                    correlation_summary[key] += 1
            elif str(refs.get(key) or "").strip():
                correlation_summary[key] += 1

    recent = _select_recent_records(merged, limit=max(0, recent_limit))
    out_warnings = list(warnings or [])
    if not merged:
        out_warnings.append("no_analytics_records")
    if skipped:
        out_warnings.append(f"sanitization_skipped_{skipped}")
    if export_skipped_count:
        out_warnings.append(f"export_skipped_{export_skipped_count}")

    payload: dict[str, Any] = {
        "schema_version": QUALITY_READONLY_SCHEMA_VERSION,
        "projection_type": QUALITY_READONLY_PROJECTION_TYPE,
        "generated_at": generated_at or _utc_now_iso(),
        "read_only": True,
        "source_summary": {
            "feedback_record_count": len(feedback_safe),
            "eval_shadow_record_count": len(eval_safe),
            "input_count": len(merged) + skipped + max(0, export_skipped_count),
            "exported_count": len(merged),
            "skipped_count": skipped + max(0, export_skipped_count),
        },
        "by_group": dict(sorted(by_group.items())),
        "by_domain": dict(sorted(by_domain.items())),
        "truth_mutation_summary": {
            "mutates_truth_true_count": mutates_true,
            "mutates_truth_false_count": mutates_false,
        },
        "correlation_summary": correlation_summary,
        "recent_records": recent,
        "warnings": out_warnings,
        "not_proven": list(_DEFAULT_NOT_PROVEN),
    }
    return strip_forbidden_nested(payload)


def _select_recent_records(records: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not records:
        return []

    def _sort_key(rec: dict[str, Any]) -> str:
        return str(rec.get("observed_at") or rec.get("event_id") or "")

    ordered = sorted(records, key=_sort_key, reverse=True)
    return [dict(rec) for rec in ordered[:limit]]


def build_quality_readonly_projection_from_jsonl(
    *,
    feedback_jsonl: Path | None = None,
    eval_jsonl: Path | None = None,
    recent_limit: int = DEFAULT_RECENT_RECORD_LIMIT,
) -> dict[str, Any]:
    feedback_rows = read_jsonl(feedback_jsonl) if feedback_jsonl else []
    eval_rows = read_jsonl(eval_jsonl) if eval_jsonl else []
    feedback_list = [r for r in feedback_rows if isinstance(r, dict)]
    eval_list = [r for r in eval_rows if isinstance(r, dict)]
    return build_quality_readonly_projection(feedback_list, eval_list, recent_limit=recent_limit)


def validate_quality_readonly_slice(obj: Any) -> list[str]:
    """Structural validation for optional ``feed.quality_readonly`` (projection-only)."""
    errs: list[str] = []
    if not isinstance(obj, dict):
        return ["quality_readonly must be an object"]
    if obj.get("read_only") is not True:
        errs.append("quality_readonly.read_only must be true")
    if str(obj.get("projection_type") or "") != QUALITY_READONLY_PROJECTION_TYPE:
        errs.append("quality_readonly.projection_type must be quality_readonly")
    if str(obj.get("schema_version") or "") != QUALITY_READONLY_SCHEMA_VERSION:
        errs.append(f"quality_readonly.schema_version must be {QUALITY_READONLY_SCHEMA_VERSION}")
    if _contains_forbidden_quality_content(obj):
        errs.append("quality_readonly contains forbidden raw keys")
    return errs


def prepare_quality_readonly_for_feed(quality_projection: dict[str, Any]) -> dict[str, Any]:
    """Sanitize and validate before attaching to operational feed."""
    if not isinstance(quality_projection, dict):
        raise ValueError("quality_projection must be a dict")
    errs = validate_quality_readonly_slice(quality_projection)
    if errs:
        raise ValueError(f"invalid quality_readonly slice: {errs}")
    return strip_forbidden_nested(dict(quality_projection))


def attach_quality_slice_to_operational_feed(
    operational_feed_snapshot: dict[str, Any],
    quality_projection: dict[str, Any],
) -> dict[str, Any]:
    """
    Pure merge helper for future Daszek feed consumers — does not POST or persist.
    Returns a shallow copy of the feed snapshot with ``feed.quality_readonly`` set.
    """
    if not isinstance(operational_feed_snapshot, dict):
        raise ValueError("operational_feed_snapshot must be a dict")
    merged = dict(operational_feed_snapshot)
    feed = dict(merged.get("feed") or {}) if isinstance(merged.get("feed"), dict) else {}
    feed["quality_readonly"] = prepare_quality_readonly_for_feed(quality_projection)
    merged["feed"] = feed
    return strip_forbidden_nested(merged)


def write_quality_readonly_projection_json(path: Path, payload: dict[str, Any]) -> None:
    write_json(path, strip_forbidden_nested(payload))


def run_quality_projection_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build read-only quality projection JSON from feedback/eval analytics JSONL.",
    )
    parser.add_argument("--feedback-jsonl", type=Path, help="Sanitized feedback analytics JSONL.")
    parser.add_argument("--eval-jsonl", type=Path, help="Sanitized eval shadow analytics JSONL.")
    parser.add_argument("--output-json", type=Path, required=True, help="Write projection snapshot JSON.")
    parser.add_argument("--recent-limit", type=int, default=DEFAULT_RECENT_RECORD_LIMIT)
    args = parser.parse_args(argv)

    if not args.feedback_jsonl and not args.eval_jsonl:
        parser.error("Provide --feedback-jsonl and/or --eval-jsonl")

    payload = build_quality_readonly_projection_from_jsonl(
        feedback_jsonl=args.feedback_jsonl,
        eval_jsonl=args.eval_jsonl,
        recent_limit=args.recent_limit,
    )
    write_quality_readonly_projection_json(args.output_json, payload)
    print(json.dumps(payload.get("source_summary") or {}, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    return run_quality_projection_cli()


if __name__ == "__main__":
    raise SystemExit(main())
