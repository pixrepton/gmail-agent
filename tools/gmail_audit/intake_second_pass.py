"""Conditional second-pass LLM: review-first supplement only (semantic alignment)."""

from __future__ import annotations
from log_config import get_logger

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from central_llm_stage import resolve_case_id, resolve_engagement_id, run_central_structured_stage
from intake_policy import REVIEW_FLAGS
from llm_contracts.intake_second_pass import IntakeSecondPassResult
from redaction import sanitize_for_storage

logger = get_logger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "intake_second_pass_v1.json"


def load_second_pass_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _env_second_pass_enabled() -> bool:
    return os.getenv("INTAKE_SECOND_PASS_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


HIGH_VALUE_ATTACHMENT_TYPES = frozenset(
    {
        "technical_pdf",
        "project_document",
        "delivery_confirmation",
        "invoice",
        "technical_photo",
    }
)


def attachment_intel_triggers_second_pass(att_intel: dict[str, Any]) -> bool:
    """High-value document with weak extraction or scan-without-text."""
    for rec in att_intel.get("attachments") or []:
        if not isinstance(rec, dict):
            continue
        bt = str(rec.get("attachment_business_type") or "")
        if bt not in HIGH_VALUE_ATTACHMENT_TYPES:
            continue
        conf = float(rec.get("extraction_confidence") or 0.0)
        method = str(rec.get("extraction_method") or "")
        scan = method == "pdf_no_text_layer"
        if scan or conf < 0.45:
            return True
    return False


def should_run_intake_second_pass(
    *,
    preclassification: dict[str, Any],
    first_response_json: dict[str, Any] | None,
    cached_attachment_intelligence: dict[str, Any] | None,
) -> bool:
    if not _env_second_pass_enabled():
        return False
    if str((preclassification or {}).get("lane") or "") != "intake_llm":
        return False
    first = first_response_json if isinstance(first_response_json, dict) else {}
    if not first or str(first.get("schema_version") or "") != "1.0":
        return False
    att_intel = cached_attachment_intelligence if isinstance(cached_attachment_intelligence, dict) else {}
    if attachment_intel_triggers_second_pass(att_intel):
        return True
    cf = str(((first.get("case_assessment") or {}).get("case_family")) or "")
    conf = first.get("confidence") if isinstance(first.get("confidence"), dict) else {}
    dc = float(conf.get("decision_confidence") or 1.0)
    ec = float(conf.get("extraction_confidence") or 1.0)
    if cf == "unknown" and dc < 0.55:
        return True
    if dc < 0.42:
        return True
    if ec < 0.38 and cf == "unknown":
        return True
    return False


def merge_intake_second_pass_supplement(
    base_intake: dict[str, Any],
    supplement: dict[str, Any],
) -> dict[str, Any]:
    """Merge review-first supplement without replacing first-pass decision objects."""
    out = deepcopy(base_intake)
    review = out.get("review")
    if not isinstance(review, dict):
        review = {"required": False, "flags": []}
        out["review"] = review
    flags = list(review.get("flags") or [])
    valid = set(REVIEW_FLAGS)
    for raw in supplement.get("additional_review_flags") or []:
        f = str(raw).strip()
        if f in valid and f not in flags:
            flags.append(f)
    review["flags"] = flags
    if supplement.get("suggested_review_escalation"):
        review["required"] = True
    extra = str(supplement.get("supplement_notes_pl") or "").strip()
    ev = str(supplement.get("evidence_assessment_pl") or "").strip()
    tail = " ".join(part for part in (extra, ev) if part).strip()
    if tail:
        base_reason = str(out.get("reason") or "").strip()
        suffix = "[second_pass] " + tail
        out["reason"] = (base_reason + "\n" + suffix).strip() if base_reason else suffix
    return out


def _compact_second_pass_prompt_input(
    first_json: dict[str, Any],
    att_intel: dict[str, Any],
) -> dict[str, Any]:
    slim_att: list[dict[str, Any]] = []
    for rec in (att_intel.get("attachments") or [])[:4]:
        if not isinstance(rec, dict):
            continue
        slim_att.append(
            {
                "file_name": str(rec.get("file_name") or "")[:120],
                "attachment_business_type": str(rec.get("attachment_business_type") or ""),
                "extraction_confidence": float(rec.get("extraction_confidence") or 0.0),
                "extraction_method": str(rec.get("extraction_method") or ""),
                "attachment_summary_pl": str(rec.get("attachment_summary_pl") or "")[:400],
            }
        )
    decision = first_json.get("decision") if isinstance(first_json.get("decision"), dict) else {}
    case_a = first_json.get("case_assessment") if isinstance(first_json.get("case_assessment"), dict) else {}
    conf = first_json.get("confidence") if isinstance(first_json.get("confidence"), dict) else {}
    return sanitize_for_storage(
        {
            "first_pass_summary": {
                "action": str(decision.get("action") or ""),
                "case_family": str(case_a.get("case_family") or ""),
                "decision_confidence": float(conf.get("decision_confidence") or 0.0),
                "extraction_confidence": float(conf.get("extraction_confidence") or 0.0),
            },
            "attachment_digest": {"attachments": slim_att},
            "task": (
                "Do not change decision.action or case_assessment. Only suggest review escalation and flags "
                "when attachment evidence quality is insufficient."
            ),
        }
    )


SECOND_PASS_INSTRUCTIONS = (
    "You are a conservative Gmail intake reviewer. The first-pass JSON is already decided. "
    "Output only the supplement schema. Never contradict the first pass decision fields. "
    "If document evidence is weak (scan without text, low extraction confidence) on invoices or technical PDFs, "
    "suggest review escalation and add review flags from the allowed intake vocabulary when appropriate."
)


def run_intake_second_pass_supplement(
    *,
    settings: Any,
    model: str | None,
    verbose: bool,
    first_response_json: dict[str, Any],
    cached_attachment_intelligence: dict[str, Any] | None,
    context_bundle: dict[str, Any] | None = None,
    case_link_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema = load_second_pass_schema()
    att = cached_attachment_intelligence if isinstance(cached_attachment_intelligence, dict) else {}
    prompt_input = _compact_second_pass_prompt_input(first_response_json, att)
    query_text = str(first_response_json.get("reason") or "intake second pass")
    case_id = resolve_case_id(context_bundle=context_bundle, case_link_result=case_link_result)
    engagement_id = resolve_engagement_id(
        context_bundle=context_bundle,
        case_link_result=case_link_result,
    )
    out = run_central_structured_stage(
        settings,
        stage_name="intake_second_pass",
        task_instructions=SECOND_PASS_INSTRUCTIONS,
        prompt_input=prompt_input,
        query_text=query_text,
        json_schema=schema,
        schema_name="intake_second_pass_v1",
        case_id=case_id or None,
        engagement_id=engagement_id or None,
        model=model,
        verbose=verbose,
        input_variants=None,
        output_model=IntakeSecondPassResult,
        context_bundle=context_bundle,
    )
    if out is not None and str(out.get("parse_status") or "") == "pydantic_failed":
        errors = (out.get("request_meta") or {}).get("pydantic_errors")
        logger.warning("[intake_second_pass] Pydantic ValidationError: %s", errors)
    return out
