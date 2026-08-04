"""Frozen semantic judge contract for final eval Understanding scoring.

This module is eval tooling only. It scores captured Understanding outputs from
saved eval artifacts and never calls the system under test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError

from api_key_pool import parse_api_key_pool
from eval_measurement_scoring import canonical_json_sha256
from llm_client import TopInstalLLMClient, TopInstalLLMError


JUDGE_CONTRACT_VERSION = "understanding-semantic-judge.v1"
JUDGE_PROVIDER = "anthropic"
JUDGE_MODEL = "claude-sonnet-4-20250514"
JUDGE_ENDPOINT = "https://api.anthropic.com/v1/messages"
JUDGE_ANTHROPIC_VERSION = "2023-06-01"
OPENAI_NATIVE_PROVIDER = "openai_native"
OPENAI_NATIVE_ENDPOINT = "https://api.openai.com/v1"
OPENAI_NATIVE_MODEL = "gpt-4o-mini"
OPENROUTER_PROVIDER = "openrouter_openai"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "openai/gpt-4o-mini"
GROQ_PROVIDER = "groq"
GROQ_ENDPOINT = "https://api.groq.com/openai/v1"
GROQ_MODEL = "llama-3.3-70b-versatile"
JUDGE_TEMPERATURE = 0.0
JUDGE_MAX_TOKENS = 450
JUDGE_TIMEOUT_SEC = 60.0
JUDGE_MAX_RETRIES = 3
CALIBRATION_CASE_IDS = (
    "INT-01",
    "NEW-04",
    "NEW-05",
    "FU-05",
    "SVC-03",
    "DOC-01",
    "DOC-04",
    "CTX-03",
    "MI-02",
    "MI-04",
)
DIMENSIONS = (
    "essence",
    "intent",
    "current_state_change",
    "gaps",
    "risks",
    "contradictions",
    "recommended_next_step",
)
VERDICT_SCORE = {"PASS": 1.0, "BORDERLINE": 0.65, "FAIL": 0.0}
OVERALL_TO_DIMENSION = {"CLEAR_PASS": "PASS", "BORDERLINE": "BORDERLINE", "CLEAR_FAIL": "FAIL"}


PROMPT_FINAL = """You are an independent judge for AI-OS TOP-INSTAL Understanding semantic quality only.

Use only supplied source_input, frozen_expectations, applicable_dimensions, and actual_understanding_output.
Do not score tool use, planner execution, extraction fields, draft style, runtime delivery, or outside facts.

Score semantic equivalence, not lexical overlap. must means required meaning; must_not means forbidden meaning.
For each applicable dimension, inspect the whole Understanding output; required meaning may appear in any field.
Do not fail solely because a title is generic when the required meaning is explicit elsewhere.
Applicable dimensions may also cover claims the output actually asserted, such as gaps or risks.
Do not penalize a missing dimension when it is not applicable.

Verdicts:
PASS = meaning captured; no material omission.
BORDERLINE = mostly correct but incomplete, imprecise, inconsistent, or mildly misleading.
FAIL = material misunderstanding, omission, unsupported invention, wrong business meaning, or unsafe recommendation.
Use BORDERLINE, not FAIL, when required meaning is present but weak or only in a secondary field.
Use BORDERLINE/FAIL for asserted gaps, risks, contradictions, or next steps that could mislead an operator.
Boolean unsupported=true is provenance metadata; evaluate the visible claim. A provenance flag alone cannot make a supported claim FAIL.

Return one JSON object, no markdown, no chain-of-thought:
case_id, dimensions, overall_verdict, unsafe_misinterpretation.
dimensions MUST be a JSON object keyed by exact dimension names, not an array.
Example shape: {"dimensions":{"essence":{"applicable":true,"verdict":"PASS","reason_code":"...","evidence":"..."}}}
Each dimension object: applicable, verdict, reason_code, evidence. Evidence must be short.
overall_verdict: CLEAR_PASS if all applicable dimensions PASS; BORDERLINE if no FAIL and any BORDERLINE; CLEAR_FAIL if any FAIL or unsafe_misinterpretation.
"""


class JudgeDimension(BaseModel):
    applicable: bool
    verdict: str = Field(pattern="^(PASS|BORDERLINE|FAIL)$")
    reason_code: str
    evidence: str


class UnderstandingJudgeResult(BaseModel):
    case_id: str
    dimensions: dict[str, JudgeDimension]
    overall_verdict: str = Field(pattern="^(CLEAR_PASS|BORDERLINE|CLEAR_FAIL)$")
    unsafe_misinterpretation: bool = False


@dataclass(frozen=True)
class JudgeConfig:
    provider: str = JUDGE_PROVIDER
    model: str = JUDGE_MODEL
    endpoint: str = JUDGE_ENDPOINT
    anthropic_version: str = JUDGE_ANTHROPIC_VERSION
    temperature: float = JUDGE_TEMPERATURE
    max_tokens: int = JUDGE_MAX_TOKENS
    timeout_sec: float = JUDGE_TIMEOUT_SEC
    max_retries: int = JUDGE_MAX_RETRIES
    fallback: str = "none"
    output_schema_version: str = "understanding-judge-output.v1"
    prompt_version: str = JUDGE_CONTRACT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint,
            "anthropic_version": self.anthropic_version,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout_sec": self.timeout_sec,
            "max_retries": self.max_retries,
            "fallback": self.fallback,
            "output_schema_version": self.output_schema_version,
            "prompt_version": self.prompt_version,
        }


def select_judge_config() -> JudgeConfig:
    """Select one configured independent judge provider with no fallback."""

    if _secret_value("ANTHROPIC_API_KEY"):
        return JudgeConfig()
    openai_key = _secret_value("AGENT_OPENAI_API_KEY") or _secret_value("OPENAI_API_KEY")
    openai_base = _config_value("AGENT_OPENAI_BASE_URL") or OPENAI_NATIVE_ENDPOINT
    if openai_key and "api.openai.com" in openai_base and not openai_key.startswith("sk-or-"):
        model = _normalize_openai_model(
            _config_value("JUDGE_OPENAI_MODEL") or _config_value("AGENT_MODEL") or OPENAI_NATIVE_MODEL
        )
        return JudgeConfig(
            provider=OPENAI_NATIVE_PROVIDER,
            model=model,
            endpoint=openai_base.rstrip("/"),
            anthropic_version="not_applicable",
        )
    if _groq_key_pool():
        return JudgeConfig(
            provider=GROQ_PROVIDER,
            model=_config_value("JUDGE_GROQ_MODEL") or GROQ_MODEL,
            endpoint=_config_value("AGENT_GROQ_BASE_URL") or GROQ_ENDPOINT,
            anthropic_version="not_applicable",
        )
    openrouter_key = (
        _secret_value("OPENROUTER_API_KEY")
        or _secret_value("OPENAI_COMPAT_API_KEY")
        or _secret_value("AGENT_OPENAI_API_KEY")
        or _secret_value("AGENT_OPENAI_NATIVE_API_KEY")
    )
    openrouter_base = _config_value("OPENROUTER_BASE_URL") or _config_value("OPENAI_COMPAT_BASE_URL") or OPENROUTER_ENDPOINT
    if openrouter_key and ("openrouter.ai" in openrouter_base or openrouter_key.startswith("sk-or-")):
        model = _config_value("JUDGE_OPENROUTER_MODEL") or _config_value("OPENAI_COMPAT_MODEL") or _config_value("AGENT_MODEL") or OPENROUTER_MODEL
        if not model.startswith("openai/"):
            model = "openai/" + _normalize_openai_model(model)
        return JudgeConfig(
            provider=OPENROUTER_PROVIDER,
            model=model,
            endpoint=OPENROUTER_ENDPOINT,
            anthropic_version="not_applicable",
        )
    return JudgeConfig()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def output_schema() -> dict[str, Any]:
    dimension_schema = {
        "type": "object",
        "properties": {
            "applicable": {"type": "boolean"},
            "verdict": {"type": "string", "enum": ["PASS", "BORDERLINE", "FAIL"]},
            "reason_code": {"type": "string"},
            "evidence": {"type": "string"},
        },
        "required": ["applicable", "verdict", "reason_code", "evidence"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "case_id": {"type": "string"},
            "dimensions": {
                "type": "object",
                "properties": {name: dimension_schema for name in DIMENSIONS},
                "required": list(DIMENSIONS),
                "additionalProperties": False,
            },
            "overall_verdict": {"type": "string", "enum": ["CLEAR_PASS", "BORDERLINE", "CLEAR_FAIL"]},
            "unsafe_misinterpretation": {"type": "boolean"},
        },
        "required": ["case_id", "dimensions", "overall_verdict", "unsafe_misinterpretation"],
        "additionalProperties": False,
    }


def judge_contract(corpus: dict[str, Any] | None = None) -> dict[str, Any]:
    rubric_payload = _rubric_payload(corpus or {})
    config = select_judge_config().as_dict()
    return {
        "contract_version": JUDGE_CONTRACT_VERSION,
        "scope": "understanding_semantic_quality_only",
        "semantic_dimensions": list(DIMENSIONS),
        "excluded_dimensions": [
            "extraction_factual_correctness",
            "structured_field_equality",
            "planner_action_correctness",
            "safety_escalation_gates",
            "tool_runtime_failures",
            "draft_quality_unless_separately_contracted",
        ],
        "verdict_semantics": {
            "PASS": "Meaning captured correctly; no material omission for this dimension.",
            "BORDERLINE": "Core meaning substantially correct, but incomplete, imprecise, or partially misleading without being materially wrong.",
            "FAIL": "Material misunderstanding, material omission, unsupported invention, wrong business meaning, or unsafe recommendation.",
        },
        "aggregation": {
            "CLEAR_PASS": "No applicable dimension FAIL, no unsafe_misinterpretation, and all applicable dimensions PASS.",
            "BORDERLINE": "No applicable dimension FAIL and at least one applicable dimension BORDERLINE.",
            "CLEAR_FAIL": "Any applicable dimension FAIL or unsafe_misinterpretation=true.",
        },
        "failure_policy": {
            "provider_unavailable": "JUDGE_UNAVAILABLE",
            "timeout": "JUDGE_ERROR",
            "parse_failure": "JUDGE_ERROR",
            "schema_violation": "JUDGE_ERROR",
            "no_fallback": True,
            "judge_errors_are_not_capability_failures": True,
        },
        "config": config,
        "prompt_sha256": sha256_text(PROMPT_FINAL),
        "schema_sha256": canonical_json_sha256(output_schema()),
        "config_sha256": canonical_json_sha256(config),
        "rubric_sha256": canonical_json_sha256(rubric_payload),
    }


def build_judge_input(case: dict[str, Any], case_output: dict[str, Any]) -> dict[str, Any]:
    ground_truth = case.get("ground_truth") if isinstance(case.get("ground_truth"), dict) else {}
    understanding_truth = ground_truth.get("understanding") if isinstance(ground_truth.get("understanding"), dict) else {}
    actual_understanding = _compact_understanding_output(
        case_output.get("understanding") or case_output.get("understanding_output") or {}
    )
    return {
        "case_id": str(case.get("id") or case_output.get("id") or case_output.get("case_id") or ""),
        "source_input": case.get("input") or {},
        "prior_context": case.get("prior_context") or {},
        "frozen_expectations": {
            "must": list(understanding_truth.get("must") or []),
            "must_not": list(understanding_truth.get("must_not") or []),
            "dimensions": understanding_truth.get("dimensions") if isinstance(understanding_truth.get("dimensions"), dict) else {},
        },
        "applicable_dimensions": infer_applicable_dimensions(case, actual_understanding),
        "actual_understanding_output": actual_understanding,
    }


def infer_applicable_dimensions(case: dict[str, Any], actual_understanding: dict[str, Any] | None = None) -> list[str]:
    ground_truth = case.get("ground_truth") if isinstance(case.get("ground_truth"), dict) else {}
    understanding = ground_truth.get("understanding") if isinstance(ground_truth.get("understanding"), dict) else {}
    if isinstance(understanding.get("dimensions"), dict) and understanding["dimensions"]:
        return [name for name in DIMENSIONS if name in understanding["dimensions"]]
    text = _norm(" ".join(list(understanding.get("must") or []) + list(understanding.get("must_not") or [])))
    source = _norm(json.dumps(case.get("input") or {}, ensure_ascii=False) + " " + json.dumps(case.get("prior_context") or {}, ensure_ascii=False))
    dims = set()
    if any(token in text for token in ("rozpoznanie", "intencj", "lead", "wiadomosc", "spraw", "dokument", "faktur", "serwis", "akceptacj")):
        dims.add("essence")
    if any(token in text for token in ("intencj", "lead", "akceptacj", "odmow", "odroczen", "serwis", "wycene", "ofert")):
        dims.add("intent")
    if any(token in text for token in ("brak", "missing", "niepelne", "kompletn", "gotowa")):
        dims.add("gaps")
    if any(token in text for token in ("piln", "priorytet", "ryzyk", "dziecko", "temperatur", "standardowa", "niska prioryt")):
        dims.add("risks")
    if any(token in text for token in ("sprzeczn", "konflikt", "conflict")):
        dims.add("contradictions")
    if case.get("prior_context") or any(token in text + " " + source for token in ("zmienia", "wczesniej", "thread_delta", "odroczen", "akceptacj", "status", "re:")):
        dims.add("current_state_change")
    if any(token in text for token in ("next step", "kolejny krok", "nastepny krok", "rekomendowan", "zalecan", "dalsze dzialanie")):
        dims.add("recommended_next_step")
    if isinstance(actual_understanding, dict):
        if actual_understanding.get("missing_information") or actual_understanding.get("missing_critical_fields"):
            dims.add("gaps")
        if actual_understanding.get("risks"):
            dims.add("risks")
        if actual_understanding.get("contradictions"):
            dims.add("contradictions")
        if actual_understanding.get("next_best_action_recommendation"):
            dims.add("recommended_next_step")
    return [name for name in DIMENSIONS if name in dims]


def _backfill_redundant_top_level_verdict(payload: Any) -> Any:
    """`normalize_judge_result` ALWAYS recomputes `overall_verdict` deterministically from
    `dimensions` (see below: `out["overall_verdict"] = overall`) -- the raw top-level
    `overall_verdict`/`unsafe_misinterpretation` from the LLM are discarded and overwritten
    regardless of what they contain. Occasionally the provider omits these vestigial fields
    while still returning complete, usable per-dimension verdicts (observed:
    ValidationError "overall_verdict Field required" on an otherwise well-formed payload).
    Backfilling a placeholder here only relaxes the SCHEMA acceptance of a field whose
    value is never actually used -- it cannot change any judged verdict, since the
    aggregation always runs against `dimensions` after this. Never invents dimensions.

    Guarded against degrading into a false pass: requires `dimensions` to be non-empty AND
    contain at least one recognized dimension name with an actual `verdict` value -- an
    empty `{}` (a genuinely broken/truncated response) does NOT qualify and is left to fail
    real Pydantic validation -> JUDGE_ERROR, exactly as before this fix. Without this guard,
    an empty dimensions dict would backfill cleanly, then normalize_judge_result would fill
    all DIMENSIONS with the synthetic non-applicable default and aggregate to a false
    CLEAR_PASS (adversarial review finding, confirmed)."""
    if not isinstance(payload, dict):
        return payload
    dims = payload.get("dimensions")
    if not isinstance(dims, dict) or not dims:
        return payload
    has_real_dimension = any(
        name in DIMENSIONS and isinstance(item, dict) and "verdict" in item
        for name, item in dims.items()
    )
    if not has_real_dimension:
        return payload
    out = dict(payload)
    out.setdefault("overall_verdict", "CLEAR_FAIL")
    out.setdefault("unsafe_misinterpretation", False)
    return out


def run_judge(
    judge_input: dict[str, Any],
    *,
    invoke: Callable[[str, str, str], dict[str, Any]] | None = None,
    config: JudgeConfig | None = None,
) -> dict[str, Any]:
    case_id = str(judge_input.get("case_id") or "")
    user = json.dumps(judge_input, ensure_ascii=False, sort_keys=True, indent=2)
    started = time.monotonic()
    try:
        payload = invoke(PROMPT_FINAL, user, case_id) if invoke else _invoke_provider(PROMPT_FINAL, user, case_id, config or select_judge_config())
        payload = _backfill_redundant_top_level_verdict(payload)
        result = UnderstandingJudgeResult.model_validate(payload)
    except (TopInstalLLMError, ValidationError, ValueError, TypeError) as exc:
        status = "JUDGE_UNAVAILABLE" if "API_KEY" in str(exc) or "not configured" in str(exc) else "JUDGE_ERROR"
        return {
            "case_id": case_id,
            "status": status,
            "error_type": type(exc).__name__,
            "error": _preview(_sanitize_error(str(exc)), 300),
            "latency_ms": int(round((time.monotonic() - started) * 1000)),
        }
    normalized = normalize_judge_result(result.model_dump(mode="python"))
    normalized["status"] = "SCORED"
    normalized["latency_ms"] = int(round((time.monotonic() - started) * 1000))
    return normalized


def normalize_judge_result(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    dimensions = out.get("dimensions") if isinstance(out.get("dimensions"), dict) else {}
    normalized_dims: dict[str, Any] = {}
    for name in DIMENSIONS:
        item = dimensions.get(name)
        if not isinstance(item, dict):
            item = {"applicable": False, "verdict": "PASS", "reason_code": "not_applicable", "evidence": ""}
        verdict = str(item.get("verdict") or "FAIL").strip().upper()
        if verdict not in VERDICT_SCORE:
            verdict = "FAIL"
        normalized_dims[name] = {
            "applicable": bool(item.get("applicable")),
            "verdict": verdict,
            "reason_code": str(item.get("reason_code") or "").strip()[:80],
            "evidence": str(item.get("evidence") or "").strip()[:300],
            "score": VERDICT_SCORE[verdict] if bool(item.get("applicable")) else 1.0,
            "status": "passed" if verdict == "PASS" or not bool(item.get("applicable")) else "failed",
            "scorer_type": "llm_judged",
        }
    out["dimensions"] = normalized_dims
    applicable = [item for item in normalized_dims.values() if item.get("applicable")]
    if out.get("unsafe_misinterpretation") or any(item.get("verdict") == "FAIL" for item in applicable):
        overall = "CLEAR_FAIL"
    elif any(item.get("verdict") == "BORDERLINE" for item in applicable):
        overall = "BORDERLINE"
    else:
        overall = "CLEAR_PASS"
    out["overall_verdict"] = overall
    out["score"] = _overall_score(overall)
    out["passed"] = overall == "CLEAR_PASS"
    out["needs_llm_judge"] = False
    out["scorer_type"] = "llm_judged"
    return out


def load_calibration_labels(path: str | Path) -> dict[str, str]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rows = data.get("rows") if isinstance(data, dict) else []
    return {str(row.get("case_id")): _normalize_label(row.get("human")) for row in rows if isinstance(row, dict)}


def build_calibration_manifest(corpus: dict[str, Any], results: dict[str, Any], labels: dict[str, str]) -> dict[str, Any]:
    cases = {str(case.get("id")): case for case in corpus.get("cases", [])}
    outputs = {str(case.get("id") or case.get("case_id")): case for case in results.get("cases", [])}
    rows = []
    for case_id in CALIBRATION_CASE_IDS:
        if case_id in cases and case_id in outputs:
            row = build_judge_input(cases[case_id], outputs[case_id])
            row["human_label_available"] = case_id in labels
            rows.append(row)
    return {
        "contract_version": JUDGE_CONTRACT_VERSION,
        "case_ids": list(CALIBRATION_CASE_IDS),
        "cases": rows,
    }


def build_run_judge_manifest(corpus: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    cases = {str(case.get("id")): case for case in corpus.get("cases", [])}
    outputs = {str(case.get("id") or case.get("case_id")): case for case in results.get("cases", [])}
    rows = []
    for case_id, case in cases.items():
        ground_truth = case.get("ground_truth") if isinstance(case.get("ground_truth"), dict) else {}
        if not isinstance(ground_truth.get("understanding"), dict):
            continue
        if case_id not in outputs:
            continue
        rows.append(build_judge_input(case, outputs[case_id]))
    return {
        "contract_version": JUDGE_CONTRACT_VERSION,
        "case_ids": [row["case_id"] for row in rows],
        "cases": rows,
    }


def compare_with_human(judge_rows: list[dict[str, Any]], labels: dict[str, str]) -> dict[str, Any]:
    rows = []
    exact = 0
    binary = 0
    major = 0
    judge_verdicts = []
    for row in judge_rows:
        case_id = str(row.get("case_id") or "")
        human = str(labels.get(case_id) or "")
        judge = str(row.get("overall_verdict") or row.get("status") or "")
        judge_verdicts.append(judge)
        is_exact = human == judge
        is_binary = _binary_label(human) == _binary_label(judge)
        is_major = {human, judge} == {"CLEAR_PASS", "CLEAR_FAIL"}
        exact += int(is_exact)
        binary += int(is_binary)
        major += int(is_major)
        rows.append(
            {
                "case_id": case_id,
                "human_verdict": human,
                "judge_verdict": judge,
                "agreement": is_exact,
                "binary_agreement": is_binary,
                "major_disagreement": is_major,
            }
        )
    total = len(rows)
    all_pass_bias = total > 0 and all(v == "CLEAR_PASS" for v in judge_verdicts)
    all_fail_bias = total > 0 and all(v == "CLEAR_FAIL" for v in judge_verdicts)
    return {
        "rows": rows,
        "metrics": {
            "sample_size": total,
            "exact_3class_agreement": exact,
            "binary_agreement": binary,
            "major_disagreement": major,
            "all_pass_bias": all_pass_bias,
            "all_fail_bias": all_fail_bias,
            "gate_pass": binary >= 8 and major <= 1 and not all_pass_bias and not all_fail_bias,
        },
    }


def compare_runs(run1: list[dict[str, Any]], run2: list[dict[str, Any]]) -> dict[str, Any]:
    by2 = {str(row.get("case_id")): row for row in run2}
    rows = []
    exact = 0
    critical = 0
    for row1 in run1:
        case_id = str(row1.get("case_id") or "")
        row2 = by2.get(case_id) or {}
        v1 = str(row1.get("overall_verdict") or row1.get("status") or "")
        v2 = str(row2.get("overall_verdict") or row2.get("status") or "")
        is_exact = v1 == v2
        is_critical = {v1, v2} == {"CLEAR_PASS", "CLEAR_FAIL"}
        exact += int(is_exact)
        critical += int(is_critical)
        rows.append({"case_id": case_id, "run1": v1, "run2": v2, "exact": is_exact, "critical_flip": is_critical})
    return {
        "rows": rows,
        "metrics": {
            "sample_size": len(rows),
            "exact_agreement": exact,
            "critical_flips": critical,
            "stable": critical == 0,
        },
    }


def write_freeze_artifacts(out_dir: Path, corpus: dict[str, Any], manifest: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    contract = judge_contract(corpus)
    schema = output_schema()
    config = select_judge_config().as_dict()
    hashes = {
        "JUDGE_CONTRACT_VERSION": JUDGE_CONTRACT_VERSION,
        "JUDGE_PROMPT_HASH": sha256_text(PROMPT_FINAL),
        "JUDGE_SCHEMA_HASH": canonical_json_sha256(schema),
        "JUDGE_CONFIG_HASH": canonical_json_sha256(config),
        "RUBRIC_HASH": contract["rubric_sha256"],
    }
    _write_text(out_dir / "judge-requirements.md", _requirements_text())
    _write_json(out_dir / "judge-contract.json", contract)
    _write_text(out_dir / "judge-prompt-v0.txt", PROMPT_FINAL)
    _write_text(out_dir / "judge-prompt-final.txt", PROMPT_FINAL)
    _write_json(out_dir / "judge-output-schema.json", schema)
    _write_json(out_dir / "judge-config.json", config)
    _write_json(out_dir / "judge-hashes.json", hashes)
    _write_json(out_dir / "calibration-input-manifest.json", manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze and run the Understanding semantic judge contract.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    freeze = sub.add_parser("freeze")
    freeze.add_argument("--corpus", required=True)
    freeze.add_argument("--run-results", required=True)
    freeze.add_argument("--human-labels", required=True)
    freeze.add_argument("--out-dir", required=True)

    calibrate = sub.add_parser("calibrate")
    calibrate.add_argument("--manifest", required=True)
    calibrate.add_argument("--out", required=True)

    score_run = sub.add_parser("score-run")
    score_run.add_argument("--corpus", required=True)
    score_run.add_argument("--run-results", required=True)
    score_run.add_argument("--out", required=True)

    compare = sub.add_parser("compare")
    compare.add_argument("--run1", required=True)
    compare.add_argument("--run2")
    compare.add_argument("--human-labels", required=True)
    compare.add_argument("--out", required=True)

    args = parser.parse_args(argv)
    if args.cmd == "freeze":
        corpus = _read_json(args.corpus)
        results = _read_json(args.run_results)
        labels = load_calibration_labels(args.human_labels)
        manifest = build_calibration_manifest(corpus, results, labels)
        write_freeze_artifacts(Path(args.out_dir), corpus, manifest)
        return 0
    if args.cmd == "calibrate":
        manifest = _read_json(args.manifest)
        config = select_judge_config()
        rows = []
        cases = list(manifest.get("cases") or [])
        for index, row in enumerate(cases):
            rows.append(run_judge(row, config=config))
            if index < len(cases) - 1:
                time.sleep(_inter_case_delay(config))
        _write_json(
            args.out,
            {"contract_version": JUDGE_CONTRACT_VERSION, "judge_config": config.as_dict(), "cases": rows},
        )
        return 0
    if args.cmd == "score-run":
        corpus = _read_json(args.corpus)
        results = _read_json(args.run_results)
        manifest = build_run_judge_manifest(corpus, results)
        config = select_judge_config()
        out_path = Path(args.out)
        existing_by_case: dict[str, dict[str, Any]] = {}
        if out_path.is_file():
            existing = _read_json(out_path)
            existing_rows = existing.get("cases") if isinstance(existing, dict) else []
            existing_by_case = {
                str(row.get("case_id")): row
                for row in existing_rows
                if isinstance(row, dict) and row.get("status") == "SCORED"
            }
        rows = []
        cases = list(manifest.get("cases") or [])
        for index, row in enumerate(cases):
            case_id = str(row.get("case_id") or "")
            scored = existing_by_case.get(case_id)
            if scored:
                rows.append(scored)
            else:
                rows.append(run_judge(row, config=config))
            _write_json(
                out_path,
                {
                    "contract_version": JUDGE_CONTRACT_VERSION,
                    "judge_config": config.as_dict(),
                    "case_count": len(cases),
                    "completed_count": len(rows),
                    "cases": rows,
                },
            )
            print(f"judged {len(rows)}/{len(cases)} {case_id} status={rows[-1].get('status')}", flush=True)
            if index < len(cases) - 1:
                time.sleep(_inter_case_delay(config))
        _write_json(
            out_path,
            {
                "contract_version": JUDGE_CONTRACT_VERSION,
                "judge_config": config.as_dict(),
                "case_count": len(rows),
                "completed_count": len(rows),
                "cases": rows,
            },
        )
        return 0
    if args.cmd == "compare":
        run1 = _read_json(args.run1).get("cases") or []
        labels = load_calibration_labels(args.human_labels)
        payload = {"human_vs_judge": compare_with_human(run1, labels)}
        if args.run2:
            run2 = _read_json(args.run2).get("cases") or []
            payload["stability"] = compare_runs(run1, run2)
        _write_json(args.out, payload)
        return 0
    raise AssertionError(args.cmd)


def _invoke_provider(system: str, user: str, case_id: str, config: JudgeConfig) -> dict[str, Any]:
    if config.provider in {OPENAI_NATIVE_PROVIDER, OPENROUTER_PROVIDER, GROQ_PROVIDER}:
        return _openai_compatible_invoke(system, user, case_id, config)
    return _anthropic_invoke(system, user, case_id, config)


def _anthropic_invoke(system: str, user: str, case_id: str, config: JudgeConfig) -> dict[str, Any]:
    api_key = _secret_value("ANTHROPIC_API_KEY")
    if not api_key:
        raise TopInstalLLMError("ANTHROPIC_API_KEY is not configured.")
    client = TopInstalLLMClient(
        api_key=api_key,
        model=config.model,
        base_url=config.endpoint,
        timeout_sec=config.timeout_sec,
        max_retries=config.max_retries,
        temperature=config.temperature,
    )
    return client.complete_json(system=system, user=user, case_id=case_id, model=config.model)


def _openai_compatible_invoke(system: str, user: str, case_id: str, config: JudgeConfig) -> dict[str, Any]:
    import requests

    if config.provider == OPENAI_NATIVE_PROVIDER:
        api_keys = parse_api_key_pool(
            _secret_value("AGENT_OPENAI_API_KEY"),
            _secret_value("OPENAI_API_KEY"),
        )
    elif config.provider == GROQ_PROVIDER:
        api_keys = _groq_key_pool()
    else:
        api_keys = parse_api_key_pool(
            _secret_value("OPENROUTER_API_KEY"),
            _secret_value("OPENAI_COMPAT_API_KEY"),
            _secret_value("AGENT_OPENAI_API_KEY"),
            _secret_value("AGENT_OPENAI_NATIVE_API_KEY"),
        )
    if not api_keys:
        raise TopInstalLLMError("OpenAI-compatible judge API key is not configured.")
    url = config.endpoint.rstrip("/") + "/chat/completions"
    response_format = {"type": "json_object"}
    user_content = (
        f"{user.strip()}\n\n"
        "Return JSON with exactly these top-level keys: case_id, dimensions, overall_verdict, unsafe_misinterpretation. "
        "dimensions must contain verdict objects for applicable dimensions; verdicts are PASS, BORDERLINE, or FAIL."
    )
    body = {
        "model": config.model,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "response_format": response_format,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    }
    response = None
    last_status = 0
    last_text = "missing response"
    for key_index, api_key in enumerate(api_keys):
        auth_rejected = False
        for attempt in range(1, max(1, int(config.max_retries)) + 1):
            response = requests.post(
                url,
                headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
                json=body,
                timeout=config.timeout_sec,
            )
            last_status = int(response.status_code)
            last_text = response.text
            if response.status_code == 429 and attempt < int(config.max_retries):
                time.sleep(_retry_after_seconds(response.text))
                continue
            if response.status_code in {401, 403}:
                auth_rejected = True
                break
            break
        if response is not None and response.status_code < 400:
            break
        if auth_rejected and key_index + 1 < len(api_keys):
            continue
        break
    if response is None or response.status_code >= 400:
        status_code = response.status_code if response is not None else last_status
        text = response.text if response is not None else last_text
        raise TopInstalLLMError(
            f"OpenAI judge HTTP {status_code}: {_sanitize_error(text[:500])}",
            details={"status_code": status_code, "case_id": case_id, "key_pool_size": len(api_keys)},
        )
    data = response.json()
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    if not choices:
        raise TopInstalLLMError("OpenAI judge response missing choices.")
    content = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not content:
        raise TopInstalLLMError("OpenAI judge response missing content.")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise TopInstalLLMError(f"OpenAI judge response was not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TopInstalLLMError("OpenAI judge JSON root must be an object.")
    return payload


def _env_file_candidates() -> list[Path]:
    """Match runtime env priority used by local-vps / load_settings.

    GMAIL_AGENT_ENV_FILE (mounted .env.local-vps) must win over tools/gmail_audit/.env,
    otherwise the judge reads a stale single GROQ_API_KEY while the app uses the pool.
    """

    tool_dir = Path(__file__).resolve().parent
    repo_root = tool_dir.parent.parent
    candidates: list[Path] = []
    override = (os.getenv("GMAIL_AGENT_ENV_FILE") or "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    candidates.extend(
        [
            # Prefer local-vps app env before tools/.env so host-side judge
            # matches docker runtime (compose mounts this as GMAIL_AGENT_ENV_FILE).
            repo_root / ".env.local-vps",
            tool_dir / ".env",
            repo_root / ".env",
        ]
    )
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(path)
    return ordered


def _secret_value(key: str) -> str:
    env_val = (os.getenv(key) or "").strip()
    if env_val:
        return env_val
    for path in _env_file_candidates():
        value = _dotenv_value(path, key).strip()
        if value:
            return value
    return ""


def _config_value(key: str) -> str:
    return _secret_value(key)


def _groq_key_pool() -> tuple[str, ...]:
    """Deduped Groq key pool: GROQ_API_KEYS + GROQ_API_KEY + AGENT_GROQ_API_KEY."""

    return parse_api_key_pool(
        _secret_value("GROQ_API_KEYS"),
        _secret_value("GROQ_API_KEY"),
        _secret_value("AGENT_GROQ_API_KEY"),
    )


def _normalize_openai_model(model: str) -> str:
    value = str(model or "").strip() or OPENAI_NATIVE_MODEL
    if value.startswith("openai/"):
        return value.split("/", 1)[1]
    return value


def _sanitize_error(text: str) -> str:
    text = re.sub(r"sk-[A-Za-z0-9_\-]+", "[REDACTED_API_KEY]", text)
    text = re.sub(r"sk-or-v1[A-Za-z0-9_\-]+", "[REDACTED_API_KEY]", text)
    text = re.sub(r"gsk_[A-Za-z0-9_\-]+", "[REDACTED_API_KEY]", text)
    return text


def _retry_after_seconds(text: str) -> float:
    match = re.search(r"try again in ([0-9]+(?:\.[0-9]+)?)s", text, flags=re.IGNORECASE)
    if not match:
        return 30.0
    return min(70.0, max(1.0, float(match.group(1)) + 1.0))


def _inter_case_delay(config: JudgeConfig) -> float:
    if config.provider == GROQ_PROVIDER and config.model == "openai/gpt-oss-120b":
        return 12.0
    if config.provider == GROQ_PROVIDER:
        return 1.0
    return 0.0


def _dotenv_value(path: Path, key: str) -> str:
    if not path.exists():
        return ""
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix) :].strip().strip('"').strip("'")
        return value
    return ""


def _rubric_payload(corpus: dict[str, Any]) -> dict[str, Any]:
    return {
        "understanding_ground_truth": [
            {
                "case_id": case.get("id"),
                "understanding": (case.get("ground_truth") or {}).get("understanding"),
            }
            for case in corpus.get("cases", [])
            if isinstance((case.get("ground_truth") or {}).get("understanding"), dict)
        ]
    }


_UNDERSTANDING_ALIAS_PAIRS = (
    ("summary_pl", "situation_summary_pl"),
    ("customer_intent_pl", "current_customer_intent"),
)


def _compact_understanding_output(actual: Any) -> dict[str, Any]:
    if not isinstance(actual, dict):
        return {}
    resolved = dict(actual)
    diagnostics: list[dict[str, str]] = []
    for canonical_field, alias_field in _UNDERSTANDING_ALIAS_PAIRS:
        canonical_value = resolved.get(canonical_field)
        alias_value = resolved.get(alias_field)
        canonical_present = canonical_value not in (None, "", [], {})
        alias_present = alias_value not in (None, "", [], {})
        if not canonical_present and alias_present:
            resolved[canonical_field] = alias_value
            diagnostics.append(
                {
                    "code": "alias_promoted",
                    "canonical_field": canonical_field,
                    "alias_field": alias_field,
                    "precedence": "alias_when_canonical_absent",
                }
            )
        elif canonical_present and alias_present and canonical_json_sha256(canonical_value) != canonical_json_sha256(alias_value):
            diagnostics.append(
                {
                    "code": "divergent_alias",
                    "canonical_field": canonical_field,
                    "alias_field": alias_field,
                    "precedence": "canonical",
                }
            )
    # STRUCTURED-INPUT-AND-CAPABILITY-BASELINE-CLOSEOUT-01 — measurement-fidelity fix, not
    # a judge-contract/prompt change. `situation_summary_pl` and `current_customer_intent`
    # are expected to mirror `summary_pl`/`customer_intent_pl` respectively. Equal aliases
    # are omitted, but divergence is now surfaced above instead of being silently lost.
    # Keeping duplicate values wasted ~40% of the truncation budget. Measured blast radius:
    # with the old key
    # order+budget, thread_delta (which carries prior_known_state_pl and
    # operator_visible_delta_summary -- the ONLY place continuity/state-change evidence
    # survives compaction) was silently truncated away in 23/23 (100%) of cases that had
    # real thread_delta content. Moving it earlier and removing the duplicate keys ensures
    # the judge actually sees the continuity evidence it is asked to score against.
    keys = (
        "summary_pl",
        "operator_explanation",
        "customer_intent_pl",
        "thread_delta",
        "missing_information",
        "completeness_gaps",
        "missing_critical_fields",
        "risks",
        "conflicting_facts",
        "unsupported_claims",
        "next_best_action_recommendation",
        "facts_explicit",
        "facts_extracted",
        "facts_inferred",
        "facts_disputed",
    )
    compact: dict[str, Any] = {}
    if diagnostics:
        compact["measurement_diagnostics"] = diagnostics
    compact.update(
        {
            key: resolved.get(key)
            for key in keys
            if key in resolved and resolved.get(key) not in (None, "", [], {})
        }
    )
    return _truncate_payload(compact, limit=1800)


def _truncate_payload(value: Any, *, limit: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[: limit - 3] + "..."
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        remaining = limit
        for key, item in value.items():
            if remaining <= 0:
                break
            truncated = _truncate_payload(item, limit=min(remaining, 300))
            out[key] = truncated
            remaining -= len(json.dumps(truncated, ensure_ascii=False, default=str))
        return out
    if isinstance(value, list):
        out = []
        remaining = limit
        for item in value[:4]:
            if remaining <= 0:
                break
            truncated = _truncate_payload(item, limit=min(remaining, 180))
            out.append(truncated)
            remaining -= len(json.dumps(truncated, ensure_ascii=False, default=str))
        return out
    return value


def _overall_score(overall: str) -> float:
    if overall == "CLEAR_PASS":
        return 1.0
    if overall == "BORDERLINE":
        return 0.65
    return 0.0


def _binary_label(value: str) -> str:
    return "CLEAR_PASS" if _normalize_label(value) == "CLEAR_PASS" else "NOT_CLEAR_PASS"


def _normalize_label(value: Any) -> str:
    return str(value or "").strip().upper().replace(" ", "_")


def _requirements_text() -> str:
    return """# Understanding Semantic Judge Requirements

Scope: Understanding semantic quality only.

Excluded: extraction field correctness, planner/action correctness, safety gates,
tool/runtime failures, and draft quality unless separately contracted.

Judge: prefer Anthropic claude-sonnet-4-20250514 when configured; otherwise
use native OpenAI gpt-4o-mini through api.openai.com. The selected provider is
frozen in judge-config.json. Temperature 0, no fallback.

Judge failures are JUDGE_ERROR/JUDGE_UNAVAILABLE and are never capability
failures.
"""


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("_", " ").split())


def _preview(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write_json(path: str | Path, payload: Any) -> None:
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_text(path: str | Path, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
