"""No-side-effect real-mail intelligence discovery harness.

This module does not fetch Gmail, call LLMs, push Daszek, or execute tools.
It consumes operator-curated historical case records and produces a compact
gap map for the next intelligence workstream.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from artifact_io import read_jsonl, write_json, write_jsonl, write_text
from redaction import sanitize_for_storage, sanitize_text


SCHEMA_VERSION = "real_mail_intelligence_discovery.v1"
DEFAULT_MIN_CASES = 10
DEFAULT_MAX_CASES = 15
GAP_CATEGORIES = (
    "RAG_GAP",
    "FACT_GAP",
    "ATTACHMENT_UNDERSTANDING_GAP",
    "CASE_HISTORY_GAP",
    "BUSINESS_REASONING_GAP",
    "POLICY_GAP",
    "TOOL_GAP",
    "IDENTITY_GAP",
    "CALCULATION_GAP",
    "WORKFLOW_GAP",
    "PROMPT_LLM_REASONING_GAP",
)
NO_GAP = "NO_GAP"


@dataclass(frozen=True, slots=True)
class RealMailDiscoveryOptions:
    input_path: Path
    output_dir: Path
    run_id: str = ""
    min_cases: int = DEFAULT_MIN_CASES
    max_cases: int = DEFAULT_MAX_CASES
    allow_small_sample: bool = False


def run_real_mail_intelligence_discovery(options: RealMailDiscoveryOptions) -> dict[str, Any]:
    """Build a no-side-effect gap map for curated historical real-mail cases."""

    cases = load_discovery_cases(options.input_path)
    run_id = options.run_id.strip() or _make_run_id()
    started_at = datetime.now().astimezone().isoformat()
    items = [analyze_discovery_case(case, index=index + 1) for index, case in enumerate(cases)]
    missing_label_count = sum(1 for item in items if item["status"] == "needs_operator_label")
    gap_counts = Counter(category for item in items for category in item["gap_categories"] if category != NO_GAP)
    status = _run_status(
        case_count=len(items),
        missing_label_count=missing_label_count,
        options=options,
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": status,
        "qualification": _qualification(status),
        "started_at": started_at,
        "completed_at": datetime.now().astimezone().isoformat(),
        "input": {
            "path": str(options.input_path),
            "case_count": len(items),
            "min_cases": int(options.min_cases),
            "max_cases": int(options.max_cases),
            "allow_small_sample": bool(options.allow_small_sample),
        },
        "safety": {
            "input_mode": "file_only",
            "live_gmail_fetch": False,
            "llm_calls": 0,
            "outbound_actions": "disabled",
            "daszek_push": False,
            "tool_execution": False,
            "raw_mail_body_written": False,
        },
        "counts": {
            "case_count": len(items),
            "needs_operator_label": missing_label_count,
            "no_gap_detected": sum(1 for item in items if item["gap_categories"] == [NO_GAP]),
            "actionable_gap_cases": sum(1 for item in items if item["status"] == "actionable_gap"),
        },
        "gap_counts": dict(sorted(gap_counts.items())),
        "items": items,
        "next_actions": _next_actions(status, gap_counts, missing_label_count),
    }
    return sanitize_for_storage(summary)


def write_real_mail_discovery_proof(summary: dict[str, Any], *, output_dir: Path) -> dict[str, str]:
    """Write stable proof artifacts without raw message bodies."""

    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "summary.json", summary)
    write_jsonl(output_dir / "case-gap-map.jsonl", list(summary.get("items") or []))
    write_text(output_dir / "README.md", render_markdown_summary(summary))
    return {
        "summary": str(output_dir / "summary.json"),
        "case_gap_map": str(output_dir / "case-gap-map.jsonl"),
        "readme": str(output_dir / "README.md"),
    }


def load_discovery_cases(path: Path) -> list[dict[str, Any]]:
    """Load discovery cases from JSON array/object or JSONL."""

    if not path.is_file():
        raise OSError(f"real-mail discovery input not found: {path}")
    if path.suffix.lower() == ".jsonl":
        rows = read_jsonl(path)
        return [_require_object(row, path=path, index=index + 1) for index, row in enumerate(rows)]
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        rows = payload.get("cases")
        if rows is None:
            rows = payload.get("items")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise OSError(f"Expected JSON array or object with cases/items in {path}")
    return [_require_object(row, path=path, index=index + 1) for index, row in enumerate(rows)]


def analyze_discovery_case(case: dict[str, Any], *, index: int) -> dict[str, Any]:
    """Classify one curated case into capability gap categories."""

    expected = _dict(case.get("expected") or case.get("operator_expected") or {})
    actual = _dict(case.get("actual") or case.get("agent_actual") or case.get("agent_output") or {})
    evidence = _dict(case.get("evidence") or {})
    categories: set[str] = set()
    categories.update(_explicit_gap_categories(case, expected, actual))

    expected_action = _first_text(expected, "best_action", "action", "next_best_action")
    actual_action = _first_text(actual, "best_action", "action", "next_best_action", "recommended_next_action")
    if expected_action and actual_action and _norm(expected_action) != _norm(actual_action):
        categories.add("BUSINESS_REASONING_GAP")

    required_facts = _text_set(expected.get("required_facts") or expected.get("decision_required_facts"))
    available_facts = _text_set(actual.get("available_facts") or actual.get("used_facts") or evidence.get("available_facts"))
    blocked_facts = _text_set(actual.get("blocked_facts") or actual.get("unusable_facts") or evidence.get("blocked_facts"))
    missing_facts = sorted(required_facts - available_facts)
    unusable_required = sorted(required_facts.intersection(blocked_facts))
    if missing_facts or unusable_required:
        categories.add("FACT_GAP")

    required_capabilities = _text_set(expected.get("required_capabilities") or expected.get("decision_required_capabilities"))
    qualified_capabilities = _text_set(actual.get("qualified_capabilities") or evidence.get("qualified_capabilities"))
    unqualified_capabilities = sorted(required_capabilities - qualified_capabilities)
    if any("rag" in item or "product" in item or "technical" in item for item in unqualified_capabilities):
        categories.add("RAG_GAP")

    if _boolish(expected.get("requires_attachment_understanding")) and not _boolish(
        actual.get("attachment_understanding_ready") or evidence.get("attachment_understanding_ready")
    ):
        categories.add("ATTACHMENT_UNDERSTANDING_GAP")
    if _boolish(expected.get("requires_case_history")) and not _boolish(
        actual.get("case_history_ready") or evidence.get("case_history_ready")
    ):
        categories.add("CASE_HISTORY_GAP")
    if _boolish(actual.get("policy_block_unexpected")):
        categories.add("POLICY_GAP")
    if _boolish(actual.get("tool_missing")) or _required_tool_missing(expected, actual):
        categories.add("TOOL_GAP")
    if _boolish(actual.get("identity_unresolved")) or _boolish(evidence.get("identity_unresolved")):
        categories.add("IDENTITY_GAP")
    if _boolish(expected.get("requires_calculation")) and not _boolish(actual.get("calculation_ready") or evidence.get("calculation_ready")):
        categories.add("CALCULATION_GAP")
    if _boolish(actual.get("workflow_gap")) or _boolish(evidence.get("workflow_gap")):
        categories.add("WORKFLOW_GAP")
    if _boolish(actual.get("llm_reasoning_gap")) or _boolish(evidence.get("llm_reasoning_gap")):
        categories.add("PROMPT_LLM_REASONING_GAP")

    needs_label = not expected and not categories
    final_categories = sorted(categories) if categories else [NO_GAP]
    return {
        "index": index,
        "case_id": _case_id(case, index),
        "message_ref": _message_ref(case),
        "status": "needs_operator_label" if needs_label else ("no_gap_detected" if final_categories == [NO_GAP] else "actionable_gap"),
        "gap_categories": final_categories,
        "expected_action": expected_action,
        "actual_action": actual_action,
        "missing_required_facts": missing_facts,
        "unusable_required_facts": unusable_required,
        "unqualified_required_capabilities": unqualified_capabilities,
        "notes": sanitize_text(_first_text(case, "reviewer_notes", "notes") or ""),
    }


def render_markdown_summary(summary: dict[str, Any]) -> str:
    gaps = summary.get("gap_counts") or {}
    lines = [
        "# Real Mail Intelligence Discovery",
        "",
        f"- status: {summary.get('status', '')}",
        f"- qualification: {summary.get('qualification', '')}",
        f"- case_count: {(summary.get('counts') or {}).get('case_count', 0)}",
        f"- needs_operator_label: {(summary.get('counts') or {}).get('needs_operator_label', 0)}",
        f"- actionable_gap_cases: {(summary.get('counts') or {}).get('actionable_gap_cases', 0)}",
        "- safety: file_only, no Gmail fetch, no LLM calls, no outbound actions",
        "",
        "## Gap Counts",
    ]
    if gaps:
        lines.extend(f"- {key}: {value}" for key, value in sorted(gaps.items()))
    else:
        lines.append("- none")
    lines.extend(["", "## Next Actions"])
    lines.extend(f"- {item}" for item in summary.get("next_actions") or [])
    return "\n".join(lines) + "\n"


def _run_status(*, case_count: int, missing_label_count: int, options: RealMailDiscoveryOptions) -> str:
    if case_count <= 0:
        return "blocked_no_cases"
    if missing_label_count:
        return "blocked_missing_operator_labels"
    if case_count < int(options.min_cases):
        if options.allow_small_sample:
            return "completed_small_sample"
        return "blocked_insufficient_case_count"
    if not options.allow_small_sample and case_count > int(options.max_cases):
        return "blocked_excess_case_count"
    return "completed"


def _qualification(status: str) -> str:
    if status == "completed":
        return "DISCOVERY_QUALIFIED"
    if status == "completed_small_sample":
        return "SMOKE_ONLY"
    return "DATASET_REQUIRED"


def _next_actions(status: str, gap_counts: Counter[str], missing_label_count: int) -> list[str]:
    if status == "blocked_no_cases":
        return ["Provide 10-15 curated historical real-mail cases as JSON/JSONL input."]
    if status == "blocked_missing_operator_labels":
        return [f"Add operator expected outcome labels for {missing_label_count} case(s)."]
    if status == "blocked_insufficient_case_count":
        return ["Add more historical cases before using this as the program discovery proof."]
    if status == "blocked_excess_case_count":
        return ["Reduce this first discovery cohort to 10-15 representative cases."]
    if status == "completed_small_sample":
        return ["This is a smoke/dev run only; provide 10-15 cases for formal discovery qualification."]
    if not gap_counts:
        return ["Proceed to a larger historical qualification cohort; no dominant gap detected in this sample."]
    top_gap = gap_counts.most_common(1)[0][0]
    return [f"Open the next bounded workstream for dominant gap category: {top_gap}."]


def _explicit_gap_categories(*values: Any) -> set[str]:
    categories: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        raw = value.get("gap_categories") or value.get("failure_clusters") or value.get("root_causes")
        for item in _text_set(raw):
            category = item.upper().replace("-", "_").replace("/", "_")
            if category in GAP_CATEGORIES:
                categories.add(category)
    return categories


def _required_tool_missing(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    required = _text_set(expected.get("required_tools") or expected.get("required_action_tools"))
    if not required:
        return False
    available = _text_set(actual.get("available_tools") or actual.get("effective_tools") or actual.get("offered_tools"))
    return bool(available and required - available)


def _message_ref(case: dict[str, Any]) -> dict[str, Any]:
    message = _dict(case.get("message") or {})
    body = str(message.get("body") or case.get("body") or "")
    subject = sanitize_text(str(message.get("subject") or case.get("subject") or ""))
    sender = sanitize_text(str(message.get("sender") or message.get("from") or case.get("sender") or ""))
    return {
        "message_id": str(message.get("message_id") or case.get("message_id") or ""),
        "thread_id": str(message.get("thread_id") or case.get("thread_id") or ""),
        "subject": _clip(subject),
        "sender_domain": _sender_domain(sender),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest() if body else "",
        "body_chars": len(body),
        "attachment_count": len(message.get("attachments") or message.get("attachment_parts") or case.get("attachments") or []),
        "redacted_summary": _clip(sanitize_text(str(case.get("redacted_summary") or message.get("redacted_summary") or "")), limit=220),
    }


def _case_id(case: dict[str, Any], index: int) -> str:
    for key in ("case_id", "id", "fixture_id"):
        value = str(case.get(key) or "").strip()
        if value:
            return value
    return f"case-{index:03d}"


def _sender_domain(sender: str) -> str:
    if "@" not in sender:
        return ""
    return sender.rsplit("@", 1)[-1].strip(" >").lower()


def _clip(value: str, *, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _first_text(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _text_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace(";", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(part).strip() for part in value]
    else:
        parts = [str(value).strip()]
    return {_norm(part) for part in parts if _norm(part)}


def _norm(value: str) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "tak", "required", "blocked"}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _require_object(value: Any, *, path: Path, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OSError(f"Expected object at {path} item #{index}")
    return value


def _make_run_id() -> str:
    return "real-mail-discovery-" + datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


__all__ = [
    "DEFAULT_MAX_CASES",
    "DEFAULT_MIN_CASES",
    "GAP_CATEGORIES",
    "NO_GAP",
    "RealMailDiscoveryOptions",
    "SCHEMA_VERSION",
    "analyze_discovery_case",
    "load_discovery_cases",
    "render_markdown_summary",
    "run_real_mail_intelligence_discovery",
    "write_real_mail_discovery_proof",
]
