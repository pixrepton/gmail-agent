"""Operator-facing reply drafting in shadow mode."""

from __future__ import annotations

import json
from typing import Any

from central_llm_stage import resolve_case_id, run_central_structured_stage
from config import Settings
from groq_client import GroqClientError, extract_json_candidate
from log_config import get_logger
from signal_extractor import build_signal_extraction_query
from intake_payload import build_reply_draft_payload, load_prompt_text
from intake_schema import validate_reply_draft_result
from llm_contracts.reply_draft import ReplyDraftResult
from redaction import sanitize_text

logger = get_logger(__name__)


REPLY_DRAFT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["draft_enabled", "drafts", "do_not_send_reasons"],
    "properties": {
        "draft_enabled": {"type": "boolean"},
        "recommended_variant": {"type": "string"},
        "do_not_send_reasons": {"type": "array", "items": {"type": "string"}},
        "requires_manual_edit": {"type": "boolean"},
        "unsafe_claims_detected": {"type": "boolean"},
        "confidence": {"type": "number"},
        "drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["variant", "subject_suggestion", "body", "goal"],
                "properties": {
                    "variant": {"type": "string"},
                    "subject_suggestion": {"type": "string"},
                    "body": {"type": "string"},
                    "goal": {"type": "string"},
                    "tone": {"type": "string"},
                },
            },
        },
    },
}


def should_draft_reply(
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    business_result: dict[str, Any] | None,
) -> bool:
    """Return True when drafting a reply is useful enough to justify shadow generation."""
    action = str(intake_result.get("decision", {}).get("action") or "")
    business_action = str((business_result or {}).get("recommended_next_action") or "")
    if action in {"ignore", "mark_watchlist"}:
        return False
    if intake_result.get("review_required") and business_action not in {"reply", "collect_data"}:
        return False
    sender = str(snapshot.get("source_message", {}).get("sender") or "").lower()
    if any(token in sender for token in ("noreply", "no-reply", "mailer-daemon")):
        return False
    return business_action in {"reply", "collect_data", "call"} or bool((business_result or {}).get("reply_recommended"))


def build_reply_draft_prompt_input(
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    business_result: dict[str, Any],
    business_context_bundle: dict[str, Any],
    context_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the reply-drafter input payload."""
    return build_reply_draft_payload(
        snapshot,
        intake_result,
        business_result,
        business_context_bundle,
        context_bundle,
    )


def run_reply_drafter(
    *,
    settings: Settings,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    business_result: dict[str, Any],
    business_context_bundle: dict[str, Any],
    context_bundle: dict[str, Any] | None = None,
    model: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Run reply drafting in shadow mode with a safe fallback."""
    if not should_draft_reply(snapshot, intake_result, business_result):
        return fallback_reply_drafter(reason="reply_not_recommended")

    prompt_input = build_reply_draft_prompt_input(
        snapshot,
        intake_result,
        business_result,
        business_context_bundle,
        context_bundle,
    )
    instructions = load_prompt_text("reply_drafter_system_prompt.md")

    try:
        case_id = resolve_case_id(context_bundle=context_bundle or {})
        query_text = str(intake_result.get("reason") or "").strip() or build_signal_extraction_query(snapshot)
        stage_call = run_central_structured_stage(
            settings,
            stage_name="reply_drafter",
            task_instructions=instructions,
            prompt_input=prompt_input,
            query_text=query_text,
            json_schema=REPLY_DRAFT_SCHEMA,
            schema_name="reply_draft_v1",
            case_id=case_id or None,
            model=model,
            verbose=verbose,
            output_model=ReplyDraftResult,
            context_bundle=context_bundle,
        )
        if stage_call is None:
            return fallback_reply_drafter(reason="central_llm_stage_unavailable")
        if str(stage_call.get("parse_status") or "") == "pydantic_failed":
            errors = (stage_call.get("request_meta") or {}).get("pydantic_errors")
            logger.warning("[reply_drafter] Pydantic ValidationError: %s", errors)
        parsed = parse_and_validate_reply_draft(stage_call["response_text"])
        parsed["execution_metadata"] = stage_call
        return parsed
    except GroqClientError as exc:
        return fallback_reply_drafter(reason=sanitize_text(str(exc)))


def parse_and_validate_reply_draft(raw_text: str) -> dict[str, Any]:
    """Parse raw reply-drafter output into a validated contract."""
    try:
        candidate = json.loads(extract_json_candidate(raw_text))
    except json.JSONDecodeError as exc:
        raise GroqClientError(f"Reply drafter did not return valid JSON: {exc}") from exc
    return validate_reply_draft_result(candidate)


def fallback_reply_drafter(*, reason: str) -> dict[str, Any]:
    """Return a conservative non-draft fallback."""
    result = validate_reply_draft_result(
        {
            "draft_enabled": False,
            "drafts": [],
            "do_not_send_reasons": [reason],
            "requires_manual_edit": True,
            "unsafe_claims_detected": False,
            "confidence": 0.0,
        }
    )
    result["execution_metadata"] = {
        "stage_name": "reply_drafter",
        "fallback_used": True,
        "parse_status": "fallback",
        "error": reason,
    }
    return result


def build_skipped_reply_draft(*, lane: str, reason: str) -> dict[str, Any]:
    """Return a deterministic non-draft artifact when the lane skips reply drafting."""
    do_not_send_reasons = {
        "skip": ["skip_lane"],
        "reference_only": ["reference_only"],
        "review_direct": ["manual_review_first"],
    }.get(lane, [reason])
    result = validate_reply_draft_result(
        {
            "draft_enabled": False,
            "drafts": [],
            "do_not_send_reasons": do_not_send_reasons,
            "requires_manual_edit": True,
            "unsafe_claims_detected": False,
            "confidence": 0.0,
        }
    )
    result["execution_metadata"] = {
        "stage_name": "reply_drafter",
        "fallback_used": True,
        "parse_status": "skipped_for_lane",
        "lane": lane,
        "error": reason,
    }
    return result


__all__ = [
    "build_skipped_reply_draft",
    "build_reply_draft_prompt_input",
    "fallback_reply_drafter",
    "run_reply_drafter",
    "should_draft_reply",
]
