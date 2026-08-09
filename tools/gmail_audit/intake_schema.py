"""Schema loading and strict validation for Gmail intake outputs."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from groq_client import GroqClientError, extract_json_candidate
from evidence_ref import normalize_case_guidance_evidence_refs, strip_forbidden_evidence_like_rows
from intake_policy import (
    ACTION_BEARING_DECISIONS,
    CREATE_TASK_CASE_LINK_SUSPICIOUS_THRESHOLD,
    EXTRACTION_CONFIDENCE_REQUIRES_REVIEW,
    FORCED_REVIEW_FLAGS,
    HIGH_RISK_ACTION_MIN_DECISION_CONFIDENCE,
    HIGH_RISK_ACTIONS,
    HIGH_RISK_AREAS,
    LOW_DECISION_CONFIDENCE_REQUIRES_REVIEW,
    NEW_CASE_ACTIONS,
    POSSIBLE_EXISTING_CASE_THRESHOLD,
    REVIEW_FLAGS,
    REFERENCE_ONLY_ACTIONS,
    REFERENCE_ACTION_MIN_DECISION_CONFIDENCE,
    SELF_FORWARD_REQUIRES_STRONG_MATCH,
    STRONG_EXISTING_CASE_THRESHOLD,
    UPDATE_ACTIONS,
    OUTPUT_ORIGIN_INVALID,
    OUTPUT_ORIGINS,
    OUTPUT_ORIGIN_NORMALIZED_VALID,
    OUTPUT_ORIGIN_RAW_VALID,
    OUTPUT_ORIGIN_REPAIRED_VALID,
    top_case_candidate_confidence,
)

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - handled at runtime
    Draft202012Validator = None  # type: ignore[assignment]


SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"
DEFAULT_SCHEMA_PATH = SCHEMAS_DIR / "intake_output_v1.json"

# Semantic ceiling: decision_confidence must not exceed signal_confidence by more than this margin
# (see _validate_semantics). Repair clamps decision_confidence down to this ceiling; signal is never raised.
DECISION_OVER_SIGNAL_MARGIN = 0.2


@dataclass(slots=True)
class ValidationResult:
    is_valid: bool
    parse_ok: bool
    schema_ok: bool
    semantic_ok: bool
    errors: list[str]
    data: dict[str, Any] | None


@dataclass(slots=True)
class ValidationTrace:
    result: ValidationResult
    raw_result: ValidationResult
    normalized_result: ValidationResult | None
    repaired_result: ValidationResult | None
    raw_candidate: dict[str, Any] | None
    normalized_candidate: dict[str, Any] | None
    repaired_candidate: dict[str, Any] | None
    normalization_applied: bool
    repair_applied: bool
    normalization_notes: list[str]
    repair_notes: list[str]
    final_output_origin: str


def load_intake_schema(schema_path: str | Path | None = None) -> dict[str, Any]:
    """Load the intake JSON schema from disk."""
    path = Path(schema_path) if schema_path else DEFAULT_SCHEMA_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def validate_output_text(
    raw_text: str,
    *,
    schema: dict[str, Any] | None = None,
) -> ValidationResult:
    """Parse, schema-validate, and semantically validate intake output text."""
    return validate_output_with_repair(raw_text, schema=schema).result


def validate_output_with_repair(
    raw_text: str,
    *,
    schema: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> ValidationTrace:
    """Validate model output with safe normalization and local repair."""
    schema_obj = schema or load_intake_schema()
    raw_result = _parse_and_validate_raw(raw_text, schema_obj)
    if not raw_result.parse_ok or raw_result.data is None:
        return ValidationTrace(
            result=raw_result,
            raw_result=raw_result,
            normalized_result=None,
            repaired_result=None,
            raw_candidate=None,
            normalized_candidate=None,
            repaired_candidate=None,
            normalization_applied=False,
            repair_applied=False,
            normalization_notes=[],
            repair_notes=[],
            final_output_origin=OUTPUT_ORIGIN_INVALID,
        )

    raw_candidate = raw_result.data
    normalized_candidate, normalization_notes = normalize_candidate(raw_candidate, snapshot=snapshot)
    normalization_applied = normalized_candidate != raw_candidate or bool(normalization_notes)
    normalized_result = _validate_candidate_data(normalized_candidate, schema_obj)
    if normalized_result.is_valid:
        final_output_origin = OUTPUT_ORIGIN_NORMALIZED_VALID if normalization_applied else OUTPUT_ORIGIN_RAW_VALID
        return ValidationTrace(
            result=normalized_result,
            raw_result=raw_result,
            normalized_result=normalized_result,
            repaired_result=None,
            raw_candidate=raw_candidate,
            normalized_candidate=normalized_candidate if normalization_applied else raw_candidate,
            repaired_candidate=None,
            normalization_applied=normalization_applied,
            repair_applied=False,
            normalization_notes=normalization_notes,
            repair_notes=[],
            final_output_origin=final_output_origin,
        )

    repaired_candidate, repair_notes = attempt_safe_repair(
        normalized_candidate,
        errors=normalized_result.errors or raw_result.errors,
        snapshot=snapshot,
        schema=schema_obj,
    )
    if repaired_candidate is not None:
        repaired_result = _validate_candidate_data(repaired_candidate, schema_obj)
        if repaired_result.is_valid:
            return ValidationTrace(
                result=repaired_result,
                raw_result=raw_result,
                normalized_result=normalized_result,
                repaired_result=repaired_result,
                raw_candidate=raw_candidate,
                normalized_candidate=normalized_candidate,
                repaired_candidate=repaired_candidate,
                normalization_applied=normalization_applied,
                repair_applied=True,
                normalization_notes=normalization_notes,
                repair_notes=repair_notes,
                final_output_origin=OUTPUT_ORIGIN_REPAIRED_VALID,
            )
        return ValidationTrace(
            result=repaired_result,
            raw_result=raw_result,
            normalized_result=normalized_result,
            repaired_result=repaired_result,
            raw_candidate=raw_candidate,
            normalized_candidate=normalized_candidate,
            repaired_candidate=repaired_candidate,
            normalization_applied=normalization_applied,
            repair_applied=True,
            normalization_notes=normalization_notes,
            repair_notes=repair_notes,
            final_output_origin=OUTPUT_ORIGIN_INVALID,
        )

    return ValidationTrace(
        result=normalized_result,
        raw_result=raw_result,
        normalized_result=normalized_result,
        repaired_result=None,
        raw_candidate=raw_candidate,
        normalized_candidate=normalized_candidate if normalization_applied else raw_candidate,
        repaired_candidate=None,
        normalization_applied=normalization_applied,
        repair_applied=False,
        normalization_notes=normalization_notes,
        repair_notes=[],
        final_output_origin=OUTPUT_ORIGIN_INVALID,
    )


def require_valid_output(raw_text: str, *, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return validated output or raise a friendly exception."""
    result = validate_output_text(raw_text, schema=schema)
    if not result.is_valid or result.data is None:
        joined = "; ".join(result.errors) or "Unknown validation error."
        raise GroqClientError(f"Structured intake output is invalid: {joined}")
    return result.data


CASE_LINK_DECISIONS = {"linked", "weak_link", "no_link", "competing_links"}
CASE_LINK_SOURCES = {"thread", "explicit_reference", "subject_continuity", "entity_match", "context_candidate", "none"}
BUSINESS_REASONING_AREAS = {"lead", "installation", "service", "finance", "supplier", "admin", "internal", "unknown"}
CUSTOMER_STATE_GUESSES = {
    "new_lead",
    "active_case",
    "post_offer",
    "waiting_for_data",
    "supplier_thread",
    "finance_flow",
    "unclear",
}
BUSINESS_NEXT_ACTIONS = {"reply", "call", "collect_data", "create_task", "update_case", "wait", "ignore", "escalate_review"}
BUSINESS_URGENCY_LEVELS = {"low", "normal", "high"}
HUMAN_REVIEW_BIAS = {"low", "medium", "high"}
REPLY_DRAFT_VARIANTS = {"short_operational", "customer_friendly"}
ACTION_PLAN_ACTIONS = {"create_review", "create_task", "update_case", "prepare_reply", "hold", "ignore"}
ACTION_PLAN_PROJECTION_MODES = {"task", "review", "case_update", "reference", "ignore"}
ACTION_PLAN_REVIEW_PRIORITIES = {"low", "normal", "high"}


def validate_intake_result(
    obj: dict[str, Any],
    *,
    final_output_origin: str | None = None,
    normalization_notes: list[str] | None = None,
    repair_notes: list[str] | None = None,
    guardrail_flags: list[str] | None = None,
) -> dict[str, Any]:
    """Return a normalized intake-result contract built on top of the v1 intake schema."""
    if not isinstance(obj, dict):
        raise GroqClientError("IntakeResult must be a JSON object.")

    schema_obj = load_intake_schema()
    result = _validate_candidate_data(obj, schema_obj)
    if not result.is_valid or result.data is None:
        joined = "; ".join(result.errors) or "Unknown intake result validation error."
        raise GroqClientError(f"IntakeResult is invalid: {joined}")

    intake_result = deepcopy(result.data)
    intake_result["classification"] = str(intake_result.get("primary_signal", {}).get("code") or "unknown")
    intake_result["review_required"] = bool(intake_result.get("review", {}).get("required"))
    intake_result["review_reasons"] = _normalize_string_list_contract(intake_result.get("review", {}).get("flags"))
    intake_result["linked_case_candidates"] = list(intake_result.get("thread", {}).get("linked_case_candidates") or [])
    intake_result["guardrail_flags"] = _normalize_string_list_contract(guardrail_flags)
    intake_result["normalization_notes"] = _normalize_string_list_contract(normalization_notes)
    intake_result["repair_notes"] = _normalize_string_list_contract(repair_notes)
    intake_result["final_output_origin"] = _normalize_choice(
        final_output_origin or intake_result.get("final_output_origin"),
        OUTPUT_ORIGINS,
        default=OUTPUT_ORIGIN_RAW_VALID,
        field_name="final_output_origin",
    )
    intake_result["classification_confidence"] = _bounded_float(
        intake_result.get("confidence", {}).get("signal_confidence"),
        default=0.0,
    )
    intake_result["output_warnings"] = _normalize_string_list_contract(
        [
            *intake_result["review_reasons"],
            *intake_result["guardrail_flags"],
        ]
    )
    return intake_result


def validate_case_link_result(obj: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the deterministic case-link contract for the v2 shadow stage."""
    if not isinstance(obj, dict):
        raise GroqClientError("CaseLinkResult must be a JSON object.")

    normalized_candidates: list[dict[str, Any]] = []
    for candidate in obj.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        case_key = str(candidate.get("case_key") or "").strip()
        if not case_key:
            continue
        reasons = _normalize_string_list_contract(candidate.get("reasons") or candidate.get("evidence"))
        normalized_candidates.append(
            {
                "case_key": case_key,
                "score": _bounded_float(candidate.get("score", candidate.get("match_confidence")), default=0.0),
                "source": _normalize_case_link_source(candidate.get("source") or candidate.get("case_type") or "none"),
                "reasons": reasons,
                "hard_match_count": _coerce_int(candidate.get("hard_match_count"), default=_count_hard_matches(reasons)),
                "soft_match_count": _coerce_int(candidate.get("soft_match_count"), default=max(0, len(reasons) - _count_hard_matches(reasons))),
            }
        )

    normalized_candidates.sort(key=lambda item: (item["score"], item["hard_match_count"]), reverse=True)
    selected_case_key = str(obj.get("selected_case_key") or "").strip()
    decision = _normalize_choice(obj.get("decision"), CASE_LINK_DECISIONS, default="no_link", field_name="case_link.decision")
    if not selected_case_key and normalized_candidates:
        selected_case_key = normalized_candidates[0]["case_key"]
    if decision == "no_link":
        selected_case_key = ""
    selected_case_id = str(obj.get("case_id") or obj.get("selected_case_id") or "").strip()
    if decision == "no_link":
        selected_case_id = ""

    reasons = _normalize_string_list_contract(obj.get("reasons"))
    normalized = {
        "selected_case_key": selected_case_key,
        "selected_case_id": selected_case_id,
        "case_id": selected_case_id,
        "decision": decision,
        "confidence": _bounded_float(obj.get("confidence"), default=normalized_candidates[0]["score"] if normalized_candidates else 0.0),
        "source": _normalize_case_link_source(obj.get("source"), default=_infer_case_link_source_from_candidates(normalized_candidates, decision)),
        "reasons": reasons,
        "candidates": normalized_candidates,
        "competing_case_keys": [item["case_key"] for item in normalized_candidates[1:3]] if decision == "competing_links" else [],
        "hard_match_count": _coerce_int(obj.get("hard_match_count"), default=sum(item["hard_match_count"] for item in normalized_candidates)),
        "soft_match_count": _coerce_int(obj.get("soft_match_count"), default=sum(item["soft_match_count"] for item in normalized_candidates)),
    }
    return normalized


def validate_business_reasoning_result(obj: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the business-reasoning stage contract."""
    if not isinstance(obj, dict):
        raise GroqClientError("BusinessReasoningResult must be a JSON object.")

    coercion_notes: list[dict[str, Any]] = []
    confidence_source = obj.get("confidence") if isinstance(obj.get("confidence"), dict) else {}
    business_interpretation = _string_or_default(
        obj.get("business_interpretation"),
        default="Business interpretation unavailable.",
    )
    recommended_next_action = _normalize_choice(
        obj.get("recommended_next_action"),
        BUSINESS_NEXT_ACTIONS,
        default="escalate_review",
        field_name="business_reasoning.recommended_next_action",
            notes=coercion_notes,
    )
    customer_state_guess = _normalize_choice(
        obj.get("customer_state_guess"),
        CUSTOMER_STATE_GUESSES,
        default="unclear",
        field_name="business_reasoning.customer_state_guess",
        notes=coercion_notes,
    )
    # CLOSEOUT-01 Phase 4 — deterministic decision-class consistency normalization.
    # `collect_data` is a pre-offer data-gathering action; it is contract-incoherent once the
    # customer is already `post_offer` (the offer is out, so the operative blocker is an
    # operator decision/negotiation — e.g. a discount request — not missing intake data).
    # Canonicalize that one incoherent (customer_state_guess, recommended_next_action) pair to
    # the module's existing safe default `escalate_review`. This keys ONLY on normalized enum
    # fields (never on free reasoning text), is one-directional (it can only ever *increase*
    # escalation, so it can never introduce an unsafe non-escalation), and encodes the contract's
    # own "prefer escalate_review when evidence is weak" bias deterministically. It resolves the
    # post_offer recommended_action flip (escalate_review vs collect_data) proven on
    # byte-identical production-faithful BusinessReasoning input (DEC-02).
    if customer_state_guess == "post_offer" and recommended_next_action == "collect_data":
        recommended_next_action = "escalate_review"
    normalized = {
        "business_interpretation": business_interpretation,
        "business_area": _normalize_choice(obj.get("business_area"), BUSINESS_REASONING_AREAS, default="unknown", field_name="business_reasoning.business_area", notes=coercion_notes),
        "customer_state_guess": customer_state_guess,
        "recommended_next_action": recommended_next_action,
        "recommended_action_reason": _string_or_default(
            obj.get("recommended_action_reason"),
            default="Manual review recommended until business reasoning is confirmed.",
        ),
        "missing_information": _normalize_string_list_contract(obj.get("missing_information")),
        "risks": _normalize_string_list_contract(obj.get("risks")),
        "urgency": _normalize_choice(obj.get("urgency"), BUSINESS_URGENCY_LEVELS, default="normal", field_name="business_reasoning.urgency", notes=coercion_notes),
        "operator_note": _string_or_default(obj.get("operator_note"), default="Manual review recommended."),
        "confidence": {
            "business_confidence": _bounded_float(confidence_source.get("business_confidence"), default=0.0),
            "action_confidence": _bounded_float(confidence_source.get("action_confidence"), default=0.0),
        },
        "business_summary_short": _string_or_default(
            obj.get("business_summary_short"),
            default=business_interpretation[:160],
        ),
        "reply_recommended": bool(obj.get("reply_recommended")) or recommended_next_action in {"reply", "collect_data"},
        "human_review_bias": _normalize_choice(
            obj.get("human_review_bias"),
            HUMAN_REVIEW_BIAS,
            default="medium",
            field_name="business_reasoning.human_review_bias",
            notes=coercion_notes,
        ),
        "safety_notes": _normalize_string_list_contract(obj.get("safety_notes")),
        "evidence_refs": normalize_case_guidance_evidence_refs(obj.get("evidence_refs") or [], source_mode="llm_reasoned"),
        "assumptions": _normalize_string_list_contract(obj.get("assumptions")),
        "unsupported_claims": _normalize_string_list_contract(obj.get("unsupported_claims")),
        "conflict_refs": strip_forbidden_evidence_like_rows(obj.get("conflict_refs") or []),
    }
    # SLICE-2A: bounded per-field coercion evidence. Absent when nothing was coerced, so a clean
    # result stays byte-identical to the pre-slice contract.
    if coercion_notes:
        normalized["normalization_notes"] = coercion_notes
    return normalized


# Narrow normalization boundary for RC-D1: the LLM reliably emits a small set of
# semantically-equivalent alternate serializations of the same canonical draft
# contract (a nested single-key envelope such as "reply_draft_v1", and "subject"
# instead of "subject_suggestion"). These are recognized structurally, not by a
# large keyword taxonomy, and never fabricate content — they only relocate a
# value the model already produced under its intended field name.
def _unwrap_reply_draft_envelope(obj: dict[str, Any]) -> dict[str, Any]:
    if "drafts" in obj:
        return obj
    if len(obj) == 1:
        (only_key, only_value), = obj.items()
        if isinstance(only_value, dict) and "drafts" in only_value:
            return only_value
    return obj


def _normalize_reply_draft_item_keys(draft: dict[str, Any]) -> dict[str, Any]:
    if "subject_suggestion" not in draft or not str(draft.get("subject_suggestion") or "").strip():
        subject = draft.get("subject")
        if isinstance(subject, str) and subject.strip():
            draft = {**draft, "subject_suggestion": subject}
    return draft


def validate_reply_draft_result(obj: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the reply-drafter stage contract."""
    if not isinstance(obj, dict):
        raise GroqClientError("ReplyDraftResult must be a JSON object.")
    obj = _unwrap_reply_draft_envelope(obj)

    drafts: list[dict[str, Any]] = []
    for draft in obj.get("drafts") or []:
        if not isinstance(draft, dict):
            continue
        draft = _normalize_reply_draft_item_keys(draft)
        variant = _normalize_choice(
            draft.get("variant"),
            REPLY_DRAFT_VARIANTS,
            default="short_operational",
            field_name="reply_draft.variant",
        )
        body = _string_or_default(draft.get("body"), default="")
        if not body:
            continue
        drafts.append(
            {
                "variant": variant,
                "subject_suggestion": _string_or_default(draft.get("subject_suggestion"), default=""),
                "body": body,
                "goal": _string_or_default(draft.get("goal"), default="respond_safely"),
                "tone": _string_or_default(draft.get("tone"), default="operational"),
            }
        )

    draft_enabled = bool(obj.get("draft_enabled")) and bool(drafts)
    confidence = _bounded_float(obj.get("confidence"), default=0.0)
    recommended_variant = str(obj.get("recommended_variant") or "").strip()
    if not recommended_variant and drafts:
        recommended_variant = drafts[0]["variant"]

    return {
        "draft_enabled": draft_enabled,
        "drafts": drafts,
        "recommended_variant": recommended_variant,
        "do_not_send_reasons": _normalize_string_list_contract(obj.get("do_not_send_reasons")),
        "requires_manual_edit": bool(obj.get("requires_manual_edit", True)),
        "unsafe_claims_detected": bool(obj.get("unsafe_claims_detected", False)),
        "confidence": confidence,
    }


def validate_action_plan_result(obj: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the action-planner stage contract."""
    if not isinstance(obj, dict):
        raise GroqClientError("ActionPlanResult must be a JSON object.")

    return {
        "primary_action": _normalize_choice(obj.get("primary_action"), ACTION_PLAN_ACTIONS, default="hold", field_name="action_plan.primary_action"),
        "secondary_actions": _normalize_string_list_contract(obj.get("secondary_actions")),
        "operator_checklist": _normalize_string_list_contract(obj.get("operator_checklist")),
        "daszek_projection_mode": _normalize_choice(
            obj.get("daszek_projection_mode"),
            ACTION_PLAN_PROJECTION_MODES,
            default="review",
            field_name="action_plan.daszek_projection_mode",
        ),
        "safe_for_live_push": bool(obj.get("safe_for_live_push", False)),
        "safe_for_operator_projection": bool(obj.get("safe_for_operator_projection", False)),
        "confidence": _bounded_float(obj.get("confidence"), default=0.0),
        "why_this_action": _string_or_default(obj.get("why_this_action"), default="Conservative operator hold."),
        "why_not_other_actions": _normalize_string_list_contract(obj.get("why_not_other_actions")),
        "review_priority": _normalize_choice(
            obj.get("review_priority"),
            ACTION_PLAN_REVIEW_PRIORITIES,
            default="normal",
            field_name="action_plan.review_priority",
        ),
        "requires_case_confirmation": bool(obj.get("requires_case_confirmation", False)),
    }


def _normalize_case_link_source(value: Any, *, default: str = "none") -> str:
    text = str(value or "").strip()
    mapping = {
        "thread_context": "thread",
        "reference_context": "explicit_reference",
        "subject_context": "subject_continuity",
        "message_context": "context_candidate",
        "thread": "thread",
        "explicit_reference": "explicit_reference",
        "subject_continuity": "subject_continuity",
        "entity_match": "entity_match",
        "context_candidate": "context_candidate",
        "none": "none",
    }
    return mapping.get(text, default)


def _infer_case_link_source_from_candidates(candidates: list[dict[str, Any]], decision: str) -> str:
    if decision == "no_link" or not candidates:
        return "none"
    return _normalize_case_link_source(candidates[0].get("source"))


def _normalize_choice(
    value: Any,
    allowed: set[str] | tuple[str, ...],
    *,
    default: str,
    field_name: str,
    notes: list[dict[str, Any]] | None = None,
) -> str:
    """Normalize a free-string field onto a fixed vocabulary.

    SLICE-2A: the coercion was previously silent — the `field_name` argument was accepted at every
    call site and never used, so a value the model actually produced could be replaced by a default
    with no record anywhere. Behaviour is unchanged; when a `notes` sink is supplied, each REAL
    coercion appends bounded per-field evidence. A value that is already valid produces no note.
    """
    text = str(value or "").strip()
    if text in allowed:
        return text
    if notes is not None:
        notes.append(
            {
                "field_name": field_name,
                "raw_value": text[:120],
                "normalized_value": default,
                "reason_code": "empty_value_defaulted" if not text else "value_not_in_allowed_vocabulary",
            }
        )
    return default


def _normalize_string_list_contract(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text:
            normalized.append(text)
    return normalized


def _normalize_dict_list_contract(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in values:
        if isinstance(item, dict):
            normalized.append(dict(item))
    return normalized


def _string_or_default(value: Any, *, default: str) -> str:
    text = str(value or "").strip()
    return text or default


def _bounded_float(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, round(number, 4)))


def _normalize_confidence_scalar(value: Any, *, default: float) -> float:
    if isinstance(value, (int, float)):
        return _bounded_float(value, default=default)
    text = str(value or "").strip().lower()
    qualitative = {
        "low": 0.35,
        "medium": 0.6,
        "high": 0.85,
        "very_high": 0.92,
        "very_low": 0.2,
    }
    if text in qualitative:
        return qualitative[text]
    return _bounded_float(value, default=default)


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _count_hard_matches(reasons: list[str]) -> int:
    return sum(
        1
        for reason in reasons
        if any(token in reason for token in ("explicit", "thread", "same_thread", "shared_reference"))
    )


def normalize_candidate(
    candidate: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Apply safe structural normalization without changing business meaning."""
    data = deepcopy(candidate)
    notes: list[str] = []
    snapshot_source = (snapshot or {}).get("source_message") or {}

    if not str(data.get("schema_version") or "").strip():
        data["schema_version"] = "1.0"
        notes.append("filled_schema_version")

    top_level_thread_summary = str(data.pop("thread_summary", "") or "").strip()
    if top_level_thread_summary:
        thread_seed = data.get("thread") if isinstance(data.get("thread"), dict) else {}
        thread = _ensure_dict(data, "thread")
        if not str(thread.get("thread_summary") or "").strip():
            thread["thread_summary"] = top_level_thread_summary
            notes.append("wrapped_top_level_thread_summary")
        elif not thread_seed:
            notes.append("dropped_duplicate_top_level_thread_summary")

    source = _ensure_dict(data, "source")
    if not str(source.get("channel") or "").strip():
        source["channel"] = "gmail"
        notes.append("filled_source_channel")
    if snapshot:
        if not str(source.get("mailbox") or "").strip():
            source["mailbox"] = str(snapshot.get("mailbox") or "")
            notes.append("filled_source_mailbox")
        if not str(source.get("observed_at") or "").strip():
            source["observed_at"] = str(snapshot.get("observed_at") or "")
            notes.append("filled_source_observed_at")

    message = _ensure_dict(data, "message")
    _fill_if_missing(message, "message_id", snapshot_source.get("message_id"), notes, "filled_message_id")
    _fill_if_missing(message, "date", snapshot_source.get("date"), notes, "filled_message_date")
    _fill_if_missing(message, "sender", snapshot_source.get("sender"), notes, "filled_message_sender")
    _fill_if_missing(message, "subject", snapshot_source.get("subject"), notes, "filled_message_subject")
    _fill_if_missing(message, "snippet", snapshot_source.get("snippet"), notes, "filled_message_snippet")
    if not isinstance(message.get("to"), list):
        message["to"] = list(snapshot_source.get("to") or [])
        notes.append("filled_message_to")
    if not isinstance(message.get("cc"), list):
        message["cc"] = list(snapshot_source.get("cc") or [])
        notes.append("filled_message_cc")
    if not isinstance(message.get("labels"), list):
        message["labels"] = list(snapshot_source.get("labels") or [])
    if not isinstance(message.get("has_attachments"), bool):
        message["has_attachments"] = bool(snapshot_source.get("has_attachments"))
        notes.append("filled_message_has_attachments")

    thread = _ensure_dict(data, "thread")
    _fill_if_missing(thread, "thread_id", snapshot_source.get("thread_id"), notes, "filled_thread_id")
    _fill_if_missing(thread, "thread_position", snapshot_source.get("thread_position_hint"), notes, "filled_thread_position")
    if not str(thread.get("thread_position") or "").strip():
        thread["thread_position"] = "new_thread"
        notes.append("filled_default_thread_position")
    if not isinstance(thread.get("is_reply_or_forward"), bool):
        thread["is_reply_or_forward"] = bool(snapshot_source.get("is_reply_or_forward_hint"))
        notes.append("filled_thread_reply_flag")
    if not str(thread.get("thread_summary") or "").strip():
        thread["thread_summary"] = _default_thread_summary(snapshot=snapshot, candidate=data)
        notes.append("filled_thread_summary")
    top_level_linked_candidates = data.pop("linked_case_candidates", None)
    if isinstance(top_level_linked_candidates, list):
        current_linked = thread.get("linked_case_candidates")
        if not isinstance(current_linked, list) or not current_linked:
            thread["linked_case_candidates"] = top_level_linked_candidates
            notes.append("moved_top_level_linked_case_candidates_to_thread")
        elif top_level_linked_candidates:
            notes.append("dropped_duplicate_top_level_linked_case_candidates")
    elif top_level_linked_candidates is not None:
        notes.append("dropped_invalid_top_level_linked_case_candidates")
    if not isinstance(thread.get("linked_case_candidates"), list):
        thread["linked_case_candidates"] = []
        notes.append("filled_linked_case_candidates")

    secondary_signals = data.get("secondary_signals")
    if not isinstance(secondary_signals, list):
        data["secondary_signals"] = []
        notes.append("filled_secondary_signals")
    else:
        data["secondary_signals"] = _dedupe_signal_list(secondary_signals)

    case_assessment = _ensure_dict(data, "case_assessment")
    confidence_keys = (
        "signal_confidence",
        "case_link_confidence",
        "decision_confidence",
        "extraction_confidence",
    )
    nested_business_area = str(case_assessment.pop("business_area", "") or "").strip()
    if not str(data.get("business_area") or "").strip() and nested_business_area:
        data["business_area"] = nested_business_area
        notes.append("hoisted_case_assessment_business_area")
    elif nested_business_area:
        notes.append("dropped_duplicate_case_assessment_business_area")
    decision_seed = data.get("decision") if isinstance(data.get("decision"), dict) else {}
    nested_decision_business_area = str(decision_seed.pop("business_area", "") or "").strip()
    if not str(data.get("business_area") or "").strip() and nested_decision_business_area:
        data["business_area"] = nested_decision_business_area
        notes.append("hoisted_decision_business_area")
    elif nested_decision_business_area:
        notes.append("dropped_duplicate_decision_business_area")
    if not str(data.get("business_area") or "").strip():
        family_area_defaults = {
            "lead_opportunity": "sales",
            "finance_settlement": "finance",
            "procurement_delivery": "procurement",
            "supplier_commercial_review": "supplier_commercial",
            "platform_service_security": "security",
            "compliance_legal_review": "compliance_legal",
            "marketing_performance_review": "marketing_growth",
            "internal_coordination": "internal_coordination",
            "unknown": "general_admin",
        }
        case_family = str(case_assessment.get("case_family") or "").strip()
        inferred_area = family_area_defaults.get(case_family)
        if inferred_area:
            data["business_area"] = inferred_area
            notes.append("inferred_business_area_from_case_family")
    case_confidence_block: dict[str, Any] = {}
    for key in confidence_keys:
        if key in case_assessment:
            case_confidence_block[key] = case_assessment.pop(key)
    if case_confidence_block:
        confidence_obj = data.get("confidence") if isinstance(data.get("confidence"), dict) else {}
        for key, value in case_confidence_block.items():
            confidence_obj.setdefault(key, value)
        data["confidence"] = confidence_obj
        notes.append("hoisted_case_assessment_confidence_fields")
    if not str(case_assessment.get("state_detected") or "").strip():
        case_assessment["state_detected"] = "none"
        notes.append("filled_state_detected")
    state_change = case_assessment.get("state_change")
    if not isinstance(state_change, dict):
        case_assessment["state_change"] = {"detected": False}
        notes.append("filled_state_change")
    elif not isinstance(state_change.get("detected"), bool):
        state_change["detected"] = bool(state_change.get("detected"))
        notes.append("normalized_state_change_detected")

    decision = _ensure_dict(data, "decision")
    if decision.get("sla_hint") is None and "sla_hint" in decision:
        decision.pop("sla_hint", None)
        notes.append("removed_null_sla_hint")
    if decision.get("suggested_owner") is None and "suggested_owner" in decision:
        decision.pop("suggested_owner", None)
        notes.append("removed_null_suggested_owner")
    nested_priority = str(decision.pop("priority", "") or "").strip()
    if not str(data.get("priority") or "").strip() and nested_priority:
        data["priority"] = nested_priority
        notes.append("hoisted_decision_priority")
    elif nested_priority and str(data.get("priority") or "").strip():
        notes.append("dropped_duplicate_decision_priority")
    nested_reason = str(decision.pop("reason", "") or "").strip()
    if not str(data.get("reason") or "").strip() and nested_reason:
        data["reason"] = nested_reason
        notes.append("hoisted_decision_reason")
    elif nested_reason and str(data.get("reason") or "").strip():
        notes.append("dropped_duplicate_decision_reason")
    nested_review = decision.pop("review", None)
    if isinstance(nested_review, dict):
        existing_review = data.get("review")
        if not isinstance(existing_review, dict) or not existing_review:
            data["review"] = nested_review
            notes.append("hoisted_decision_review")
        else:
            notes.append("dropped_duplicate_decision_review")
    elif nested_review is not None:
        notes.append("dropped_invalid_decision_review")
    nested_secondary = decision.pop("secondary_signals", None)
    if not isinstance(data.get("secondary_signals"), list) and isinstance(nested_secondary, list):
        data["secondary_signals"] = nested_secondary
        notes.append("hoisted_decision_secondary_signals")
    elif nested_secondary:
        notes.append("dropped_duplicate_decision_secondary_signals")
    nested_confidence_block: dict[str, Any] = {}
    nested_confidence = decision.pop("confidence", None)
    if isinstance(nested_confidence, dict):
        for key in confidence_keys:
            if key in nested_confidence:
                nested_confidence_block[key] = nested_confidence[key]
        notes.append("hoisted_decision_confidence_object")
    elif nested_confidence is not None:
        notes.append("dropped_invalid_decision_confidence_object")
    for key in confidence_keys:
        if key in decision:
            nested_confidence_block[key] = decision.pop(key)
    if nested_confidence_block:
        confidence_obj = data.get("confidence") if isinstance(data.get("confidence"), dict) else {}
        for key, value in nested_confidence_block.items():
            confidence_obj.setdefault(key, value)
        data["confidence"] = confidence_obj
        notes.append("hoisted_decision_confidence_fields")
    if not isinstance(data.get("confidence"), dict):
        hoisted_confidence: dict[str, Any] = {}
        for key in confidence_keys:
            if key in data:
                hoisted_confidence[key] = data.pop(key)
        if hoisted_confidence:
            data["confidence"] = hoisted_confidence
            notes.append("wrapped_top_level_confidence_fields")
    if not str(data.get("reason") or "").strip():
        thread_summary = str(thread.get("thread_summary") or "").strip()
        if thread_summary:
            data["reason"] = f"Model omitted reason; thread summary: {thread_summary}"
            notes.append("filled_reason_from_thread_summary")
    if not str(decision.get("action_rationale") or "").strip():
        reason_text = str(data.get("reason") or "").strip()
        if reason_text:
            decision["action_rationale"] = reason_text
            notes.append("filled_action_rationale_from_reason")
    if not isinstance(case_assessment.get("is_new_case"), bool):
        action = str(decision.get("action") or "").strip()
        if action in NEW_CASE_ACTIONS:
            case_assessment["is_new_case"] = True
            notes.append("filled_is_new_case_from_create_action")
        elif action in UPDATE_ACTIONS or action in REFERENCE_ONLY_ACTIONS:
            case_assessment["is_new_case"] = False
            notes.append("filled_is_new_case_false_for_update_or_reference")
        else:
            linked = thread.get("linked_case_candidates") or []
            case_assessment["is_new_case"] = not bool(linked)
            notes.append("filled_is_new_case_from_link_candidates")
    if not str(data.get("priority") or "").strip():
        data["priority"] = "medium"
        notes.append("filled_default_priority")
    primary_signal = _ensure_dict(data, "primary_signal")
    if not str(primary_signal.get("code") or "").strip():
        case_family = str(case_assessment.get("case_family") or "").strip()
        business_area = str(data.get("business_area") or "").strip()
        thread_summary = str(thread.get("thread_summary") or "").strip()
        reason_text = str(data.get("reason") or "").strip()
        if case_family == "lead_opportunity" and business_area == "sales":
            primary_signal.update(
                {
                    "code": "lead_inquiry",
                    "name": "Lead inquiry",
                    "description": reason_text or thread_summary or "Incoming sales lead inquiry.",
                    "business_significance": "New business opportunity requiring sales follow-up.",
                }
            )
            notes.append("inferred_primary_signal_lead_inquiry")
    confidence = data.get("confidence")
    action_for_confidence = str(decision.get("action") or "").strip()
    default_signal = 0.72 if action_for_confidence in NEW_CASE_ACTIONS else 0.6
    default_decision = 0.72 if action_for_confidence in NEW_CASE_ACTIONS else 0.55
    if not isinstance(confidence, dict):
        data["confidence"] = {
            "signal_confidence": default_signal,
            "case_link_confidence": 0.0,
            "decision_confidence": default_decision,
            "extraction_confidence": 0.5,
        }
        notes.append("filled_default_confidence")
    else:
        for field, default in (
            ("signal_confidence", default_signal),
            ("case_link_confidence", 0.0),
            ("decision_confidence", default_decision),
            ("extraction_confidence", 0.5),
        ):
            try:
                float(confidence[field])
            except (KeyError, TypeError, ValueError):
                confidence[field] = _normalize_confidence_scalar(confidence.get(field), default=default)
                notes.append(f"filled_confidence_{field}")
            else:
                confidence[field] = _normalize_confidence_scalar(confidence.get(field), default=default)

    case_link_default = 0.0
    confidence_after = data.get("confidence") if isinstance(data.get("confidence"), dict) else {}
    if isinstance(confidence_after, dict):
        case_link_default = _normalize_confidence_scalar(
            confidence_after.get("case_link_confidence"),
            default=0.0,
        )
    thread["linked_case_candidates"] = _normalize_linked_case_candidates(
        thread.get("linked_case_candidates"),
        default_match_confidence=case_link_default,
        notes=notes,
    )

    review = _ensure_dict(data, "review")
    if not isinstance(review.get("flags"), list):
        review["flags"] = []
        notes.append("filled_review_flags")
    review["flags"] = _normalize_review_flags(review.get("flags") or [])
    if not isinstance(review.get("required"), bool):
        review["required"] = bool(review["flags"])
        notes.append("filled_review_required")

    if not str(data.get("reason") or "").strip():
        fallback_reason = str(decision.get("action_rationale") or "").strip()
        if fallback_reason:
            data["reason"] = fallback_reason
            notes.append("filled_reason_from_action_rationale")

    extracted = _ensure_dict(data, "extracted_data")
    required_extracted_keys = ("entities", "dates", "amounts", "references", "deadlines")
    if not all(key in extracted for key in required_extracted_keys):
        flat_keys = [key for key in list(extracted.keys()) if key not in required_extracted_keys and key != "lead_details"]
        if flat_keys:
            lead_details = extracted.get("lead_details") if isinstance(extracted.get("lead_details"), dict) else {}
            for key in flat_keys:
                lead_details[key] = extracted.pop(key)
            if "approximate_area_m2" in lead_details and "floor_area_m2" not in lead_details:
                lead_details["floor_area_m2"] = lead_details.pop("approximate_area_m2")
                notes.append("mapped_approximate_area_m2_to_floor_area_m2")
            if "location" in lead_details and "city" not in lead_details:
                lead_details["city"] = lead_details.pop("location")
                notes.append("mapped_location_to_city")
            entities = _ensure_dict(extracted, "entities")
            for key in ("people", "organizations", "locations", "products"):
                if not isinstance(entities.get(key), list):
                    entities[key] = []
            if "contact_email" in lead_details:
                entities["people"].append(str(lead_details.pop("contact_email")))
                notes.append("mapped_contact_email_to_entities_people")
            if "contact_phone" in lead_details:
                entities["people"].append(str(lead_details.pop("contact_phone")))
                notes.append("mapped_contact_phone_to_entities_people")
            if "scope" in lead_details:
                entities["products"].append(str(lead_details.pop("scope")))
                notes.append("mapped_scope_to_entities_products")
            allowed_lead_details = {
                "property_type",
                "floor_area_m2",
                "city",
                "county",
                "inquiry_source",
            }
            pruned_lead_details = {
                key: value
                for key, value in lead_details.items()
                if key in allowed_lead_details and value is not None
            }
            if pruned_lead_details != lead_details:
                notes.append("pruned_unknown_lead_details_fields")
            extracted["lead_details"] = pruned_lead_details
            notes.append("wrapped_flat_extracted_data_into_lead_details")
    for key in ("amounts", "dates", "deadlines"):
        if not isinstance(extracted.get(key), list):
            extracted[key] = []
            notes.append(f"filled_{key}")
    entities = _ensure_dict(extracted, "entities")
    for key in ("people", "organizations", "locations", "products"):
        if not isinstance(entities.get(key), list):
            entities[key] = []
    references = _ensure_dict(extracted, "references")
    for key in ("invoice_numbers", "shipment_numbers", "order_numbers", "transaction_numbers", "case_ids"):
        if not isinstance(references.get(key), list):
            references[key] = []
    if not isinstance(extracted.get("lead_details"), dict):
        extracted["lead_details"] = {}
        notes.append("filled_lead_details")

    return data, sorted(set(notes))


def _clamp_decision_confidence_to_signal_ceiling(
    data: dict[str, Any],
    *,
    notes: list[str] | None = None,
) -> None:
    """Lower decision_confidence so it satisfies the signal ceiling rule; never raises signal_confidence."""
    confidence = data.get("confidence")
    if not isinstance(confidence, dict):
        return
    try:
        signal_value = float(confidence["signal_confidence"])
        decision_value = float(confidence["decision_confidence"])
    except (KeyError, TypeError, ValueError):
        return
    if not math.isfinite(signal_value) or not math.isfinite(decision_value):
        return
    ceiling = signal_value + DECISION_OVER_SIGNAL_MARGIN
    if decision_value > ceiling:
        confidence["decision_confidence"] = min(decision_value, ceiling)
        if notes is not None:
            notes.append("clamped_decision_confidence_to_signal_plus_margin")


def attempt_safe_repair(
    candidate: dict[str, Any],
    *,
    errors: list[str],
    snapshot: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Conservatively repair invalid outputs while preserving the original action when possible."""
    repaired = deepcopy(candidate)
    notes: list[str] = []

    decision = _ensure_dict(repaired, "decision")
    review = _ensure_dict(repaired, "review")
    case_assessment = _ensure_dict(repaired, "case_assessment")
    primary_signal = _ensure_dict(repaired, "primary_signal")
    synthesized_primary_signal = False
    if not all(
        (
            str(primary_signal.get("code") or "").strip(),
            str(primary_signal.get("name") or "").strip(),
            str(primary_signal.get("description") or "").strip(),
            str(primary_signal.get("business_significance") or "").strip(),
        )
    ):
        primary_signal.update(
            {
                "code": str(primary_signal.get("code") or "").strip() or "manual_review_required",
                "name": str(primary_signal.get("name") or "").strip() or "Manual review required",
                "description": (
                    str(primary_signal.get("description") or "").strip()
                    or "Original model output omitted or damaged the primary signal, so the message was downgraded to manual review."
                ),
                "business_significance": (
                    str(primary_signal.get("business_significance") or "").strip()
                    or "Potential operational meaning exists, but an operator must confirm the signal before automation."
                ),
            }
        )
        notes.append("filled_primary_signal_for_review")
        synthesized_primary_signal = True
    essential_present = all(
        (
            str(repaired.get("business_area") or "").strip(),
            str(repaired.get("priority") or "").strip(),
            str(case_assessment.get("case_family") or "").strip(),
            str(primary_signal.get("code") or "").strip(),
            str(primary_signal.get("name") or "").strip(),
        )
    )
    if not essential_present:
        return None, []

    _clamp_decision_confidence_to_signal_ceiling(repaired, notes=notes)

    validation_schema = schema or load_intake_schema()
    minimal_result = _validate_candidate_data(repaired, validation_schema)
    if minimal_result.is_valid and not synthesized_primary_signal:
        return repaired, notes

    repair_flags = set(_normalize_review_flags(review.get("flags") or []))
    repair_flags.update(_derive_repair_flags(candidate, errors=errors, snapshot=snapshot))
    if synthesized_primary_signal or repair_flags:
        reviewed = deepcopy(repaired)
        reviewed_review = _ensure_dict(reviewed, "review")
        reviewed_review["required"] = True
        if not repair_flags:
            repair_flags.add("ambiguous_signal")
        reviewed_review["flags"] = sorted(repair_flags)
        reviewed_result = _validate_candidate_data(reviewed, validation_schema)
        if reviewed_result.is_valid:
            return reviewed, notes
        repaired = reviewed

    fallback_review = deepcopy(repaired)
    fallback_decision = _ensure_dict(fallback_review, "decision")
    fallback_review_block = _ensure_dict(fallback_review, "review")
    fallback_review_block["required"] = True
    fallback_flags = set(_normalize_review_flags(fallback_review_block.get("flags") or []))
    fallback_flags.update(_derive_repair_flags(candidate, errors=errors, snapshot=snapshot))
    if not fallback_flags:
        fallback_flags.add("ambiguous_signal")
    fallback_review_block["flags"] = sorted(fallback_flags)

    if str(fallback_decision.get("action") or "").strip() != "review":
        notes.append("downgraded_action_to_review")
    fallback_decision["action"] = "review"
    action_rationale = str(fallback_decision.get("action_rationale") or "").strip()
    if action_rationale:
        fallback_decision["action_rationale"] = (
            f"{action_rationale} Safe repair: downgraded to review because the original output was invalid."
        )
    else:
        fallback_decision["action_rationale"] = (
            "Safe repair: downgraded to review because the original output was invalid."
        )
        notes.append("filled_action_rationale_for_review")
    if not str(fallback_review.get("reason") or "").strip():
        fallback_review["reason"] = "Review required because the model output was incomplete or unsafe for automation."
        notes.append("filled_reason_for_review")

    fallback_result = _validate_candidate_data(fallback_review, validation_schema)
    if fallback_result.is_valid:
        return fallback_review, notes
    return fallback_review, notes


def apply_contextual_guards(
    data: dict[str, Any],
    *,
    snapshot: dict[str, Any],
    schema: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Downgrade unsafe-but-schema-valid outputs to review when snapshot context is too weak."""
    guarded = deepcopy(data)
    flags: list[str] = []

    action = str(guarded["decision"]["action"])
    review_required = bool(guarded["review"]["required"])
    review_flags = set(guarded["review"]["flags"])
    linked_candidates = guarded["thread"].get("linked_case_candidates") or []
    top_candidate_confidence = top_case_candidate_confidence(linked_candidates)
    thread_quality = str(snapshot.get("thread_context_quality") or "weak")
    thread_position = str(guarded["thread"].get("thread_position") or "unknown")
    is_reply_or_forward = bool(guarded["thread"].get("is_reply_or_forward"))
    routing_hints = snapshot.get("routing_hints") or {}
    self_forward = bool(routing_hints.get("self_forward"))
    invoice_refs = guarded["extracted_data"]["references"].get("invoice_numbers") or []
    amounts = guarded["extracted_data"].get("amounts") or []
    deadlines = guarded["extracted_data"].get("deadlines") or []

    if action in UPDATE_ACTIONS and thread_quality != "strong":
        flags.append("insufficient_thread_context")
    if (
        action in ACTION_BEARING_DECISIONS
        and thread_quality == "weak"
        and (is_reply_or_forward or thread_position in {"reply", "forward"})
    ):
        flags.append("insufficient_thread_context")
    if action in NEW_CASE_ACTIONS and top_candidate_confidence >= POSSIBLE_EXISTING_CASE_THRESHOLD and not review_required:
        flags.append("possible_existing_case_but_no_match")
    if self_forward and action in ACTION_BEARING_DECISIONS and top_candidate_confidence < SELF_FORWARD_REQUIRES_STRONG_MATCH:
        flags.append("self_forward_requires_meaning_inference")
    if invoice_refs and action in {"create_case", "create_case_and_task", "ignore"} and not (amounts or deadlines):
        flags.append("financial_document_without_payable_context")

    merged_flags = sorted(review_flags.union(flags))
    if flags:
        guarded["review"]["required"] = True
        guarded["review"]["flags"] = merged_flags
        guarded["decision"]["action"] = "review"
        rationale = str(guarded["decision"].get("action_rationale") or "").strip()
        guard_reason = ", ".join(flags)
        guarded["decision"]["action_rationale"] = (
            f"{rationale} Guardrail override: downgraded to review because {guard_reason}."
            if rationale
            else f"Guardrail override: downgraded to review because {guard_reason}."
        )

    _clamp_decision_confidence_to_signal_ceiling(guarded, notes=None)

    validation = _validate_semantics(guarded)
    if validation:
        joined = "; ".join(validation)
        raise GroqClientError(f"Contextual guard produced an invalid output: {joined}")

    if schema is not None:
        schema_errors = _validate_schema(guarded, schema)
        if schema_errors:
            joined = "; ".join(schema_errors)
            raise GroqClientError(f"Contextual guard produced a schema-invalid output: {joined}")

    return guarded, flags


def _parse_and_validate_raw(raw_text: str, schema: dict[str, Any]) -> ValidationResult:
    try:
        data = json.loads(extract_json_candidate(raw_text))
    except json.JSONDecodeError as exc:
        return ValidationResult(
            is_valid=False,
            parse_ok=False,
            schema_ok=False,
            semantic_ok=False,
            errors=[f"Invalid JSON: {exc}"],
            data=None,
        )

    if not isinstance(data, dict):
        return ValidationResult(
            is_valid=False,
            parse_ok=True,
            schema_ok=False,
            semantic_ok=False,
            errors=["Top-level JSON value must be an object."],
            data=None,
        )

    return _validate_candidate_data(data, schema)


def _validate_candidate_data(data: dict[str, Any], schema: dict[str, Any]) -> ValidationResult:
    schema_errors = _validate_schema(data, schema)
    if schema_errors:
        return ValidationResult(
            is_valid=False,
            parse_ok=True,
            schema_ok=False,
            semantic_ok=False,
            errors=schema_errors,
            data=data,
        )

    semantic_errors = _validate_semantics(data)
    return ValidationResult(
        is_valid=not semantic_errors,
        parse_ok=True,
        schema_ok=True,
        semantic_ok=not semantic_errors,
        errors=semantic_errors,
        data=data,
    )


def _ensure_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if isinstance(value, dict):
        return value
    payload[key] = {}
    return payload[key]


def _fill_if_missing(
    payload: dict[str, Any],
    key: str,
    value: Any,
    notes: list[str],
    note: str,
) -> None:
    if str(payload.get(key) or "").strip():
        return
    if value is None:
        return
    payload[key] = value
    notes.append(note)


def _default_thread_summary(*, snapshot: dict[str, Any] | None, candidate: dict[str, Any]) -> str:
    message = candidate.get("message") if isinstance(candidate.get("message"), dict) else {}
    subject = str(message.get("subject") or "").strip()
    if subject:
        return subject
    if snapshot:
        source_message = snapshot.get("source_message") if isinstance(snapshot.get("source_message"), dict) else {}
        subject = str(source_message.get("subject") or source_message.get("normalized_subject") or "").strip()
        if subject:
            return subject
    return ""


def _dedupe_signal_list(signals: list[Any]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        code = str(signal.get("code") or "").strip()
        if not code or code in seen_codes:
            continue
        name = str(signal.get("name") or "").strip()
        deduped.append({"code": code, "name": name})
        seen_codes.add(code)
    return deduped


def _normalize_linked_case_candidates(
    values: Any,
    *,
    default_match_confidence: float,
    notes: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            notes.append("dropped_invalid_linked_case_candidate")
            continue
        case_key = str(item.get("case_key") or "").strip()
        if not case_key:
            notes.append("dropped_linked_case_candidate_without_case_key")
            continue
        case_type = str(item.get("case_type") or "").strip()
        if not case_type:
            case_type = "thread_context" if case_key.startswith("thread:") else "unknown"
        match_confidence = _normalize_confidence_scalar(
            item.get("match_confidence"),
            default=default_match_confidence,
        )
        clean = {
            "case_key": case_key,
            "case_type": case_type,
            "match_confidence": match_confidence,
        }
        if item != clean:
            notes.append("normalized_linked_case_candidate_contract")
        normalized.append(clean)
    return normalized


def _normalize_review_flags(flags: list[Any]) -> list[str]:
    allowed = set(REVIEW_FLAGS)
    normalized = {
        str(item).strip()
        for item in flags
        if str(item).strip() and str(item).strip() in allowed
    }
    return sorted(normalized)


def _derive_repair_flags(
    candidate: dict[str, Any],
    *,
    errors: list[str],
    snapshot: dict[str, Any] | None,
) -> set[str]:
    flags: set[str] = set()
    extracted = candidate.get("extracted_data") if isinstance(candidate.get("extracted_data"), dict) else {}
    deadlines = extracted.get("deadlines") if isinstance(extracted.get("deadlines"), list) else []
    amounts = extracted.get("amounts") if isinstance(extracted.get("amounts"), list) else []
    references = extracted.get("references") if isinstance(extracted.get("references"), dict) else {}
    if deadlines:
        flags.add("deadline_found_without_owner")
    if amounts or references.get("invoice_numbers"):
        flags.add("financial_document_without_payable_context")
    if snapshot and str(snapshot.get("thread_context_quality") or "weak") != "strong":
        flags.add("insufficient_thread_context")
    if any("security" in error.lower() for error in errors):
        flags.add("security_or_platform_risk")
    if any("legal" in error.lower() or "compliance" in error.lower() for error in errors):
        flags.add("legal_or_compliance_risk")
    if not flags and errors:
        flags.add("ambiguous_signal")
    return flags


def _validate_schema(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    if Draft202012Validator is None:
        return [
            "Missing optional dependency `jsonschema`. Install tools/gmail_audit requirements before running intake."
        ]

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda item: list(item.path))
    return [_format_schema_error(error) for error in errors]


def _format_schema_error(error: Any) -> str:
    path = ".".join(str(part) for part in error.path)
    if path:
        return f"{path}: {error.message}"
    return error.message


def _validate_semantics(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    action = str(data["decision"]["action"])
    priority = str(data["priority"])
    business_area = str(data["business_area"])
    case_family = str(data["case_assessment"]["case_family"])
    is_new_case = bool(data["case_assessment"]["is_new_case"])
    state_change = data["case_assessment"]["state_change"]
    state_change_detected = bool(state_change["detected"])
    review_required = bool(data["review"]["required"])
    review_flags = set(data["review"]["flags"])
    signal_confidence = float(data["confidence"]["signal_confidence"])
    case_link_confidence = float(data["confidence"]["case_link_confidence"])
    decision_confidence = float(data["confidence"]["decision_confidence"])
    extraction_confidence = float(data["confidence"]["extraction_confidence"])
    deadlines = data["extracted_data"]["deadlines"]
    amounts = data["extracted_data"]["amounts"]
    references = data["extracted_data"]["references"]
    lead_details = data["extracted_data"].get("lead_details") or {}
    linked_candidates = data["thread"].get("linked_case_candidates") or []
    primary_signal_code = str(data["primary_signal"]["code"])
    secondary_signal_codes = [str(item["code"]) for item in data["secondary_signals"]]
    has_attachments = bool((data.get("message") or {}).get("has_attachments"))

    has_deadlines = bool(deadlines)
    has_amounts = bool(amounts)
    has_references = _has_references(references)
    has_invoice_refs = bool(references.get("invoice_numbers"))
    has_shipment_refs = bool(references.get("shipment_numbers"))
    has_case_ids = bool(references.get("case_ids"))
    has_lead_details = bool(lead_details)
    top_candidate_confidence = top_case_candidate_confidence(linked_candidates)

    if len(secondary_signal_codes) != len(set(secondary_signal_codes)):
        errors.append("secondary_signals contains duplicate codes.")
    if primary_signal_code in secondary_signal_codes:
        errors.append("primary_signal must not be duplicated in secondary_signals.")

    if action == "review" and not review_required:
        errors.append("decision.action=review requires review.required=true.")
    if review_required and not review_flags:
        errors.append("review.required=true requires at least one review flag.")
    if review_flags and not review_required:
        errors.append("review.flags may only be present when review.required=true.")
    if FORCED_REVIEW_FLAGS & review_flags and not review_required:
        errors.append("Forced-review flags require review.required=true.")

    if review_required and action in {"mark_reference", "mark_watchlist", "ignore"}:
        errors.append(f"{action} cannot be used when review.required=true.")

    if decision_confidence < LOW_DECISION_CONFIDENCE_REQUIRES_REVIEW and not review_required:
        errors.append(
            f"decision_confidence below {LOW_DECISION_CONFIDENCE_REQUIRES_REVIEW:.2f} requires review."
        )
    if action in HIGH_RISK_ACTIONS and decision_confidence < HIGH_RISK_ACTION_MIN_DECISION_CONFIDENCE and not review_required:
        errors.append(
            f"{action} requires decision_confidence >= {HIGH_RISK_ACTION_MIN_DECISION_CONFIDENCE:.2f} or review."
        )
    if action in REFERENCE_ONLY_ACTIONS and decision_confidence < REFERENCE_ACTION_MIN_DECISION_CONFIDENCE and not review_required:
        errors.append(
            f"{action} requires decision_confidence >= {REFERENCE_ACTION_MIN_DECISION_CONFIDENCE:.2f} or review."
        )
    if decision_confidence > signal_confidence + DECISION_OVER_SIGNAL_MARGIN:
        errors.append("decision_confidence is implausibly higher than signal_confidence.")
    if extraction_confidence < EXTRACTION_CONFIDENCE_REQUIRES_REVIEW and (has_deadlines or has_amounts or has_references) and not review_required:
        errors.append("Weak extraction_confidence with structured facts requires review.")

    if action == "ignore":
        if review_required:
            errors.append("ignore cannot coexist with review.required=true.")
        if deadlines:
            errors.append("ignore is not allowed when deadlines are present.")
        if amounts:
            errors.append("ignore is not allowed when monetary amounts are present.")
        if has_references:
            errors.append("ignore is not allowed when business references are present.")
        if has_lead_details:
            errors.append("ignore is not allowed when lead details are present.")
        if business_area in HIGH_RISK_AREAS:
            errors.append("ignore is unsafe for security or compliance/legal business areas.")
        if priority != "low":
            errors.append("ignore requires low priority.")
        if case_family != "unknown":
            errors.append("ignore should not be used when a concrete case_family was identified.")

    if action in {"append_to_existing_case", "update_case_state"}:
        if is_new_case:
            errors.append(f"{action} is inconsistent with case_assessment.is_new_case=true.")
        if not linked_candidates:
            errors.append(f"{action} requires at least one linked_case_candidate.")
        if case_link_confidence < POSSIBLE_EXISTING_CASE_THRESHOLD:
            errors.append(
                f"{action} requires case_link_confidence >= {POSSIBLE_EXISTING_CASE_THRESHOLD:.2f}."
            )
        if top_candidate_confidence < POSSIBLE_EXISTING_CASE_THRESHOLD:
            errors.append(
                f"{action} requires linked_case_candidate.match_confidence >= {POSSIBLE_EXISTING_CASE_THRESHOLD:.2f}."
            )

    if action == "update_case_state":
        if not state_change_detected:
            errors.append("update_case_state requires state_change.detected=true.")
        if not str(state_change.get("to_state") or "").strip():
            errors.append("update_case_state requires state_change.to_state.")

    if action == "append_to_existing_case" and state_change_detected and not review_required:
        errors.append("append_to_existing_case should not be used when a concrete state change was detected.")

    if action in {"create_case", "create_case_and_task"}:
        if not is_new_case:
            errors.append(f"{action} is inconsistent with case_assessment.is_new_case=false.")
        if top_candidate_confidence >= STRONG_EXISTING_CASE_THRESHOLD and not review_required:
            errors.append(f"{action} is unsafe when a strong linked_case_candidate already exists.")

    if action == "mark_reference":
        if business_area in HIGH_RISK_AREAS:
            errors.append("mark_reference is unsafe for security or compliance/legal business areas.")
        if has_deadlines or has_amounts or state_change_detected or has_lead_details:
            errors.append("mark_reference cannot hide deadlines, money, state changes, or lead details.")
        if priority in {"critical", "high"}:
            errors.append("mark_reference cannot carry high or critical priority.")

    if action == "mark_watchlist":
        if business_area in HIGH_RISK_AREAS:
            errors.append("mark_watchlist is unsafe for security or compliance/legal business areas.")
        if state_change_detected:
            errors.append("mark_watchlist cannot replace a real state-change decision.")
        if has_lead_details:
            errors.append("mark_watchlist cannot be used for plausible lead data.")
        if has_deadlines and priority in {"critical", "high"}:
            errors.append("mark_watchlist is unsafe for urgent deadline-bearing messages.")

    if action == "create_task" and has_case_ids and case_link_confidence >= CREATE_TASK_CASE_LINK_SUSPICIOUS_THRESHOLD and not review_required:
        errors.append("create_task is suspicious when the message already points strongly to an existing case.")

    if business_area == "logistics" and state_change_detected and action in {"mark_reference", "mark_watchlist", "ignore"}:
        errors.append("Logistics state changes cannot be buried as reference, watchlist, or ignore.")

    if "deadline_found_without_owner" in review_flags and not has_deadlines:
        errors.append("deadline_found_without_owner review flag requires at least one deadline.")
    # STRUCTURED-INPUT-AND-CAPABILITY-BASELINE-CLOSEOUT-01 — Phase 4 fix. The flag's own
    # name means "a financial document IS present, but payable context (amounts/invoice
    # numbers) could NOT be extracted from it" -- so requiring has_amounts/has_invoice_refs
    # as its evidence was self-contradictory: those fields represent exactly the payable
    # context the flag says is MISSING. A real financial-document attachment (e.g. an
    # invoice the model cannot read line items from) legitimately has this flag with BOTH
    # fields empty. The correct evidence is that a financial document was actually received:
    # an attachment present AND business_area=finance together (unchanged, backward-
    # compatible: amounts/invoice_refs already extracted remain independently sufficient).
    # Adversarial review: `has_attachments OR business_area=="finance"` ALONE was too broad
    # (e.g. a marketing_growth case with an unrelated PDF attached would have passed). Every
    # live re-run of DOC-01/MI-04 that legitimately carried this flag had BOTH conditions
    # true simultaneously, so requiring both together still fully covers the real evidence
    # while rejecting the marketing-case counter-example.
    if "financial_document_without_payable_context" in review_flags and not (
        has_amounts or has_invoice_refs or (has_attachments and business_area == "finance")
    ):
        errors.append("financial_document_without_payable_context requires evidence of a financial document.")
    if "supplier_mail_may_be_noise_or_opportunity" in review_flags and business_area != "supplier_commercial":
        errors.append("supplier_mail_may_be_noise_or_opportunity must stay in supplier_commercial area.")
    if "legal_or_compliance_risk" in review_flags and business_area != "compliance_legal":
        errors.append("legal_or_compliance_risk should use business_area=compliance_legal.")
    if "security_or_platform_risk" in review_flags and business_area != "security":
        errors.append("security_or_platform_risk should use business_area=security.")
    if "possible_existing_case_but_no_match" in review_flags and action in {"append_to_existing_case", "update_case_state"}:
        errors.append("possible_existing_case_but_no_match conflicts with append/update actions.")
    if "insufficient_thread_context" in review_flags and action in {"append_to_existing_case", "update_case_state"}:
        errors.append("insufficient_thread_context conflicts with append/update actions.")

    if has_shipment_refs and action == "ignore":
        errors.append("Shipment references must never be ignored.")
    if has_invoice_refs and action == "ignore":
        errors.append("Invoice references must never be ignored.")
    if business_area == "sales" and has_lead_details and action in {"mark_reference", "mark_watchlist", "ignore"}:
        errors.append("Lead-like messages cannot be buried as reference/watchlist/ignore without review.")

    return errors


def _has_references(references: dict[str, Any]) -> bool:
    for value in references.values():
        if isinstance(value, list) and value:
            return True
    return False
