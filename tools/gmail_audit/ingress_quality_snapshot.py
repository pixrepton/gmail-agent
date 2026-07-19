"""
Build a sanitized Daszek-facing ingress quality snapshot from bounded-run artifacts.

Reads only:
  - latest20_operator_review.json (primary)
  - latest20_run_report.md (rate limits / truncation / infra prose)
  - optionally latest20_operator_review.md (report_refs only)

Does not read summary.json (may embed preflight secrets). Does not call Gmail/LLM.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_NAME = "daszek_ingress_quality_snapshot"
SCHEMA_VERSION = "1"

DECISION_KEYS = (
    "ignore",
    "create_case",
    "append_to_existing_case",
    "review",
    "update_case_state",
    "create_task",
    "mark_reference",
)

FORBIDDEN_ITEM_KEYS = frozenset(
    {
        "body",
        "email_body",
        "snippet",
        "subject",
        "raw_llm",
        "raw_response",
        "prompt",
        "prompt_text",
        "attachment_bytes",
        "intake_result_raw",
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_latest20_run_report(md_text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not md_text or not md_text.strip():
        return out

    m = re.search(r"stderr http-429 retry lines:\s*(\d+)", md_text)
    if m:
        out["rate_limit_events"] = int(m.group(1))
    m = re.search(r"http-429`\s*retry events", md_text)
    if m and "rate_limit_events" not in out:
        m2 = re.search(r"(\d+)\s+`http-429`", md_text)
        if m2:
            out["rate_limit_events"] = int(m2.group(1))

    m = re.search(r"\[truncated\].*?:\s*(\d+)", md_text)
    if m:
        out["truncation_count"] = int(m.group(1))

    m = re.search(r"live Gmail used:\s*(\w+)", md_text, re.I)
    if m:
        v = m.group(1).strip().lower()
        out["live_gmail_used"] = v in ("yes", "true", "1")

    m = re.search(r"mailbox memory mutated:\s*(\w+)", md_text, re.I)
    if m:
        v = m.group(1).strip().lower()
        out["mailbox_memory_mutated"] = v.startswith("y") or v == "true"

    m = re.search(r"persisted push count was\s*(\d+)", md_text, re.I)
    if not m:
        m = re.search(r"projected_count:\s*(\d+)\s+persisted", md_text, re.I)
    if m:
        out["daszek_persisted_push_count"] = int(m.group(1))

    m = re.search(r"local projection preview records:\s*(\d+)", md_text, re.I)
    if m:
        out["local_projection_preview_count"] = int(m.group(1))

    m = re.search(r"outbound actions:\s*(\w+)", md_text, re.I)
    if m:
        v = m.group(1).strip().lower()
        out["outbound_actions"] = not (v == "none" or v == "no")

    fail_block = re.search(
        r"Message id:\s*`([^`]+)`.*?Non-sensitive reason:\s*([^\n]+)",
        md_text,
        re.S | re.I,
    )
    if fail_block:
        out["_report_failed_message_id"] = fail_block.group(1).strip()
        out["_report_failed_reason"] = fail_block.group(2).strip()

    return out


def _safe_run_manifest_times(run_dir: Path) -> dict[str, str]:
    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    out = {}
    for key in ("started_at", "completed_at"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            out[f"source_run_{key}"] = val.strip()
    return out


def _validation_errors_for_message(run_dir: Path, message_id: str) -> list[str]:
    path = run_dir / "validation_results.jsonl"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping):
            continue
        if row.get("message_id") != message_id:
            continue
        errs = row.get("errors")
        if isinstance(errs, list):
            return [str(e) for e in errs if isinstance(e, str) and e.strip()]
    return []


def _sanitize_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        bad = FORBIDDEN_ITEM_KEYS.intersection({k.lower() for k in raw})
        if bad:
            raise ValueError(f"operator review item contains forbidden keys: {sorted(bad)}")
        out.append(
            {
                "index": int(raw.get("index", 0)),
                "message_id": str(raw.get("message_id", "")).strip(),
                "decision": str(raw.get("decision", "")).strip(),
                "case_id": str(raw.get("case_id", "")).strip(),
                "status": str(raw.get("status", "")).strip(),
                "truncated": bool(raw.get("truncated_compact_input")),
                "operator_question": str(raw.get("operator_prompt", "")).strip(),
                "policy_status": str(raw.get("policy_status", "")).strip() or None,
            }
        )
    return out


def build_ingress_quality_snapshot(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    review_path = run_dir / "latest20_operator_review.json"
    report_md_path = run_dir / "latest20_run_report.md"
    review_md_path = run_dir / "latest20_operator_review.md"

    if not review_path.is_file():
        raise FileNotFoundError(f"missing {review_path}")

    operator = json.loads(review_path.read_text(encoding="utf-8"))
    if not isinstance(operator, Mapping):
        raise ValueError("latest20_operator_review.json must be an object")

    run_id = str(operator.get("run_id", "")).strip()
    if not run_id:
        raise ValueError("run_id missing in operator review")

    report_md = report_md_path.read_text(encoding="utf-8", errors="replace") if report_md_path.is_file() else ""
    parsed_report = parse_latest20_run_report(report_md)
    manifest_times = _safe_run_manifest_times(run_dir)

    sc = operator.get("summary_counts")
    if not isinstance(sc, Mapping):
        sc = {}

    dist_in = sc.get("decision_distribution")
    if not isinstance(dist_in, Mapping):
        dist_in = {}
    decision_distribution = {k: int(dist_in.get(k, 0) or 0) for k in DECISION_KEYS}

    items = _sanitize_items(operator.get("items"))
    failed_ids = [str(x).strip() for x in (operator.get("failed_message_ids") or []) if str(x).strip()]
    review_ids = [str(x).strip() for x in (operator.get("review_decisions_message_ids") or []) if str(x).strip()]

    manual_review_items: list[dict[str, Any]] = []
    for row in items:
        mid = row["message_id"]
        if row.get("status") in ("needs_review", "failed") or mid in review_ids:
            manual_review_items.append(
                {
                    "message_id": mid,
                    "decision": row.get("decision", ""),
                    "case_id": row.get("case_id") or "",
                    "status": row.get("status", ""),
                    "operator_question": row.get("operator_question", ""),
                    "truncated": row.get("truncated", False),
                }
            )

    failed_items: list[dict[str, Any]] = []
    for mid in failed_ids:
        reason = parsed_report.get("_report_failed_reason", "")
        if mid == parsed_report.get("_report_failed_message_id") and reason:
            pass
        else:
            verrs = _validation_errors_for_message(run_dir, mid)
            reason = verrs[0] if verrs else "validation_failed"
        failed_items.append(
            {
                "message_id": mid,
                "status": "failed",
                "non_sensitive_reason": reason,
                "recommended_operator_action": "Uruchom targeted rerun pojedynczej wiadomości po cooldown albo inspekcję rekordu walidacji.",
            }
        )

    selected = int(sc.get("items_selected", 0) or 0)
    processed = int(sc.get("items_processed", 0) or 0)
    valid_c = int(sc.get("items_valid", 0) or 0)
    failed_c = int(sc.get("items_failed", 0) or 0)

    skipped = 0
    needs_retry = 0
    projected = int(parsed_report.get("local_projection_preview_count", 0) or 0)
    m_sk = re.search(r"skipped_count:\s*(\d+)", report_md)
    if m_sk:
        skipped = int(m_sk.group(1))
    m_nr = re.search(r"needs_retry_count:\s*(\d+)", report_md)
    if m_nr:
        needs_retry = int(m_nr.group(1))

    m_proj = re.search(r"projected_count:\s*(\d+)", report_md)
    projected_persisted = int(m_proj.group(1)) if m_proj else 0

    llm_block = {
        "rate_limit_events": int(parsed_report.get("rate_limit_events", 0) or 0),
        "truncation_count": int(parsed_report.get("truncation_count", 0) or 0),
        "invalid_json": 0,
        "schema_invalid": 0,
        "semantic_invalid": 0,
        "repaired_valid_count": None,
    }

    snapshot: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "snapshot_type": "latest_gmail_ingress",
        "run_id": run_id,
        "created_at": _utc_now_iso(),
        "source_run_started_at": manifest_times.get("source_run_started_at"),
        "source_run_completed_at": manifest_times.get("source_run_completed_at"),
        "source_run_dir_reference": f"tools/gmail_audit/runs/{run_id}",
        "title": "Ostatni ingress",
        "subtitle": "Podgląd jakości ostatniego bounded ingress (read-only).",
        "operator_label": "Podgląd jakości ingressu — nie tworzy spraw i nie wykonuje akcji",
        "read_only": True,
        "creates_cases": False,
        "executes_actions": False,
        "gate_claim": False,
        "live_gmail_used": bool(parsed_report.get("live_gmail_used", True)),
        "mailbox_memory_mutated": bool(parsed_report.get("mailbox_memory_mutated", True)),
        "daszek_persisted_push_count": int(parsed_report.get("daszek_persisted_push_count", 0) or 0),
        "local_projection_preview_count": int(parsed_report.get("local_projection_preview_count", projected) or 0),
        "outbound_actions": bool(parsed_report.get("outbound_actions", False)),
        "counts": {
            "selected_count": selected,
            "processed_count": processed,
            "valid_count": valid_c,
            "failed_count": failed_c,
            "skipped_count": skipped,
            "needs_retry_count": needs_retry,
            "projected_count": projected_persisted,
        },
        "decision_distribution": decision_distribution,
        "llm": llm_block,
        "failed_items": failed_items,
        "manual_review_items": manual_review_items,
        "items": items,
        "report_refs": {
            "latest20_run_report.md": "latest20_run_report.md",
            "latest20_operator_review.md": "latest20_operator_review.md"
            if review_md_path.is_file()
            else None,
            "latest20_operator_review.json": "latest20_operator_review.json",
        },
    }

    m_ivj = re.search(r"invalid_json:\s*(\d+)", report_md)
    if m_ivj:
        snapshot["llm"]["invalid_json"] = int(m_ivj.group(1))
    m_is = re.search(r"schema_invalid:\s*(\d+)", report_md)
    if m_is:
        snapshot["llm"]["schema_invalid"] = int(m_is.group(1))
    m_sem = re.search(r"semantic_invalid:\s*(\d+)", report_md)
    if m_sem:
        snapshot["llm"]["semantic_invalid"] = int(m_sem.group(1))

    m_rep = re.search(r"items_repaired_valid:\s*(\d+)", report_md)
    if not m_rep:
        m_rep = re.search(r"repaired_valid:\s*(\d+)", report_md)
    if m_rep:
        snapshot["llm"]["repaired_valid_count"] = int(m_rep.group(1))

    # Drop private parser keys
    parsed_report.pop("_report_failed_message_id", None)
    parsed_report.pop("_report_failed_reason", None)

    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Daszek ingress quality snapshot JSON.")
    parser.add_argument("--run-dir", type=Path, required=True, help="Path to tools/gmail_audit/runs/<run_id>")
    parser.add_argument("--out", type=Path, help="Output JSON path (default: <run-dir>/ingress_quality_snapshot.json)")
    args = parser.parse_args()
    run_dir: Path = args.run_dir
    out_path = args.out or (run_dir / "ingress_quality_snapshot.json")
    snap = build_ingress_quality_snapshot(run_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(str(out_path.resolve()))


if __name__ == "__main__":
    main()
