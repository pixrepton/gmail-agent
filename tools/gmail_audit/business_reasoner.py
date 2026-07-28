"""Shadow-only business reasoning layer for Gmail Intake v2."""

from __future__ import annotations

import json
from typing import Any

from central_llm_stage import (
    resolve_case_id,
    resolve_engagement_id,
    run_central_structured_stage,
)
from config import Settings
from groq_client import GroqClientError, extract_json_candidate
from signal_extractor import build_signal_extraction_query
from intake_payload import build_business_reasoning_payload
from intake_schema import validate_business_reasoning_result
from llm_contracts.business_reasoning import BusinessReasoningResult
from log_config import get_logger
from redaction import sanitize_text
from intake_policy import HIGH_RISK_ACTION_MIN_DECISION_CONFIDENCE, BUSINESS_BLOCKED_IN_WAITING_CLIENT, BUSINESS_HIGH_RISK_ACTIONS

logger = get_logger("business_reasoner")


BUSINESS_REASONING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "business_interpretation",
        "business_area",
        "customer_state_guess",
        "recommended_next_action",
        "recommended_action_reason",
        "missing_information",
        "risks",
        "urgency",
        "operator_note",
        "confidence",
    ],
    "properties": {
        "business_interpretation": {"type": "string"},
        "business_area": {"type": "string"},
        "customer_state_guess": {"type": "string"},
        "recommended_next_action": {"type": "string"},
        "recommended_action_reason": {"type": "string"},
        "missing_information": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "urgency": {"type": "string"},
        "operator_note": {"type": "string"},
        "business_summary_short": {"type": "string"},
        "reply_recommended": {"type": "boolean"},
        "human_review_bias": {"type": "string"},
        "safety_notes": {"type": "array", "items": {"type": "string"}},
        "evidence_refs": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "conflict_refs": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "confidence": {
            "type": "object",
            "additionalProperties": False,
            "required": ["business_confidence", "action_confidence"],
            "properties": {
                "business_confidence": {"type": "number"},
                "action_confidence": {"type": "number"},
            },
        },
    },
}

BUSINESS_REASONING_INSTRUCTIONS = (
    "Zinterpretuj wynik intake dla operacji TOP-INSTAL (tylko shadow, bez wykonania). "
    "Użyj złożonego kontekstu firmy/sprawy oraz payloadu JSON. "
    "Nie wymyślaj historii, wycen ani wniosków technicznych. "
    "Elementy poparte źródłem wpisz w evidence_refs; założenia w assumptions; "
    "niesprawdzone twierdzenia w unsupported_claims. Przy słabych dowodach preferuj escalate_review. "
    "Odpowiedz wyłącznie po polsku. Wszystkie pola tekstowe — w tym business_interpretation, "
    "operator_note, business_summary_short, recommended_action_reason, missing_information, "
    "risks, assumptions i unsupported_claims — muszą być po polsku. "
    "Wartości enum (business_area, customer_state_guess, recommended_next_action, urgency, "
    "human_review_bias) pozostaw w kodach ze schematu JSON. "
    "Zwróć ścisły JSON zgodny ze schematem."
)


def _context_bundle_ref(context_bundle: dict[str, Any]) -> dict[str, Any]:
    """Lightweight pointer — full pack lives in assembled system prompt, not user JSON."""
    bundle = context_bundle if isinstance(context_bundle, dict) else {}
    pack = bundle.get("case_context_pack")
    pack_case_id = str(pack.get("case_id") or "").strip() if isinstance(pack, dict) else ""
    return {
        "case_id": str(bundle.get("case_id") or pack_case_id or "").strip(),
        "engagement_id": str(bundle.get("engagement_id") or "").strip(),
        "context_source": "assembled_system_prompt",
    }


def build_business_reasoning_prompt_input(
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
    context_bundle: dict[str, Any],
    business_context_bundle: dict[str, Any],
) -> dict[str, Any]:
    """Build the business reasoning prompt input from validated stage outputs."""
    payload = build_business_reasoning_payload(
        snapshot,
        intake_result,
        case_link_result,
        business_context_bundle,
    )
    payload["context_bundle"] = _context_bundle_ref(context_bundle)
    return payload


def _validate_recommended_action(result: dict[str, Any], case_state: str | None) -> None:
    """Sprawdza, czy rekomendowana akcja jest bezpieczna w danym stanie."""
    if not isinstance(result, dict):
        return
    action = str(result.get("recommended_next_action") or "").strip()
    if not action:
        return

    # Guard 1: Nie wysyłaj ofert/faktur do klienta, na którego czekamy
    if case_state == "waiting_client" and action in BUSINESS_BLOCKED_IN_WAITING_CLIENT:
        from exceptions import BusinessReasoningError
        raise BusinessReasoningError(
            f"LLM recommended blocked action '{action}' in state 'waiting_client'",
            context={"action": action, "state": case_state}
        )

    # Guard 2: Akcje wysokiego ryzyka wymagają podwyższonego confidence
    if action in BUSINESS_HIGH_RISK_ACTIONS:
        confidence_raw = result.get("confidence", {})
        if isinstance(confidence_raw, dict):
            biz_conf = float(confidence_raw.get("business_confidence", 0))
        else:
            biz_conf = 0.0
        if biz_conf < HIGH_RISK_ACTION_MIN_DECISION_CONFIDENCE:
            logger.warning("HIGH_RISK_ACTION_LOW_CONFIDENCE", extra={"x": {
                "action": action,
                "confidence": biz_conf,
                "threshold": HIGH_RISK_ACTION_MIN_DECISION_CONFIDENCE,
            }})


def run_business_reasoning(
    *,
    settings: Settings,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
    context_bundle: dict[str, Any],
    business_context_bundle: dict[str, Any],
    model: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Run the shadow-only business reasoner and return a validated contract."""
    prompt_input = build_business_reasoning_prompt_input(
        snapshot,
        intake_result,
        case_link_result,
        context_bundle,
        business_context_bundle,
    )
    try:
        case_id = resolve_case_id(context_bundle=context_bundle, case_link_result=case_link_result)
        engagement_id = resolve_engagement_id(
            context_bundle=context_bundle,
            case_link_result=case_link_result,
        )
        query_text = str(intake_result.get("reason") or "").strip() or build_signal_extraction_query(snapshot)
        stage_call = run_central_structured_stage(
            settings,
            stage_name="business_reasoning",
            task_instructions=BUSINESS_REASONING_INSTRUCTIONS,
            prompt_input=prompt_input,
            query_text=query_text,
            json_schema=BUSINESS_REASONING_SCHEMA,
            schema_name="business_reasoning_v1",
            case_id=case_id or None,
            engagement_id=engagement_id or None,
            model=model,
            verbose=verbose,
            output_model=BusinessReasoningResult,
            context_bundle=context_bundle,
            client_timeout=45,
            max_retries=2,
        )
        if stage_call is None:
            return fallback_business_reasoning(reason="central_llm_stage_unavailable")
        if str(stage_call.get("parse_status") or "") == "pydantic_failed":
            errors = (stage_call.get("request_meta") or {}).get("pydantic_errors")
            logger.warning("Pydantic ValidationError in business reasoning", extra={"x": {
                "error": str(errors)[:500],
            }})
        parsed = parse_and_validate_business_reasoning(stage_call["response_text"])
        # SLICE-2A: a Brain 1 consumer could not tell a real model result from a repaired,
        # coerced, skipped or fallback one -- every path returned the same schema-valid shape.
        # source_mode/reasoning_status make authorship explicit. Behaviour is unchanged.
        coerced = list(parsed.pop("normalization_notes", []) or [])
        meta = dict(stage_call) if isinstance(stage_call, dict) else {}
        meta["stage_name"] = "business_reasoning"
        meta["source_mode"] = "normalized_model_result" if coerced else "model_result"
        meta["reasoning_status"] = "ok"
        meta["fallback_used"] = False
        meta["normalization_notes"] = coerced
        parsed["execution_metadata"] = meta

        # Guard: zweryfikuj rekomendowaną akcję względem stanu sprawy
        case_state = str(intake_result.get("case_assessment", {}).get("state_detected", "") or "").strip()
        _validate_recommended_action(parsed, case_state)

        logger.info("BUSINESS_REASONING_COMPLETED", extra={"x": {
            "case_id": str(case_id or ""),
            "business_area": str(parsed.get("business_area", "")),
            "recommended_action": str(parsed.get("recommended_next_action", "")),
            "confidence_business": float(parsed.get("confidence", {}).get("business_confidence", 0)),
            "confidence_action": float(parsed.get("confidence", {}).get("action_confidence", 0)),
        }})
        # Pydantic validation of business reasoning output
        try:
            from schemas import BusinessReasoningResult as BusinessReasoningModel
            _ = BusinessReasoningModel(
                business_area=str(parsed.get("business_area", "")),
                customer_state=str(parsed.get("customer_state_guess", "") or None),
                recommended_next_action=str(parsed.get("recommended_next_action", "")),
                priority=str(parsed.get("urgency", "normal")),
                risks_summary_pl=str(parsed.get("risks", [""])[0]) if parsed.get("risks") else None,
                overall_confidence=float(parsed.get("confidence", {}).get("business_confidence", 0)),
                operator_note_pl=str(parsed.get("operator_note", "") or None),
            )
        except Exception as exc:
            logger.warning("Business reasoning Pydantic validation failed", extra={"x": {"error": str(exc)[:300]}})
        return parsed
    except GroqClientError as exc:
        return fallback_business_reasoning(reason=sanitize_text(str(exc)))


def parse_and_validate_business_reasoning(raw_text: str) -> dict[str, Any]:
    """Parse raw model text into a validated business reasoning contract."""
    try:
        candidate = json.loads(extract_json_candidate(raw_text))
    except json.JSONDecodeError as exc:
        raise GroqClientError(f"Business reasoning did not return valid JSON: {exc}") from exc
    return validate_business_reasoning_result(candidate)


def fallback_business_reasoning(*, reason: str) -> dict[str, Any]:
    """Return a conservative shadow-only fallback result."""
    result = validate_business_reasoning_result(
        {
            "business_interpretation": "Business reasoning unavailable.",
            "business_area": "unknown",
            "customer_state_guess": "unclear",
            "recommended_next_action": "escalate_review",
            "recommended_action_reason": "Business reasoning could not be confirmed safely.",
            "missing_information": [],
            "risks": ["business_reasoning_unavailable"],
            "urgency": "normal",
            "operator_note": "Business reasoning unavailable; manual review recommended.",
            "confidence": {
                "business_confidence": 0.0,
                "action_confidence": 0.0,
            },
            "human_review_bias": "high",
            "safety_notes": [reason],
            "evidence_refs": [],
            "assumptions": [],
            "unsupported_claims": [f"Business reasoning unavailable: {reason}"],
            "conflict_refs": [],
        }
    )
    result["execution_metadata"] = {
        "stage_name": "business_reasoning",
        "fallback_used": True,
        "parse_status": "fallback",
        # SLICE-2A: honest labelling only. fallback_business_reasoning's SEMANTICS are unchanged
        # in this slice (operator decision D defers that); it simply stops being indistinguishable
        # from a real conservative decision.
        "source_mode": "fallback",
        "reasoning_status": "unavailable",
        "error": reason,
    }
    return result


def build_skipped_business_reasoning(
    *,
    lane: str,
    intake_result: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Return a deterministic shadow artifact when the lane skips deep business reasoning."""
    area_mapping = {
        "sales": "lead",
        "service": "service",
        "finance": "finance",
        "general_admin": "admin",
        "internal_coordination": "internal",
        "supplier_commercial": "supplier",
    }
    business_area = area_mapping.get(str(intake_result.get("business_area") or "").strip(), "unknown")
    templates = {
        "skip": {
            "business_interpretation": "Deterministic skip lane marked the message as obvious noise.",
            "recommended_next_action": "ignore",
            "recommended_action_reason": "No meaningful business signal was detected before the LLM stage.",
            "missing_information": [],
            "risks": [],
            "urgency": "low",
            "operator_note": "No operator action recommended.",
        },
        "reference_only": {
            "business_interpretation": "Informational mail should remain visible as reference only.",
            "recommended_next_action": "wait",
            "recommended_action_reason": "The message carries reference value but no clear active ask.",
            "missing_information": [],
            "risks": [],
            "urgency": "low",
            "operator_note": "Keep as reference; no reply is needed unless a follow-up ask appears.",
        },
        "review_direct": {
            "business_interpretation": "Forwarded or low-signal content requires manual interpretation.",
            "recommended_next_action": "escalate_review",
            "recommended_action_reason": "The message is too implicit for safe autonomous business interpretation.",
            "missing_information": ["explicit operator instruction"],
            "risks": ["manual_review_first"],
            "urgency": "normal",
            "operator_note": "Review the forwarded context and identify the concrete ask before acting.",
        },
    }
    template = templates.get(lane) or templates["skip"]
    result = validate_business_reasoning_result(
        {
            "business_interpretation": template["business_interpretation"],
            "business_area": business_area,
            "customer_state_guess": "unclear",
            "recommended_next_action": template["recommended_next_action"],
            "recommended_action_reason": template["recommended_action_reason"],
            "missing_information": template["missing_information"],
            "risks": template["risks"],
            "urgency": template["urgency"],
            "operator_note": template["operator_note"],
            "business_summary_short": template["business_interpretation"],
            "reply_recommended": False,
            "human_review_bias": "medium" if lane == "reference_only" else "high",
            "safety_notes": [reason],
            "evidence_refs": [],
            "assumptions": [],
            "unsupported_claims": [f"Business reasoning skipped: {reason}"],
            "conflict_refs": [],
            "confidence": {
                "business_confidence": 0.0,
                "action_confidence": 0.0,
            },
        }
    )
    result["execution_metadata"] = {
        "stage_name": "business_reasoning",
        "fallback_used": True,
        "parse_status": "skipped_for_lane",
        "source_mode": "skipped_for_lane",
        "reasoning_status": "skipped",
        "lane": lane,
        "error": reason,
    }
    return result


__all__ = [
    "build_skipped_business_reasoning",
    "build_business_reasoning_prompt_input",
    "fallback_business_reasoning",
    "parse_and_validate_business_reasoning",
    "run_business_reasoning",
]
