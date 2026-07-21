"""Operator-facing reply drafting in shadow mode."""

from __future__ import annotations

import json
import re
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
        parsed = gate_reply_draft_commitments(parsed, case_state=_draft_case_state(intake_result, business_result))
        parsed["execution_metadata"] = stage_call
        return parsed
    except GroqClientError as exc:
        return fallback_reply_drafter(reason=sanitize_text(str(exc)))


def _draft_case_state(intake_result: dict[str, Any], business_result: dict[str, Any]) -> dict[str, Any]:
    """Authoritative case-state signals available at shadow-draft time.

    ``reply_drafter`` runs before any mutation is executed (it is a shadow
    proposal, not a confirmed action), so today there is no field anywhere in
    the intake/business payload that marks a visit as actually scheduled or an
    action as actually completed. That absence IS the authoritative state: a
    draft claiming otherwise at this stage is never supported. Explicit,
    forward-compatible flags are still honored if a caller ever supplies them.
    """
    return {
        "visit_confirmed": bool(intake_result.get("visit_confirmed") or business_result.get("visit_confirmed")),
        "action_completed": bool(intake_result.get("action_completed") or business_result.get("action_completed")),
        "deadline_confirmed": bool(intake_result.get("deadline_confirmed") or business_result.get("deadline_confirmed")),
    }


# Each entry: (compiled pattern, case_state key that must be True to leave the
# match intact, safe non-committal replacement used when unsupported). Patterns
# detect a specific committing CLAIM (visit already arranged, action already
# done, categorical guarantee, concrete delivery/installation deadline) — the
# gate then defers the allow/rewrite DECISION to case_state, so this is not a
# blacklist verdict: the same phrase is kept when the state actually supports it.
_COMMITMENT_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"gwarantujemy|na pewno|sto procent pewn\w*", re.IGNORECASE),
        "",  # a categorical guarantee is never supported deterministically
        "postaramy sie zapewnic najlepsze rozwiazanie",
    ),
    (
        re.compile(r"wizyta jest (?:juz )?umowion\w+|umowion\w+ wizyt\w+", re.IGNORECASE),
        "visit_confirmed",
        "zaproponujemy termin wizyty i potwierdzimy go z Panstwem",
    ),
    (
        re.compile(r"juz (?:wyslalismy|zamontowalismy|zrealizowalismy|dostarczylismy)", re.IGNORECASE),
        "action_completed",
        "przygotowujemy to dla Panstwa",
    ),
    (
        re.compile(r"jutro (?:zamontujemy|dostarczymy|przyjedziemy|wyslemy)", re.IGNORECASE),
        "deadline_confirmed",
        "wkrotce, po ustaleniu szczegolow",
    ),
)


def _gate_commitment_text(text: str, *, case_state: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    out = text
    for pattern, required_state_key, replacement in _COMMITMENT_PATTERNS:
        supported = bool(case_state.get(required_state_key)) if required_state_key else False
        if supported:
            continue

        def _replace(match: "re.Match[str]", _replacement: str = replacement) -> str:
            return _replacement

        new_out = pattern.sub(_replace, out)
        if new_out != out:
            reasons.append(f"unsupported_commitment_rewritten:{required_state_key or 'guarantee'}")
        out = new_out
    return out, reasons


def gate_reply_draft_commitments(parsed: dict[str, Any], *, case_state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rewrite unsupported commitment/promise claims in every drafted body.

    A commitment is left intact only when ``case_state`` actually supports it
    (see ``_draft_case_state``); otherwise the specific offending fragment —
    never the whole message — is replaced with accurate, non-committal wording.
    """
    state = case_state or {}
    drafts = list(parsed.get("drafts") or [])
    any_rewritten = False
    new_drafts: list[dict[str, Any]] = []
    for draft in drafts:
        if not isinstance(draft, dict):
            new_drafts.append(draft)
            continue
        body = str(draft.get("body") or "")
        new_body, reasons = _gate_commitment_text(body, case_state=state)
        if reasons:
            any_rewritten = True
            draft = {**draft, "body": new_body}
        new_drafts.append(draft)
    if not any_rewritten:
        return parsed
    reasons_all: list[str] = []
    for draft in drafts:
        if isinstance(draft, dict):
            _, reasons = _gate_commitment_text(str(draft.get("body") or ""), case_state=state)
            reasons_all.extend(reasons)
    out = dict(parsed)
    out["drafts"] = new_drafts
    out["requires_manual_edit"] = True
    out["do_not_send_reasons"] = list(dict.fromkeys(list(parsed.get("do_not_send_reasons") or []) + reasons_all))
    return out


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
