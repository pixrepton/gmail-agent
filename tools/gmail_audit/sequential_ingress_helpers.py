"""Small helpers for scripts/sequential_gmail_ingress_daszek.py (testable without subprocess)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from groq_client import is_rate_limit_error_message

_FAILED_FINAL_STATUSES = frozenset({"failed", "rate_limit_exhausted"})
_SUCCESS_FINAL_STATUSES = frozenset({"completed", "completed_with_errors"})

# Shipped inside sequential_summary.json for operators reading JSON only.
GROQ_429_DETECTED_COUNT_NOTE = (
    "Counts rate-limit/throttle detections across attempts, including intermediate retry attempts "
    "that later succeed. It is not equal to failed message count."
)


def bounded_text_tail(text: str | None, *, max_chars: int = 2000) -> str:
    """Last *max_chars* characters of stderr/stdout for bounded diagnostics (no mail bodies)."""
    s = text or ""
    if len(s) <= max_chars:
        return s
    return s[-max_chars:]


def parse_child_runs_index_final_rows(batch_dir: Path) -> dict[str, dict[str, Any]]:
    """Last *final* row per ``message_id`` in ``child_runs_index.jsonl``."""
    path = batch_dir / "child_runs_index.jsonl"
    if not path.is_file():
        return {}
    last_final: dict[str, dict[str, Any]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        mid = str(row.get("message_id") or "").strip()
        if not mid or not row.get("final"):
            continue
        last_final[mid] = row
    return last_final


def load_failed_items_records(batch_dir: Path) -> list[dict[str, Any]]:
    """Load ``failed_items.jsonl`` for resume (dedupe by message_id, keep last)."""
    path = batch_dir / "failed_items.jsonl"
    if not path.is_file():
        return []
    by_mid: dict[str, dict[str, Any]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            mid = str(row.get("message_id") or "").strip()
            if mid:
                by_mid[mid] = row
    return list(by_mid.values())


def make_failed_item_record(
    *,
    message_id: str,
    returncode: int | None,
    stdout_parse_ok: bool,
    stderr_tail: str,
    stdout_tail: str,
    rate_limit_hint: bool,
    run_dir: str,
    projection_proof_path: str,
    final_status: str,
    terminal_attempt: int,
) -> dict[str, Any]:
    """Single bounded failure record for ``failed_items.jsonl`` (no PII / mail content)."""
    return {
        "message_id": message_id,
        "terminal_attempt": int(terminal_attempt),
        "returncode": returncode,
        "stdout_parse_ok": bool(stdout_parse_ok),
        "stderr_tail": bounded_text_tail(stderr_tail, max_chars=2000),
        "stdout_tail": bounded_text_tail(stdout_tail, max_chars=1200),
        "rate_limit_hint": bool(rate_limit_hint),
        "run_dir": str(run_dir or "")[:2000],
        "projection_proof_path": str(projection_proof_path or "")[:2000],
        "final_status": str(final_status),
    }


def projection_batch_partial_breakdown(
    *,
    final_rows_by_mid: dict[str, dict[str, Any]],
    proof_items: list[dict[str, Any]],
    projection_proof_enabled: bool,
) -> dict[str, Any]:
    """Counts for partial batch projection proof (no fake 'accepted' where proof is missing)."""
    child_summary_missing = sum(
        1 for row in final_rows_by_mid.values() if not row.get("parsed_summary_present")
    )
    child_failed = sum(1 for row in final_rows_by_mid.values() if str(row.get("final_status") or "") in _FAILED_FINAL_STATUSES)
    success_final = sum(
        1 for row in final_rows_by_mid.values() if str(row.get("final_status") or "") in _SUCCESS_FINAL_STATUSES
    )
    n_proof = len(proof_items)
    projection_proof_available = n_proof
    projection_proof_missing = max(0, success_final - n_proof) if projection_proof_enabled else 0

    if not projection_proof_enabled:
        projection_status = "disabled"
    elif not final_rows_by_mid:
        projection_status = "not_started"
    elif child_summary_missing > 0 or child_failed > 0 or (projection_proof_enabled and success_final > 0 and n_proof < success_final):
        projection_status = "partial"
    elif n_proof == 0 and success_final == 0:
        projection_status = "missing"
    else:
        projection_status = "full"

    return {
        "projection_proof_row_count": n_proof,
        "projection_proof_available": projection_proof_available,
        "projection_proof_missing": projection_proof_missing,
        "child_summary_missing": child_summary_missing,
        "child_failed": child_failed,
        "projection_status": projection_status,
    }


def build_sequential_operator_summary(
    *,
    rollup_core: dict[str, Any],
    batch_dir: Path,
    requested_message_ids: list[str],
    dry_run: bool,
    started_at: str,
    finished_at: str,
    groq_429_detected_count: int,
    projection_proof_enabled: bool,
    proof_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge rollup metrics with operator-pass readiness metadata for ``sequential_summary.json``."""
    final_rows = parse_child_runs_index_final_rows(batch_dir)
    partial = projection_batch_partial_breakdown(
        final_rows_by_mid=final_rows,
        proof_items=proof_items,
        projection_proof_enabled=projection_proof_enabled,
    )

    attempted = len(final_rows)
    succeeded = sum(1 for row in final_rows.values() if str(row.get("final_status") or "") in _SUCCESS_FINAL_STATUSES)
    failed = sum(1 for row in final_rows.values() if str(row.get("final_status") or "") in _FAILED_FINAL_STATUSES)
    missing_summary = sum(1 for row in final_rows.values() if not row.get("parsed_summary_present"))

    if dry_run:
        overall_status = "dry_run"
    elif attempted > 0 and succeeded == 0 and failed == attempted:
        overall_status = "failed"
    elif failed > 0 or missing_summary > 0:
        overall_status = "completed_with_failures"
    else:
        overall_status = "completed"

    checkpoint = batch_dir / "child_runs_index.jsonl"
    child_summaries_path = batch_dir / "child_summaries.jsonl"
    failed_items_path = batch_dir / "failed_items.jsonl"
    batch_projection_path = batch_dir / "projection_proof_report.batch.json"

    out: dict[str, Any] = dict(rollup_core)
    out.update(
        {
            "status": overall_status,
            "requested_count": len(requested_message_ids),
            "attempted_count": attempted,
            "succeeded_count": succeeded,
            "failed_count": failed,
            "missing_summary_count": missing_summary,
            "projection_report_count": int(rollup_core.get("proof_items_count") or 0),
            "groq_429_detected_count": int(groq_429_detected_count),
            "groq_429_detected_count_note": GROQ_429_DETECTED_COUNT_NOTE,
            "resume_checkpoint_path": str(checkpoint.resolve()) if checkpoint.is_file() else str(checkpoint),
            "child_summaries_path": str(child_summaries_path.resolve()) if child_summaries_path.is_file() else str(child_summaries_path),
            "failed_items_path": str(failed_items_path.resolve()) if failed_items_path.is_file() else str(failed_items_path),
            "batch_projection_report_path": str(batch_projection_path.resolve())
            if batch_projection_path.is_file()
            else str(batch_projection_path),
            "started_at": started_at,
            "finished_at": finished_at,
            "projection_breakdown": partial,
        }
    )
    return out


def parse_newer_than_days(value: str | None) -> int | None:
    """Parse Gmail-style ``newer_than:14d`` segment or bare ``14d`` / ``14`` into days.

    Returns ``None`` if *value* is empty or cannot be parsed.
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    m = re.match(r"^(?:newer_than:)?(\d+)\s*d$", s)
    if m:
        return max(1, int(m.group(1)))
    if s.isdigit():
        return max(1, int(s))
    return None


def build_gmail_intake_message_command(
    *,
    python_executable: str,
    intake_py: str,
    message_id: str,
    gmail_source: str,
    push_daszek: bool,
    projection_proof: bool,
    keep_going: bool,
    verbose: bool,
) -> list[str]:
    _ = gmail_source  # signal-active ingress; source is resolved inside signal-run.
    cmd: list[str] = [
        python_executable,
        intake_py,
        "signal-run",
        "--oneshot",
        "--message-id",
        message_id,
        "--max-messages",
        "1",
    ]
    if push_daszek:
        cmd.append("--push-daszek")
    if projection_proof:
        cmd.append("--projection-proof")
    if keep_going:
        cmd.append("--keep-going")
    if verbose:
        cmd.append("--verbose")
    return cmd


def parse_first_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort: extract first JSON object from text (e.g. gmail_intake stdout)."""
    decoder = json.JSONDecoder()
    cursor = 0
    while cursor < len(text):
        start = text.find("{", cursor)
        if start < 0:
            return None
        try:
            obj, _end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        return obj if isinstance(obj, dict) else None
    return None


_RUN_DIR_LINE = re.compile(r"^\[info\]\s*Run directory:\s*(.+?)\s*$", re.MULTILINE)


def extract_run_dir_from_text(
    *,
    stdout: str,
    stderr: str,
    runs_root: Path,
    parsed_summary: dict[str, Any] | None,
) -> Path | None:
    """Resolve child run directory from stdout JSON and/or stderr ``[info] Run directory:``."""
    summary = parsed_summary
    if summary is None:
        summary = parse_first_json_object(stdout or "")

    if isinstance(summary, dict):
        rid = str(summary.get("run_id") or "").strip()
        if rid:
            p = runs_root / rid
            if p.is_dir():
                return p
        rd = str(summary.get("run_dir") or "").strip()
        if rd:
            p = Path(rd)
            if p.is_dir():
                return p.resolve()

    m = _RUN_DIR_LINE.search(stderr or "")
    if m:
        p = Path(m.group(1).strip())
        if p.is_dir():
            return p.resolve()

    return None


def is_rate_limit_signal(
    *,
    returncode: int,
    stdout: str,
    stderr: str,
    parsed_summary: dict[str, Any] | None,
) -> bool:
    """True when failure looks like Groq/API throttle (retry candidate)."""
    blob = f"{stderr or ''}\n{stdout or ''}"
    if is_rate_limit_error_message(blob):
        return True
    if isinstance(parsed_summary, dict):
        by_cat = parsed_summary.get("errors_by_category")
        if isinstance(by_cat, dict):
            try:
                if int(by_cat.get("throttle") or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def compute_retry_delay(*, attempt: int, base: float, cap: float) -> float:
    """Exponential backoff before retry attempt ``attempt`` (1-based after a failed try)."""
    if attempt < 1:
        return 0.0
    delay = float(base) * (2.0 ** float(attempt - 1))
    return min(float(cap), delay)


def make_child_runs_index_row(
    *,
    message_id: str,
    attempt: int,
    returncode: int | None,
    run_id: str,
    run_dir: str,
    parsed_summary_present: bool,
    rate_limited: bool,
    final: bool,
    final_status: str,
) -> dict[str, Any]:
    """Stable row shape for ``child_runs_index.jsonl``."""
    return {
        "message_id": message_id,
        "attempt": int(attempt),
        "returncode": returncode,
        "run_id": run_id,
        "run_dir": run_dir,
        "parsed_summary_present": bool(parsed_summary_present),
        "rate_limited": bool(rate_limited),
        "final": bool(final),
        "final_status": str(final_status),
    }


def load_completed_message_ids(batch_dir: Path, *, force: bool = False) -> set[str]:
    """Return message ids that finished successfully (for resume skip).

    When *force* is True, return empty set so all requested ids are processed again.
    """
    if force:
        return set()
    path = batch_dir / "child_runs_index.jsonl"
    if not path.is_file():
        return set()
    last_final: dict[str, dict[str, Any]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        mid = str(row.get("message_id") or "").strip()
        if not mid or not row.get("final"):
            continue
        last_final[mid] = row

    done: set[str] = set()
    for mid, row in last_final.items():
        st = str(row.get("final_status") or "")
        if st in {"completed", "completed_with_errors"}:
            done.add(mid)
    return done


def aggregate_projection_batch_summary(
    *,
    child_summaries: list[dict[str, Any]],
    proof_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Roll up per-message ``summary.json`` + merged projection proof ``items`` for operator batch report."""
    n = len(child_summaries)
    processed_ok = sum(1 for s in child_summaries if str(s.get("status") or "") == "completed")
    processed_failed = sum(
        1 for s in child_summaries if str(s.get("status") or "") in {"failed", "failed_auth", "failed_preflight"}
    )
    processed_with_errors = sum(1 for s in child_summaries if str(s.get("status") or "") == "completed_with_errors")

    model_errors = 0
    groq_429 = 0
    for s in child_summaries:
        by_cat = s.get("errors_by_category") if isinstance(s.get("errors_by_category"), dict) else {}
        model_errors += int(by_cat.get("parse", 0) or 0) + int(by_cat.get("schema", 0) or 0) + int(by_cat.get("semantic", 0) or 0)
        groq_429 += int(by_cat.get("throttle", 0) or 0)

    def _count_status(key: str, value: str) -> int:
        return sum(1 for row in proof_items if isinstance(row, dict) and str(row.get(key) or "") == value)

    v2_accepted = sum(
        1
        for row in proof_items
        if isinstance(row, dict)
        and str(row.get("policy_status") or "") == "accepted_projection"
        and str(row.get("surface") or "") == "v2_ingest"
    )
    v2_blocked = _count_status("policy_status", "blocked_policy")
    v2_skipped = _count_status("policy_status", "skipped_config_disabled")
    v2_failed = _count_status("policy_status", "projection_failed")

    v3_accepted = sum(
        1
        for row in proof_items
        if isinstance(row, dict)
        and str(row.get("policy_status") or "") == "accepted_projection"
        and str(row.get("surface") or "") == "v3_operational_feed"
    )
    v3_failed = sum(
        1
        for row in proof_items
        if isinstance(row, dict)
        and str(row.get("policy_status") or "") == "projection_failed"
        and str(row.get("surface") or "") == "v3_operational_feed"
    )
    feed_handoff_actionable = sum(
        1 for row in proof_items if isinstance(row, dict) and bool(row.get("feed_handoff_actionable"))
    )

    rb_found = sum(
        1
        for row in proof_items
        if isinstance(row, dict) and str(row.get("store_readback") or "") == "found"
    )
    rb_missing = sum(
        1
        for row in proof_items
        if isinstance(row, dict)
        and str(row.get("policy_status") or "") == "accepted_projection"
        and str(row.get("store_readback") or "") != "found"
    )

    ui_expected = sum(1 for row in proof_items if isinstance(row, dict) and bool(row.get("ui_visibility_expected")))

    manual = sum(
        1
        for row in proof_items
        if isinstance(row, dict)
        and str(row.get("policy_status") or "") in {"blocked_policy", "projection_failed", "unknown"}
    )
    manual += processed_failed + processed_with_errors

    return {
        "processed_total": n,
        "processed_ok": processed_ok,
        "processed_failed": processed_failed,
        "processed_completed_with_errors": processed_with_errors,
        "model_errors": model_errors,
        "groq_429_retries": groq_429,
        "v1_task_count_note": "Use per-run doctor/preflight or latest doctor JSON; not repeated per child here.",
        "v2_projection_accepted": v2_accepted,
        "v2_projection_blocked_policy": v2_blocked,
        "v2_projection_skipped": v2_skipped,
        "v2_projection_failed": v2_failed,
        "v2_readback_found": rb_found,
        "v2_readback_missing": rb_missing,
        "v3_feed_push_ok": v3_accepted,
        "v3_feed_push_failed": v3_failed,
        "feed_handoff_actionable": feed_handoff_actionable,
        "ui_visibility_expected": ui_expected,
        "manual_intervention_required": manual,
    }


__all__ = [
    "aggregate_projection_batch_summary",
    "bounded_text_tail",
    "build_gmail_intake_message_command",
    "build_sequential_operator_summary",
    "compute_retry_delay",
    "extract_run_dir_from_text",
    "GROQ_429_DETECTED_COUNT_NOTE",
    "is_rate_limit_signal",
    "load_completed_message_ids",
    "load_failed_items_records",
    "make_child_runs_index_row",
    "make_failed_item_record",
    "parse_child_runs_index_final_rows",
    "parse_first_json_object",
    "parse_newer_than_days",
    "projection_batch_partial_breakdown",
]
