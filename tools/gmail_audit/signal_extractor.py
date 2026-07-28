"""HVAC signal extraction via central ContextAssembler + structured LLM contract."""

from __future__ import annotations

from typing import Any

from central_llm_stage import (
    resolve_case_id,
    resolve_engagement_id,
    run_central_structured_stage,
)
from config import Settings
from exceptions import LLMTimeoutError, LLMInvalidResponseError, SignalExtractionError
from intake_payload import build_intake_reasoning_payload
from llm_contracts.signal_extraction import SignalExtractionResult, canonicalize_hvac_intent


SIGNAL_EXTRACTION_INSTRUCTIONS = (
    "Extract HVAC lead signals from the inbound message payload. "
    "Use only evidence present in the payload; do not invent OZC or pricing. "
    "Geographic signal must reflect explicit location mentions only. "
    # STRUCTURED-INPUT-AND-CAPABILITY-BASELINE-CLOSEOUT-01: current_heating_source should
    # not be left null when the message describes a new/under-construction building with no
    # legacy heating system -- state that explicitly (e.g. 'brak / nowy budynek w budowie'),
    # since 'no current source because construction is new' is a real, evidenced fact, not
    # an unknown. General instruction; do not fabricate a source when genuinely unstated.
    "If the building is described as new construction or currently being built with no "
    "existing heating installation, set current_heating_source explicitly to describe that "
    "(e.g. 'brak / nowy budynek w budowie') rather than leaving it null."
)


def build_signal_extraction_query(snapshot: dict[str, Any]) -> str:
    msg = snapshot.get("source_message") if isinstance(snapshot.get("source_message"), dict) else {}
    subject = str(msg.get("subject") or "").strip()
    summary = str(snapshot.get("summary_text") or "").strip()
    return " ".join(part for part in (subject, summary) if part).strip() or "hvac lead"


_SIGNAL_EXTRACTION_FAILURE_STATUSES = frozenset({"extraction_failed", "empty_result"})


def signal_extraction_failed(result: dict[str, Any] | None) -> bool:
    """SLICE-1: True when `run_signal_extraction` returned a failure marker, not signals.

    The marker dicts are truthy, so callers cannot use a bare `if result:` to tell a failed
    extraction from a successful one. A failure must be routed to telemetry, never passed into a
    prompt as evidence.
    """
    if not isinstance(result, dict) or not result:
        return False
    return str(result.get("parse_status") or "").strip() in _SIGNAL_EXTRACTION_FAILURE_STATUSES


def _canonicalize_result(result: dict[str, Any]) -> dict[str, Any]:
    """Deterministic post-validation normalization step (no LLM call): replaces the raw
    free-text hvac_intent with its canonical class while preserving the original text as
    evidence. Applied here (not at the Pydantic validation boundary) so a real-production
    free-text response can never turn into a hard parse failure -- see contract.md tier
    rationale (STRUCTURED-INPUT-AND-CAPABILITY-BASELINE-CLOSEOUT-01 Phase 2/3)."""
    canonical, raw_evidence = canonicalize_hvac_intent(result.get("hvac_intent") or "")
    result["hvac_intent"] = canonical
    result["hvac_intent_raw_evidence"] = raw_evidence
    return result


def run_signal_extraction(
    *,
    settings: Settings,
    snapshot: dict[str, Any],
    context_bundle: dict[str, Any] | None = None,
    case_link_result: dict[str, Any] | None = None,
    model: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Return validated HVAC signals as a plain dict (empty on failure)."""
    prompt_input = build_intake_reasoning_payload(snapshot, context_bundle or {})
    query_text = build_signal_extraction_query(snapshot)
    case_id = resolve_case_id(context_bundle=context_bundle, case_link_result=case_link_result)
    engagement_id = resolve_engagement_id(
        context_bundle=context_bundle,
        case_link_result=case_link_result,
    )
    schema = SignalExtractionResult.model_json_schema()
    try:
        stage = run_central_structured_stage(
            settings,
            stage_name="signal_extraction",
            task_instructions=SIGNAL_EXTRACTION_INSTRUCTIONS,
            prompt_input=prompt_input,
            query_text=query_text,
            json_schema=schema,
            schema_name="signal_extraction_v1",
            case_id=case_id or None,
            engagement_id=engagement_id or None,
            model=model,
            verbose=verbose,
            output_model=SignalExtractionResult,
            context_bundle=context_bundle,
            client_timeout=30,
            max_retries=2,
            correlation_id=str((snapshot.get("source_message") or {}).get("message_id") or "").strip() or None,
        )
        if stage is None:
            return {"parse_status": "extraction_failed", "error_reason": "central_stage_unavailable"}
        raw = stage.get("response_json")
        if isinstance(raw, dict) and raw:
            validated = SignalExtractionResult.model_validate(raw)
            return _canonicalize_result(validated.model_dump())
        text = str(stage.get("response_text") or "")
        if text:
            validated = SignalExtractionResult.model_validate_json(text)
            return _canonicalize_result(validated.model_dump())
    except LLMTimeoutError as exc:
        raise SignalExtractionError("LLM timeout during signal extraction") from exc
    except LLMInvalidResponseError as exc:
        raise SignalExtractionError("LLM returned invalid response during extraction") from exc
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        return {"parse_status": "extraction_failed", "error_reason": reason}
    return {"parse_status": "empty_result", "error_reason": "no_valid_signal_in_response"}
