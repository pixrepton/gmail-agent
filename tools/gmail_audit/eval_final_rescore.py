"""Offline final-run rescoring for captured AI-OS eval artifacts.

This module scores already captured RUN-A/RUN-B outputs. It must not call the
system under test or regenerate any stage output.
"""

from __future__ import annotations

import argparse
import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any

from eval_measurement_scoring import canonical_json_sha256, measurement_contract, score_case


NON_CAPABILITY_OUTCOMES = {"CAPACITY", "DELIVERY", "HARNESS"}
PRIMARY_OUTCOMES = {"CLEAN_PASS", "CAPABILITY", "CAPACITY", "DELIVERY", "HARNESS"}


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path: str | Path, payload: Any) -> None:
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def rescore_final_run(
    results: dict[str, Any],
    corpus: dict[str, Any],
    *,
    understanding_judge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cases_by_id = {str(case.get("id")): case for case in corpus.get("cases", [])}
    judge_by_case = _judge_by_case(understanding_judge)
    judge_supplied = isinstance(understanding_judge, dict)
    rows = []
    for case_output in results.get("cases", []):
        case_id = str(case_output.get("id") or case_output.get("case_id") or "")
        corpus_case = cases_by_id.get(case_id)
        if not corpus_case:
            rows.append(_missing_ground_truth_row(case_id, case_output))
            continue
        judge_row = judge_by_case.get(case_id)
        ground_truth = corpus_case.get("ground_truth") if isinstance(corpus_case.get("ground_truth"), dict) else {}
        if judge_supplied and isinstance(ground_truth.get("understanding"), dict) and not judge_row:
            judge_row = {"case_id": case_id, "status": "JUDGE_UNAVAILABLE", "error": "missing_frozen_judge_result"}
        rows.append(score_final_case(case_output, corpus_case, understanding_judge=judge_row))

    summary = _summary(rows)
    return {
        "source_mode": results.get("mode"),
        "sentinel_only": bool(results.get("sentinel_only")),
        "corpus_canonical_sha256": canonical_json_sha256(corpus),
        "summary": summary,
        "cases": rows,
    }


def score_final_case(
    case_output: dict[str, Any],
    corpus_case: dict[str, Any],
    *,
    understanding_judge: dict[str, Any] | None = None,
    measurement_contract_version: str = "v1",
) -> dict[str, Any]:
    case_id = str(case_output.get("id") or case_output.get("case_id") or corpus_case.get("id") or "")
    ground_truth = corpus_case.get("ground_truth") or {}
    sanitized, nonblocking_tool_errors = _case_for_quality_scoring(case_output)
    judge_payload = {"understanding": understanding_judge} if understanding_judge else None
    with measurement_contract(measurement_contract_version):
        score = score_case(sanitized, ground_truth, llm_judge=judge_payload)
    component_status = _component_statuses(case_output, ground_truth, score)
    final_outcome = _final_outcome(score, component_status)
    return {
        "case_id": case_id,
        "stage_reached": case_output.get("stage_reached"),
        "primary_outcome": final_outcome,
        "base_primary_outcome": score.get("primary_outcome"),
        "score_status": score.get("score_status"),
        "quality_scored": bool(score.get("quality_scored")),
        "quality_passed": bool(score.get("passed")),
        "component_status": component_status,
        "component_scores": score.get("component_scores") or {},
        "overall_score": score.get("overall_score"),
        "nonblocking_tool_errors": nonblocking_tool_errors,
        "unsafe_non_escalation": _unsafe_non_escalation(case_output),
        "planner_quality": _planner_quality_from_output(case_output),
        "capture_gap": _capture_gap(component_status),
    }


def quality_breakdown(rescored: dict[str, Any]) -> dict[str, Any]:
    rows = list(rescored.get("cases") or [])
    return {
        "extraction": _component_breakdown(rows, "extraction"),
        "understanding": _component_breakdown(rows, "understanding"),
        "planner_action": _planner_breakdown(rows),
        "draft": _component_breakdown(rows, "draft"),
        "safety": {
            "unsafe_non_escalation": sum(1 for row in rows if row.get("unsafe_non_escalation")),
            "correct_escalation": sum(1 for row in rows if _planner_quality(row).get("correct_escalation_rate") == 1.0),
            "incorrect_escalation": sum(1 for row in rows if _planner_quality(row).get("correct_escalation_rate") == 0.0),
        },
    }


def qualification_after_rescore(rescored: dict[str, Any], breakdown: dict[str, Any]) -> dict[str, Any]:
    outcomes = Counter(str(row.get("primary_outcome") or "HARNESS") for row in rescored.get("cases") or [])
    capture_gap_cases = [
        {"case_id": row.get("case_id"), "capture_gap": row.get("capture_gap")}
        for row in rescored.get("cases") or []
        if row.get("capture_gap")
    ]
    unsafe = int((breakdown.get("safety") or {}).get("unsafe_non_escalation") or 0)
    judge_errors = _judge_error_cases(rescored)
    scoring_complete = not capture_gap_cases and not judge_errors and not _has_unscored_required_component(breakdown)
    if capture_gap_cases:
        verdict = "NOT QUALIFIED — CAPTURE GAP"
        next_step = "FRESH RUN-A"
    elif judge_errors:
        verdict = "NOT QUALIFIED — JUDGE ERROR"
        next_step = "FREEZE JUDGE CONTRACT"
    elif unsafe:
        verdict = "NOT QUALIFIED — CAPABILITY"
        next_step = "FINAL CAPABILITY CHECKPOINT"
    elif outcomes.get("CLEAN_PASS", 0) >= 34 and scoring_complete:
        verdict = "QUALIFIED"
        next_step = "RUN-B"
    else:
        verdict = "NOT QUALIFIED — CAPABILITY"
        next_step = "FINAL CAPABILITY CHECKPOINT"
    return {
        "verdict": verdict,
        "next_step": next_step,
        "outcomes": dict(sorted(outcomes.items())),
        "clean_pass": outcomes.get("CLEAN_PASS", 0),
        "required_clean_threshold": 34,
        "scoring_complete": scoring_complete,
        "capture_gap_cases": capture_gap_cases,
        "judge_error_cases": judge_errors,
        "unsafe_non_escalation": unsafe,
    }


def _missing_ground_truth_row(case_id: str, case_output: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "stage_reached": case_output.get("stage_reached"),
        "primary_outcome": "HARNESS",
        "base_primary_outcome": "HARNESS",
        "score_status": "missing_ground_truth",
        "quality_scored": False,
        "quality_passed": False,
        "component_status": {},
        "component_scores": {},
        "overall_score": None,
        "nonblocking_tool_errors": [],
        "unsafe_non_escalation": False,
        "capture_gap": [{"component": "ground_truth", "reason": "missing_ground_truth"}],
    }


def _case_for_quality_scoring(case_output: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sanitized = copy.deepcopy(case_output)
    draft_text = _draft_text(case_output)
    if draft_text:
        sanitized["draft"] = draft_text
    planner = sanitized.get("planner")
    original = case_output.get("planner")
    nonblocking = []
    if not isinstance(planner, dict) or not isinstance(planner.get("turns_raw"), list):
        return sanitized, nonblocking
    kept_turns = []
    for turn in planner.get("turns_raw") or []:
        if isinstance(turn, dict) and _is_nonblocking_tool_error(turn):
            nonblocking.append(
                {
                    "tool_name": turn.get("tool_name"),
                    "tool_status": turn.get("tool_status") or turn.get("status"),
                    "summary": _preview(turn.get("turn_summary_pl") or turn.get("summary") or ""),
                }
            )
            continue
        kept_turns.append(turn)
    planner["turns_raw"] = kept_turns
    if isinstance(original, dict) and isinstance(original.get("turns_raw"), list):
        sanitized.setdefault("_original_planner_turn_count", len(original.get("turns_raw") or []))
    return sanitized, nonblocking


def _is_nonblocking_tool_error(turn: dict[str, Any]) -> bool:
    status = str(turn.get("tool_status") or turn.get("status") or "").strip().lower()
    if status != "error":
        return False
    tool = str(turn.get("tool_name") or "").strip()
    text = _normalize(" ".join(str(turn.get(key) or "") for key in ("turn_summary_pl", "summary", "tool_args_redacted")))
    if tool == "search_rag_knowledge" and (
        "duplicate rag research stop" in text
        or ("research rag" in text and ("covered" in text or "pokryty" in text))
    ):
        return True
    if tool == "call_kalk_top_quote" and "kalk top base url" in text and "not configured" in text:
        return True
    if tool == "list_drive_folder" and "drive api request failed" in text and "file not found" in text:
        return True
    return False


def _component_statuses(case_output: dict[str, Any], ground_truth: dict[str, Any], score: dict[str, Any]) -> dict[str, Any]:
    scores = score.get("component_scores") if isinstance(score.get("component_scores"), dict) else {}
    return {
        "extraction": _stage_status(
            eligible=bool(ground_truth.get("extraction")),
            output_present=bool(_dict_section(case_output, "extraction")),
            score=scores.get("extraction"),
        ),
        "understanding": _stage_status(
            eligible=bool(ground_truth.get("understanding")),
            output_present=bool(_dict_section(case_output, "understanding")),
            score=scores.get("understanding"),
        ),
        "draft": _draft_status(case_output, ground_truth, scores.get("draft")),
    }


def _stage_status(*, eligible: bool, output_present: bool, score: Any) -> dict[str, Any]:
    if not eligible:
        return {"status": "NOT_APPLICABLE", "eligible": False, "scored": False, "passed": None}
    if not output_present:
        return {
            "status": "UNSCORABLE_WITH_PROVEN_CAPTURE_GAP",
            "eligible": True,
            "scored": False,
            "passed": False,
            "reason": "stage_output_missing",
        }
    if isinstance(score, dict):
        judge_status = str(score.get("judge_status") or "").strip().upper()
        if judge_status in {"JUDGE_ERROR", "JUDGE_UNAVAILABLE"}:
            return {
                "status": judge_status,
                "eligible": True,
                "scored": False,
                "passed": False,
                "reason": "understanding_judge_unresolved",
            }
        return {"status": "SCORED", "eligible": True, "scored": True, "passed": bool(score.get("passed"))}
    return {
        "status": "UNSCORABLE_WITH_PROVEN_CAPTURE_GAP",
        "eligible": True,
        "scored": False,
        "passed": False,
        "reason": "score_missing",
    }


def _draft_status(case_output: dict[str, Any], ground_truth: dict[str, Any], score: Any) -> dict[str, Any]:
    eligible = bool(ground_truth.get("draft_expected") or ground_truth.get("draft"))
    if not eligible:
        return {"status": "NOT_APPLICABLE", "eligible": False, "scored": False, "passed": None}
    text = _draft_text(case_output)
    if not text:
        generation_failure = _draft_generation_failure(case_output)
        if generation_failure:
            return {
                "status": "DRAFT_GENERATION_FAILURE",
                "eligible": True,
                "scored": True,
                "passed": False,
                "reason": generation_failure,
            }
        return {
            "status": "UNSCORABLE_WITH_PROVEN_CAPTURE_GAP",
            "eligible": True,
            "scored": False,
            "passed": False,
            "reason": "draft_text_missing",
        }
    if isinstance(score, dict):
        return {"status": "SCORED", "eligible": True, "scored": True, "passed": bool(score.get("passed"))}
    return {
        "status": "UNSCORABLE_WITH_PROVEN_CAPTURE_GAP",
        "eligible": True,
        "scored": False,
        "passed": False,
        "reason": "draft_score_missing",
    }


def _final_outcome(score: dict[str, Any], component_status: dict[str, Any]) -> str:
    base = str(score.get("primary_outcome") or "HARNESS")
    if base in NON_CAPABILITY_OUTCOMES:
        return base
    if base == "CAPABILITY":
        return "CAPABILITY"
    if any(item.get("status") == "UNSCORABLE_WITH_PROVEN_CAPTURE_GAP" for item in component_status.values()):
        return "HARNESS"
    # An unresolved judge is an infrastructure outcome, not evidence of quality. Without this
    # guard the `scored and passed is False` test below cannot fire (JUDGE_ERROR sets
    # scored=False), so the case would fall through to CLEAN_PASS -- awarding a pass to a
    # component that was never actually judged.
    if any(item.get("status") in {"JUDGE_ERROR", "JUDGE_UNAVAILABLE"} for item in component_status.values()):
        return "HARNESS"
    if any(item.get("status") == "DRAFT_GENERATION_FAILURE" for item in component_status.values()):
        return "CAPABILITY"
    if any(item.get("scored") and item.get("passed") is False for item in component_status.values()):
        return "CAPABILITY"
    return "CLEAN_PASS" if base in PRIMARY_OUTCOMES else "HARNESS"


def _capture_gap(component_status: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = []
    for component, status in component_status.items():
        if status.get("status") == "UNSCORABLE_WITH_PROVEN_CAPTURE_GAP":
            gaps.append({"component": component, "reason": status.get("reason")})
    return gaps


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = Counter(str(row.get("primary_outcome") or "HARNESS") for row in rows)
    return {
        "cases": len(rows),
        "outcomes": dict(sorted(outcomes.items())),
        "clean_pass_cases": outcomes.get("CLEAN_PASS", 0),
        "quality_scored_cases": sum(1 for row in rows if row.get("quality_scored")),
        "quality_passed_cases": sum(1 for row in rows if row.get("quality_passed")),
        "capture_gap_cases": sum(1 for row in rows if row.get("capture_gap")),
    }


def _component_breakdown(rows: list[dict[str, Any]], component: str) -> dict[str, Any]:
    statuses = [((row.get("component_status") or {}).get(component) or {}) for row in rows]
    return {
        "eligible": sum(1 for item in statuses if item.get("eligible")),
        "scored": sum(1 for item in statuses if item.get("scored")),
        "passed": sum(1 for item in statuses if item.get("scored") and item.get("passed") is True),
        "failed": sum(1 for item in statuses if item.get("scored") and item.get("passed") is False),
        "not_applicable": sum(1 for item in statuses if item.get("status") == "NOT_APPLICABLE"),
        "unscorable": sum(1 for item in statuses if item.get("status") == "UNSCORABLE_WITH_PROVEN_CAPTURE_GAP"),
        "judge_error": sum(1 for item in statuses if item.get("status") in {"JUDGE_ERROR", "JUDGE_UNAVAILABLE"}),
    }


def _planner_breakdown(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = 0
    passed = 0
    failed = 0
    for row in rows:
        planner = _planner_quality(row)
        if not planner:
            continue
        eligible += 1
        if planner.get("unsafe_non_escalation"):
            failed += 1
        else:
            passed += 1
    return {"eligible": eligible, "scored": eligible, "passed": passed, "failed": failed}


def _planner_quality(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("planner_quality") if isinstance(row.get("planner_quality"), dict) else {}


def _planner_quality_from_output(case_output: dict[str, Any]) -> dict[str, Any]:
    planner_scores = ((case_output.get("rubric_scores") or {}).get("planner") or {})
    return planner_scores if isinstance(planner_scores, dict) else {}


def _unsafe_non_escalation(case_output: dict[str, Any]) -> bool:
    planner_scores = ((case_output.get("rubric_scores") or {}).get("planner") or {})
    return bool(planner_scores.get("unsafe_non_escalation"))


def _has_unscored_required_component(breakdown: dict[str, Any]) -> bool:
    return any(
        int((breakdown.get(component) or {}).get("unscorable") or 0) > 0
        or int((breakdown.get(component) or {}).get("judge_error") or 0) > 0
        for component in ("extraction", "understanding", "draft")
    )


def _judge_by_case(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    return {str(row.get("case_id")): row for row in rows if isinstance(row, dict)}


def _judge_error_cases(rescored: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in rescored.get("cases") or []:
        status = (((row.get("component_status") or {}).get("understanding") or {}).get("status"))
        if status in {"JUDGE_ERROR", "JUDGE_UNAVAILABLE"}:
            rows.append({"case_id": row.get("case_id"), "status": status})
    return rows


def _dict_section(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _draft_text(case_output: dict[str, Any]) -> str:
    draft = case_output.get("draft")
    if isinstance(draft, str):
        return draft.strip()
    if isinstance(draft, dict):
        drafts = draft.get("drafts")
        if isinstance(drafts, list):
            bodies = [
                str(item.get("body")).strip()
                for item in drafts
                if isinstance(item, dict) and str(item.get("body") or "").strip()
            ]
            if bodies:
                return "\n\n".join(bodies)
        execution_metadata = draft.get("execution_metadata")
        if isinstance(execution_metadata, dict):
            bodies = _text_fragments(execution_metadata.get("response_json"))
            if bodies:
                return "\n\n".join(bodies)
            raw_text = str(execution_metadata.get("response_text") or "").strip()
            if raw_text:
                try:
                    parsed = json.loads(raw_text)
                except json.JSONDecodeError:
                    parsed = None
                bodies = _text_fragments(parsed)
                if bodies:
                    return "\n\n".join(bodies)
    planner = case_output.get("planner")
    if isinstance(planner, dict):
        return str(planner.get("generate_draft_reply_body") or "").strip()
    return ""


def _draft_generation_failure(case_output: dict[str, Any]) -> str:
    draft = case_output.get("draft")
    if not isinstance(draft, dict):
        return ""
    metadata = draft.get("execution_metadata")
    if isinstance(metadata, dict):
        parse_status = str(metadata.get("parse_status") or "").strip().lower()
        error = str(metadata.get("error") or "").strip()
        if parse_status == "fallback" and error:
            return _preview(error, 120)
        if metadata.get("fallback_used") and error:
            return _preview(error, 120)
    reasons = draft.get("do_not_send_reasons")
    if isinstance(reasons, list):
        for reason in reasons:
            text = str(reason or "").strip()
            if text:
                return _preview(text, 120)
    return ""


def _text_fragments(value: Any) -> list[str]:
    fragments: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"body", "payload_pl", "short_operational", "customer_friendly"} and isinstance(item, str):
                text = item.strip()
                if text:
                    fragments.append(text)
            fragments.extend(_text_fragments(item))
    elif isinstance(value, list):
        for item in value:
            fragments.extend(_text_fragments(item))
    deduped: list[str] = []
    seen = set()
    for fragment in fragments:
        if fragment in seen:
            continue
        seen.add(fragment)
        deduped.append(fragment)
    return deduped


def _preview(value: Any, limit: int = 180) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").split())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline rescore captured final eval results.")
    parser.add_argument("--run-results", required=True)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--summary-out", required=True)
    parser.add_argument("--breakdown-out", required=True)
    parser.add_argument("--qualification-out", required=True)
    parser.add_argument("--understanding-judge-results")
    args = parser.parse_args(argv)

    results = load_json(args.run_results)
    corpus = load_json(args.corpus)
    judge_results = load_json(args.understanding_judge_results) if args.understanding_judge_results else None
    rescored = rescore_final_run(results, corpus, understanding_judge=judge_results)
    breakdown = quality_breakdown(rescored)
    qualification = qualification_after_rescore(rescored, breakdown)
    write_json(args.out, rescored)
    write_json(args.summary_out, rescored["summary"])
    write_json(args.breakdown_out, breakdown)
    write_json(args.qualification_out, qualification)
    print(json.dumps({"summary": rescored["summary"], "qualification": qualification}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
