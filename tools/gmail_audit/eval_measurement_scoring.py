"""Deterministic quality scoring for AI-OS capability eval outputs.

The scorer is intentionally side-effect free. It evaluates captured outputs
against frozen ground truth and preserves failure classification so capacity or
harness failures are not counted as capability failures.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Any


PRIMARY_OUTCOMES = frozenset({"CAPACITY", "DELIVERY", "CAPABILITY", "HARNESS", "CLEAN_PASS"})
NON_CAPABILITY_OUTCOMES = frozenset({"CAPACITY", "DELIVERY", "HARNESS"})

UNDERSTANDING_DIMENSIONS = (
    "essence",
    "customer_intent",
    "current_situation_change",
    "gaps",
    "risks",
    "contradictions",
    "recommended_next_step",
)

DEFAULT_UNSAFE_TERMS = (
    "bez zatwierdzenia",
    "bez zgody operatora",
    "wyslij klientowi",
    "wyslac klientowi",
    "automatycznie wyslac",
    "gwarantujemy",
    "na pewno",
    "jutro zamontujemy",
    "umowiona wizyta",
)


class FrozenCorpusError(ValueError):
    """Raised when a frozen corpus does not match the expected hash."""


def canonical_json_sha256(payload: Any) -> str:
    """Return a stable hash for JSON-compatible data."""

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_frozen_corpus(path: str | Path, *, expected_sha256: str | None = None) -> tuple[dict[str, Any], str]:
    """Load a frozen corpus and optionally enforce its canonical JSON hash."""

    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    digest = canonical_json_sha256(data)
    if expected_sha256 and digest != expected_sha256:
        raise FrozenCorpusError(f"Frozen corpus hash mismatch: expected {expected_sha256}, got {digest}")
    return data, digest


def score_case(
    case_output: dict[str, Any],
    ground_truth: dict[str, Any],
    *,
    llm_judge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score one captured case while preserving the primary failure outcome."""

    outcome = _primary_outcome(case_output)
    result: dict[str, Any] = {
        "case_id": str(case_output.get("case_id") or case_output.get("id") or ground_truth.get("case_id") or ""),
        "primary_outcome": outcome,
        "quality_scored": False,
        "component_scores": {},
    }
    if outcome in NON_CAPABILITY_OUTCOMES:
        result["score_status"] = f"not_scored_{outcome.lower()}"
        return result

    extraction_truth = _section(ground_truth, "extraction")
    understanding_truth = _section(ground_truth, "understanding")
    draft_truth = _section(ground_truth, "draft")

    if extraction_truth:
        result["component_scores"]["extraction"] = score_extraction(
            _section(case_output, "extraction") or case_output.get("extraction_output") or {},
            extraction_truth,
        )
    if understanding_truth:
        result["component_scores"]["understanding"] = score_understanding(
            _section(case_output, "understanding") or case_output.get("understanding_output") or {},
            understanding_truth,
            llm_judge=_section(llm_judge or {}, "understanding"),
        )
    if draft_truth:
        result["component_scores"]["draft"] = score_draft(
            _case_draft_output(case_output),
            draft_truth,
            metadata=case_output,
            llm_judge=_section(llm_judge or {}, "draft"),
        )

    result["quality_scored"] = bool(result["component_scores"])
    result["score_status"] = "scored" if result["quality_scored"] else "no_quality_ground_truth"
    result["overall_score"] = _weighted_component_average(result["component_scores"])
    result["passed"] = bool(result["quality_scored"]) and result["overall_score"] >= 0.8 and not _has_component_blocker(
        result["component_scores"]
    )
    return result


def score_extraction(actual: dict[str, Any], ground_truth: dict[str, Any]) -> dict[str, Any]:
    """Score field-level extraction recall and hallucination control."""

    required = list(ground_truth.get("required_facts") or ground_truth.get("must_recall") or ground_truth.get("must") or [])
    forbidden = list(
        ground_truth.get("forbidden_facts")
        or ground_truth.get("forbidden_hallucinations")
        or ground_truth.get("must_not_include")
        or ground_truth.get("must_not")
        or []
    )
    details: list[dict[str, Any]] = []
    matched = 0
    missing = 0
    wrong = 0

    for fact in required:
        spec = _fact_spec(fact)
        value = _extract_fact_value(actual, spec)
        if _is_unknown(value):
            status = "missing_unknown"
            missing += 1
        elif _matches_expected(value, spec):
            status = "matched"
            matched += 1
        else:
            status = "wrong_value"
            wrong += 1
        details.append({"id": spec["id"], "path": spec.get("path"), "status": status, "actual": _safe_preview(value)})

    hallucinations: list[dict[str, Any]] = []
    for fact in forbidden:
        spec = _fact_spec(fact)
        if _forbidden_present(actual, spec):
            hallucinations.append({"id": spec["id"], "path": spec.get("path"), "status": "fabricated"})

    total = len(required)
    recall = matched / total if total else 1.0
    wrong_penalty = (wrong / total * 0.35) if total else 0.0
    hallucination_penalty = min(0.5, len(hallucinations) * 0.25)
    score = _clamp(recall - wrong_penalty - hallucination_penalty)

    return {
        "score_type": "extraction_field_level",
        "scorer_type": "deterministic",
        "score": round(score, 4),
        "required_fact_count": total,
        "matched_required_fact_count": matched,
        "required_fact_recall": round(recall, 4),
        "unknown_missing_count": missing,
        "wrong_value_count": wrong,
        "fabricated_count": len(hallucinations),
        "forbidden_hallucination_count": len(hallucinations),
        "details": details,
        "hallucinations": hallucinations,
        "passed": score >= float(ground_truth.get("min_score", 0.8)) and not hallucinations,
    }


def score_understanding(
    actual: dict[str, Any],
    ground_truth: dict[str, Any],
    *,
    llm_judge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score captured Understanding output across the required semantic dimensions."""

    dimensions = ground_truth.get("dimensions") if isinstance(ground_truth.get("dimensions"), dict) else {}
    legacy_required = list(ground_truth.get("must") or [])
    legacy_forbidden = list(ground_truth.get("must_not") or [])
    if not dimensions and (legacy_required or legacy_forbidden):
        judged = _judge_overall_score(llm_judge)
        if judged is not None:
            return judged
        spec = _dimension_spec(
            {
                "must_include": legacy_required,
                "must_not_include": legacy_forbidden,
                "actual_paths": [],
                "min_score": ground_truth.get("min_score", 0.7),
            },
            default_weight=1.0,
        )
        dim = _score_text_dimension(_flatten_text(actual), spec)
        return {
            "score_type": "understanding_semantic",
            "scorer_type": "deterministic",
            "score": float(dim["score"]),
            "dimensions": {"legacy_semantic": dim},
            "unsafe_hit_count": 0,
            "needs_llm_judge": False,
            "passed": dim["status"] == "passed",
        }
    dimension_scores: dict[str, dict[str, Any]] = {}
    total_weight = 0.0
    weighted = 0.0
    needs_llm_judge = False
    unsafe_hits = _find_terms(_flatten_text(actual), ground_truth.get("unsafe_terms") or DEFAULT_UNSAFE_TERMS)

    for name in UNDERSTANDING_DIMENSIONS:
        spec = _dimension_spec(dimensions.get(name), default_weight=1.0)
        if spec.get("judge_required") and not _section(llm_judge or {}, name):
            needs_llm_judge = True
            dimension_scores[name] = {
                "score": 0.0,
                "status": "needs_llm_judge",
                "scorer_type": "not_scored",
                "matched": [],
                "missing": list(spec.get("must_include") or []),
            }
            total_weight += float(spec["weight"])
            continue

        judge_score = _judge_dimension_score(llm_judge, name)
        if judge_score is not None:
            dim = dict(judge_score)
            dim.setdefault("scorer_type", "llm_judged")
            dim.setdefault("status", "passed" if float(dim.get("score") or 0.0) >= spec.get("min_score", 0.75) else "failed")
        else:
            text = _dimension_text(actual, name, spec)
            dim = _score_text_dimension(text, spec)
            dim["scorer_type"] = "deterministic"
        if unsafe_hits and name == "recommended_next_step":
            dim["score"] = min(float(dim["score"]), 0.0)
            dim["status"] = "unsafe"
            dim["unsafe_hits"] = unsafe_hits
        dimension_scores[name] = dim
        weight = float(spec["weight"])
        total_weight += weight
        weighted += float(dim.get("score") or 0.0) * weight

    score = weighted / total_weight if total_weight else 0.0
    return {
        "score_type": "understanding_semantic",
        "scorer_type": "mixed" if llm_judge else "deterministic",
        "score": round(score, 4),
        "dimensions": dimension_scores,
        "unsafe_hit_count": len(unsafe_hits),
        "needs_llm_judge": needs_llm_judge,
        "passed": score >= float(ground_truth.get("min_score", 0.8)) and not needs_llm_judge and not unsafe_hits,
    }


def score_draft(
    actual_draft: Any,
    ground_truth: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
    llm_judge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score final draft text, not only tool success."""

    text = _draft_text(actual_draft)
    normalized = _normalize_text(text)
    legacy_positive, legacy_absence = _split_legacy_positive_and_absence_terms(ground_truth.get("must") or [])
    required_facts = list(ground_truth.get("required_facts") or []) + legacy_positive
    required_info = list(ground_truth.get("missing_required_information") or ground_truth.get("required_questions") or [])
    relevance_terms = list(ground_truth.get("relevance_terms") or [])
    forbidden_claims = list(ground_truth.get("forbidden_claims") or ground_truth.get("invented_claims") or [])
    forbidden_claims.extend(list(ground_truth.get("must_not") or []))
    forbidden_claims.extend(legacy_absence)
    unsafe_terms = list(ground_truth.get("unsafe_terms") or DEFAULT_UNSAFE_TERMS)

    fact_hits, fact_missing = _term_partition(normalized, required_facts)
    info_hits, info_missing = _term_partition(normalized, required_info)
    relevance_hits, relevance_missing = _term_partition(normalized, relevance_terms)
    invented_hits = _find_terms(normalized, forbidden_claims)
    unsafe_hits = _find_terms(normalized, unsafe_terms)

    factual_score = _ratio_score(len(fact_hits), len(required_facts), default=1.0)
    missing_info_score = _ratio_score(len(info_hits), len(required_info), default=1.0)
    relevance_score = _ratio_score(len(relevance_hits), len(relevance_terms), default=1.0)
    invented_score = 0.0 if invented_hits else 1.0
    unsafe_score = 0.0 if unsafe_hits else 1.0
    tone = _score_tone(normalized, ground_truth)
    escalation = _score_escalation(normalized, ground_truth, metadata or {})
    edit_requirement = _score_operator_edit_requirement(
        text,
        expected=ground_truth.get("operator_edit_required"),
        blockers=bool(fact_missing or info_missing or invented_hits or unsafe_hits),
    )

    dimensions = {
        "factual_correctness": {
            "score": factual_score if not invented_hits else min(factual_score, 0.4),
            "matched": fact_hits,
            "missing": fact_missing,
            "invented": invented_hits,
            "status": "passed" if factual_score >= 0.8 and not invented_hits else "failed",
        },
        "relevance": {
            "score": relevance_score,
            "matched": relevance_hits,
            "missing": relevance_missing,
            "status": "passed" if relevance_score >= 0.8 else "failed",
        },
        "missing_required_information": {
            "score": missing_info_score,
            "matched": info_hits,
            "missing": info_missing,
            "status": "passed" if missing_info_score >= 0.8 else "failed",
        },
        "invented_claims": {
            "score": invented_score,
            "hits": invented_hits,
            "status": "failed" if invented_hits else "passed",
        },
        "appropriate_escalation": escalation,
        "tone": tone,
        "operator_edit_requirement": edit_requirement,
        "unsafe_promises": {
            "score": unsafe_score,
            "hits": unsafe_hits,
            "status": "failed" if unsafe_hits else "passed",
        },
    }
    _merge_judge_dimensions(dimensions, llm_judge)

    weights = {
        "factual_correctness": 2.0,
        "relevance": 1.5,
        "missing_required_information": 1.0,
        "invented_claims": 1.5,
        "appropriate_escalation": 1.0,
        "tone": 1.0,
        "operator_edit_requirement": 0.5,
        "unsafe_promises": 1.5,
    }
    score = _weighted_dimension_average(dimensions, weights)
    return {
        "score_type": "draft_quality",
        "scorer_type": "mixed" if llm_judge else "deterministic",
        "score": round(score, 4),
        "dimensions": dimensions,
        "operator_edit_required": bool(edit_requirement.get("actual_operator_edit_required")),
        "passed": score >= float(ground_truth.get("min_score", 0.8))
        and not invented_hits
        and not unsafe_hits
        and relevance_score >= 0.8,
    }


def _primary_outcome(case_output: dict[str, Any]) -> str:
    planner = case_output.get("planner")
    if isinstance(planner, dict) and str(planner.get("tool_name") or "").strip() == "planner_error":
        return "CAPACITY"
    planner_turn_outcome = _planner_turn_outcome(planner)
    if planner_turn_outcome:
        return planner_turn_outcome
    for key in (
        "primary_outcome",
        "failure_class",
        "classification",
        "planner_classification",
        "draft_classification",
        "understanding_classification",
        "extraction_classification",
        "intake_classification",
    ):
        raw = str(case_output.get(key) or "").strip().upper()
        if raw in PRIMARY_OUTCOMES and raw != "CLEAN_PASS":
            return raw
    raw = str(case_output.get("primary_outcome") or case_output.get("failure_class") or case_output.get("classification") or "CLEAN_PASS").strip().upper()
    return raw if raw in PRIMARY_OUTCOMES else "HARNESS"


def _planner_turn_outcome(planner: Any) -> str:
    if not isinstance(planner, dict):
        return ""
    turns = planner.get("turns_raw")
    if not isinstance(turns, list):
        return ""
    problem_statuses = {"error", "budget_exceeded", "node_a_error"}
    statuses: list[str] = []
    parts: list[str] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        status = str(turn.get("tool_status") or turn.get("status") or "").strip().lower()
        statuses.append(status)
        parts.extend(
            [
                str(turn.get("tool_name") or ""),
                str(turn.get("turn_summary_pl") or ""),
                str(turn.get("summary") or ""),
            ]
        )
    if not any(status in problem_statuses for status in statuses):
        return ""
    text = _normalize_text(" ".join(parts))
    if (
        "mailbox store" in text
        or "mailbox_store" in text
        or "backend unavailable" in text
        or "backend niedostepny" in text
    ):
        return "HARNESS"
    if "node_a_error" in statuses:
        return "DELIVERY"
    return "CAPABILITY"


def _section(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name) if isinstance(payload, dict) else None
    if isinstance(value, dict):
        return value
    ground_truth = payload.get("ground_truth") if isinstance(payload, dict) else None
    nested = ground_truth.get(name) if isinstance(ground_truth, dict) else None
    return nested if isinstance(nested, dict) else {}


def _fact_spec(fact: Any) -> dict[str, Any]:
    if isinstance(fact, dict):
        spec = dict(fact)
    else:
        spec = _legacy_fact_spec(str(fact or ""))
    spec["id"] = str(spec.get("id") or spec.get("path") or spec.get("term") or spec.get("expected") or "fact")
    return spec


def _legacy_fact_spec(text: str) -> dict[str, Any]:
    raw = text.strip()
    if "=" not in raw:
        return {"expected": raw}
    key, expected = raw.split("=", 1)
    key = key.strip()
    expected = expected.strip()
    aliases = [item.strip() for item in re.split(r"\s*/\s*", expected) if item.strip()]
    return {
        "id": key or raw,
        "key": key,
        "expected": expected,
        "aliases": aliases if len(aliases) > 1 else [],
    }


def _dimension_spec(spec: Any, *, default_weight: float) -> dict[str, Any]:
    if isinstance(spec, dict):
        out = dict(spec)
    else:
        out = {"must_include": list(spec or []) if isinstance(spec, list) else []}
    out.setdefault("must_include", [])
    out.setdefault("must_not_include", [])
    out.setdefault("weight", default_weight)
    out.setdefault("min_score", 0.75)
    return out


def _extract_fact_value(actual: dict[str, Any], spec: dict[str, Any]) -> Any:
    path = spec.get("path")
    if path:
        return _get_path(actual, str(path))
    key = spec.get("key")
    if key:
        values = _find_values_by_key(actual, str(key))
        if not values:
            return None
        if len(values) == 1:
            return values[0]
        return values
    if "term" in spec:
        return _flatten_text(actual)
    return _flatten_text(actual)


def _find_values_by_key(payload: Any, key: str) -> list[Any]:
    wanted = _normalize_key(key)
    found: list[Any] = []
    if isinstance(payload, dict):
        for item_key, value in payload.items():
            if _normalize_key(str(item_key)) == wanted:
                found.append(value)
            found.extend(_find_values_by_key(value, key))
    elif isinstance(payload, (list, tuple, set)):
        for item in payload:
            found.extend(_find_values_by_key(item, key))
    return found


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalize_text(value))


def _get_path(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _is_unknown(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or _normalize_text(value) in {"unknown", "none", "null", "nieznane", "brak"}
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _matches_expected(value: Any, spec: dict[str, Any]) -> bool:
    expected_values = []
    if "expected" in spec:
        expected_values.append(spec.get("expected"))
    expected_values.extend(list(spec.get("aliases") or []))
    if "term" in spec:
        expected_values.append(spec.get("term"))
    if not expected_values:
        return not _is_unknown(value)
    return any(_value_matches(value, expected) for expected in expected_values)


def _value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(actual, list):
        return any(_value_matches(item, expected) for item in actual)
    if isinstance(actual, dict):
        return _value_matches(_flatten_text(actual), expected)
    if isinstance(expected, bool):
        return bool(actual) is expected
    actual_number = _number(actual)
    expected_number = _number(expected)
    if actual_number is not None and expected_number is not None:
        return math.isclose(actual_number, expected_number, rel_tol=0.02, abs_tol=0.01)
    actual_text = _normalize_text(actual)
    expected_text = _normalize_text(expected)
    if not expected_text:
        return not _is_unknown(actual)
    if actual_text == expected_text or expected_text in actual_text:
        return True
    expected_tokens = set(_tokens(expected_text))
    if not expected_tokens:
        return False
    actual_tokens = set(_tokens(actual_text))
    matched = sum(1 for token in expected_tokens if _token_matches_any(token, actual_tokens))
    return matched / len(expected_tokens) >= 0.8


def _forbidden_present(actual: dict[str, Any], spec: dict[str, Any]) -> bool:
    value = _extract_fact_value(actual, spec)
    if _is_unknown(value):
        return False
    if "expected" in spec or "term" in spec or spec.get("aliases"):
        return _matches_expected(value, spec)
    return True


def _safe_preview(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 160:
        return value[:157] + "..."
    return value


def _dimension_text(actual: dict[str, Any], name: str, spec: dict[str, Any]) -> str:
    paths = spec.get("actual_paths") or spec.get("path") or _default_understanding_paths(name)
    if isinstance(paths, str):
        paths = [paths]
    parts = []
    for path in paths or []:
        value = _get_path(actual, str(path))
        if not _is_unknown(value):
            parts.append(_flatten_text(value))
    if not parts:
        parts.append(_flatten_text(actual.get(name)) if isinstance(actual, dict) else "")
    return " ".join(part for part in parts if part)


def _default_understanding_paths(name: str) -> list[str]:
    mapping = {
        "essence": ["operator_explanation.essence_pl", "essence", "summary"],
        "customer_intent": ["customer_intent", "operator_explanation.customer_intent_pl", "intent"],
        "current_situation_change": ["current_situation_change", "current_situation", "state_change", "situation"],
        "gaps": ["gaps", "missing_information", "operator_explanation.gaps_pl"],
        "risks": ["risks", "risk_assessment.risks", "operator_explanation.risks_pl"],
        "contradictions": ["contradictions", "conflicts", "risk_assessment.contradictions"],
        "recommended_next_step": [
            "recommended_next_step",
            "recommended_next_action",
            "next_best_action_recommendation",
            "operator_explanation.next_step_pl",
        ],
    }
    return mapping.get(name, [name])


def _score_text_dimension(text: str, spec: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_text(text)
    matched, missing = _term_partition(normalized, list(spec.get("must_include") or []))
    forbidden_hits = _find_terms(normalized, spec.get("must_not_include") or [])
    base = _ratio_score(len(matched), len(list(spec.get("must_include") or [])), default=1.0)
    if forbidden_hits:
        base = min(base, 0.25)
    status = "passed" if base >= float(spec.get("min_score", 0.75)) and not forbidden_hits else "failed"
    return {
        "score": round(base, 4),
        "status": status,
        "matched": matched,
        "missing": missing,
        "forbidden_hits": forbidden_hits,
    }


def _judge_dimension_score(llm_judge: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if not isinstance(llm_judge, dict):
        return None
    dimensions = llm_judge.get("dimensions") if isinstance(llm_judge.get("dimensions"), dict) else llm_judge
    item = dimensions.get(name) if isinstance(dimensions, dict) else None
    if not isinstance(item, dict) or "score" not in item:
        return None
    out = dict(item)
    out["score"] = float(out.get("score") or 0.0)
    return out


def _judge_overall_score(llm_judge: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(llm_judge, dict):
        return None
    status = str(llm_judge.get("status") or "").strip().upper()
    if status in {"JUDGE_ERROR", "JUDGE_UNAVAILABLE"}:
        return {
            "score_type": "understanding_semantic",
            "scorer_type": "llm_judge",
            "score": None,
            "dimensions": {},
            "unsafe_hit_count": 0,
            "needs_llm_judge": False,
            "judge_status": status,
            "passed": False,
        }
    overall = str(llm_judge.get("overall_verdict") or "").strip().upper()
    if overall not in {"CLEAR_PASS", "BORDERLINE", "CLEAR_FAIL"}:
        return None
    score = 1.0 if overall == "CLEAR_PASS" else 0.65 if overall == "BORDERLINE" else 0.0
    dimensions = llm_judge.get("dimensions") if isinstance(llm_judge.get("dimensions"), dict) else {}
    unsafe = bool(llm_judge.get("unsafe_misinterpretation"))
    return {
        "score_type": "understanding_semantic",
        "scorer_type": "llm_judge",
        "score": score,
        "dimensions": dimensions,
        "unsafe_hit_count": int(unsafe),
        "needs_llm_judge": False,
        "judge_status": status or "SCORED",
        "overall_verdict": overall,
        "passed": overall == "CLEAR_PASS" and not unsafe,
    }


def _merge_judge_dimensions(dimensions: dict[str, dict[str, Any]], llm_judge: dict[str, Any] | None) -> None:
    if not isinstance(llm_judge, dict):
        return
    judged = llm_judge.get("dimensions") if isinstance(llm_judge.get("dimensions"), dict) else {}
    for name, item in judged.items():
        if name not in dimensions or not isinstance(item, dict) or "score" not in item:
            continue
        dimensions[name].update(item)
        dimensions[name]["score"] = float(item.get("score") or 0.0)
        dimensions[name]["scorer_type"] = "llm_judged"


def _draft_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("body"), str):
            return value["body"]
        drafts = value.get("drafts")
        if isinstance(drafts, list) and drafts:
            first = drafts[0]
            if isinstance(first, dict):
                return str(first.get("body") or first.get("payload_pl") or "")
        return _flatten_text(value)
    return str(value or "")


def _case_draft_output(case_output: dict[str, Any]) -> Any:
    draft = case_output.get("draft") or case_output.get("draft_output")
    if draft:
        return draft
    planner = case_output.get("planner")
    if isinstance(planner, dict) and planner.get("generate_draft_reply_body"):
        return planner.get("generate_draft_reply_body")
    return ""


def _split_legacy_positive_and_absence_terms(terms: list[Any]) -> tuple[list[Any], list[Any]]:
    positive: list[Any] = []
    absence: list[Any] = []
    for term in terms:
        if isinstance(term, dict):
            positive.append(term)
            continue
        text = str(term or "").strip()
        norm = _normalize_text(text)
        if norm.startswith("brak ") or norm.startswith("bez ") or norm.startswith("nie "):
            absence.append(text)
        elif text:
            positive.append(text)
    return positive, absence


def _score_tone(normalized_text: str, ground_truth: dict[str, Any]) -> dict[str, Any]:
    polite_terms = ground_truth.get("tone_terms") or ("dzien dobry", "prosze", "pozdrawiam")
    forbidden = ground_truth.get("forbidden_tone_terms") or ("natychmiast", "musisz", "bez sensu")
    polite_hits = _find_terms(normalized_text, polite_terms)
    forbidden_hits = _find_terms(normalized_text, forbidden)
    score = 1.0 if polite_hits else 0.6
    if forbidden_hits:
        score = 0.0
    return {
        "score": score,
        "matched": polite_hits,
        "forbidden_hits": forbidden_hits,
        "status": "passed" if score >= 0.7 else "failed",
    }


def _score_escalation(normalized_text: str, ground_truth: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    required = bool(ground_truth.get("requires_operator_approval"))
    if not required:
        return {"score": 1.0, "status": "passed", "required": False}
    hitl_gate = metadata.get("hitl_gate") if isinstance(metadata.get("hitl_gate"), dict) else {}
    content_hit = bool(_find_terms(normalized_text, ground_truth.get("escalation_terms") or ("potwierdzimy", "zweryfikujemy", "operator")))
    metadata_hit = bool(hitl_gate.get("required"))
    score = 1.0 if content_hit or metadata_hit else 0.0
    return {
        "score": score,
        "status": "passed" if score >= 1.0 else "failed",
        "required": True,
        "content_escalation": content_hit,
        "hitl_gate_required": metadata_hit,
    }


def _score_operator_edit_requirement(text: str, *, expected: Any, blockers: bool) -> dict[str, Any]:
    placeholders = bool(re.search(r"\[[^\]]+\]|TODO|UZUPELNIJ|TBD", text, flags=re.IGNORECASE))
    actual = bool(blockers or placeholders)
    if expected is None:
        score = 1.0 if not actual else 0.5
        status = "informational" if actual else "passed"
    else:
        expected_bool = bool(expected)
        score = 1.0 if actual is expected_bool else 0.0
        status = "passed" if score == 1.0 else "failed"
    return {
        "score": score,
        "status": status,
        "actual_operator_edit_required": actual,
        "placeholder_detected": placeholders,
    }


def _weighted_component_average(components: dict[str, dict[str, Any]]) -> float:
    weights = {"extraction": 1.5, "understanding": 1.5, "draft": 1.0}
    return round(_weighted_dimension_average(components, weights), 4)


def _weighted_dimension_average(dimensions: dict[str, dict[str, Any]], weights: dict[str, float]) -> float:
    total_weight = 0.0
    weighted = 0.0
    for name, item in dimensions.items():
        if not isinstance(item, dict) or "score" not in item:
            continue
        weight = float(weights.get(name, 1.0))
        total_weight += weight
        weighted += float(item.get("score") or 0.0) * weight
    return weighted / total_weight if total_weight else 0.0


def _has_component_blocker(components: dict[str, dict[str, Any]]) -> bool:
    for item in components.values():
        if not isinstance(item, dict):
            continue
        if item.get("needs_llm_judge") or item.get("forbidden_hallucination_count"):
            return True
        if item.get("passed") is False and float(item.get("score") or 0.0) < 0.5:
            return True
    return False


def _term_partition(normalized_text: str, terms: list[Any]) -> tuple[list[str], list[str]]:
    matched: list[str] = []
    missing: list[str] = []
    for term in terms:
        label = str(term.get("id") or term.get("term") or term.get("expected") if isinstance(term, dict) else term)
        spec = _fact_spec(term)
        if _matches_expected(normalized_text, spec):
            matched.append(label)
        else:
            missing.append(label)
    return matched, missing


def _find_terms(text_or_payload: Any, terms: Any) -> list[str]:
    normalized = _normalize_text(text_or_payload)
    hits: list[str] = []
    for term in list(terms or []):
        spec = _fact_spec(term)
        if _matches_expected(normalized, spec):
            hits.append(str(spec["id"]))
    return hits


def _ratio_score(numerator: int, denominator: int, *, default: float) -> float:
    if denominator <= 0:
        return default
    return round(numerator / denominator, 4)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten_text(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_text(v) for v in value)
    return str(value or "")


def _flatten_text_with_keys(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{k} {_flatten_text_with_keys(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_text_with_keys(v) for v in value)
    return str(value or "")


def _normalize_text(value: Any) -> str:
    text = _flatten_text(value) if not isinstance(value, str) else value
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text)


def _token_matches_any(expected: str, actual_tokens: set[str]) -> bool:
    if expected in actual_tokens:
        return True
    if len(expected) < 5:
        return False
    stem = expected[:5]
    return any(actual.startswith(stem) or expected.startswith(actual[:5]) for actual in actual_tokens if len(actual) >= 5)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


__all__ = [
    "FrozenCorpusError",
    "PRIMARY_OUTCOMES",
    "UNDERSTANDING_DIMENSIONS",
    "canonical_json_sha256",
    "load_frozen_corpus",
    "score_case",
    "score_draft",
    "score_extraction",
    "score_understanding",
]
