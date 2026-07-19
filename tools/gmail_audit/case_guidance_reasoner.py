"""Rich Case Guidance Layer v1 — case-level interpretation (suggestion-only, Python-owned)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from case_intelligence import _normalize_case_guidance
from config import Settings
from daszek_client import DaszekClientError
from central_llm_stage import run_central_structured_stage
from groq_client import GroqClientError, extract_json_candidate
from intake_payload import load_prompt_text
from llm_contracts.case_guidance import CaseGuidanceResult
from log_config import get_logger

logger = get_logger("case_guidance_reasoner")

_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "case_guidance_v1.json"


def load_case_guidance_schema() -> dict[str, Any]:
    """Load JSON Schema for structured Groq output."""
    raw = _SCHEMA_PATH.read_text(encoding="utf-8")
    return json.loads(raw)


def build_case_guidance_prompt_input(
    *,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
    base_intelligence: dict[str, Any],
    attachment_intelligence: dict[str, Any] | None,
    thread_memory: dict[str, Any] | None,
    remote_state_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble bounded case-level context for the guidance LLM."""
    message = snapshot.get("source_message") or {}
    case_assessment = intake_result.get("case_assessment") or {}
    cu = base_intelligence.get("case_understanding") or {}
    missing_info = base_intelligence.get("missing_info") or {}
    risk_assessment = base_intelligence.get("risk_assessment") or {}
    nba = base_intelligence.get("next_best_action") or {}
    primary = nba.get("primary_next_action") or {}
    desk = base_intelligence.get("desk_composition") or {}
    lifecycle_revision = base_intelligence.get("lifecycle_revision") or {}
    merge_split = base_intelligence.get("merge_split_suggestions") or {}
    att = attachment_intelligence or {}
    tm = thread_memory or {}

    merge_hints = int(len(merge_split.get("merge_candidates") or []) + len(merge_split.get("split_suspicions") or []))

    att_types: list[str] = []
    for row in (att.get("attachments") or [])[:5]:
        if isinstance(row, dict) and row.get("business_type"):
            att_types.append(str(row.get("business_type")))

    message_context = {
        "subject": str(message.get("subject") or ""),
        "from": str(message.get("sender") or ""),
        "date": str(message.get("date") or ""),
        "intake_summary_short": str(intake_result.get("reason") or "")[:800],
        "priority": str(intake_result.get("priority") or ""),
        "decision_action": str((intake_result.get("decision") or {}).get("action") or ""),
        "business_area": str(intake_result.get("business_area") or ""),
        "case_assessment_summary": {
            "case_family": str(case_assessment.get("case_family") or ""),
            "state_detected": str(case_assessment.get("state_detected") or ""),
            "is_new_case": bool(case_assessment.get("is_new_case")),
        },
    }

    case_link_context = {
        "decision": str(case_link_result.get("decision") or ""),
        "selected_case_key": str(case_link_result.get("selected_case_key") or ""),
        "confidence": float(case_link_result.get("confidence") or 0.0),
        "merge_split_hints_count": merge_hints,
    }

    base_case_intelligence = {
        "case_understanding": {
            "summary_short": str(cu.get("summary_short") or "")[:600],
            "summary_operator": str(cu.get("summary_operator") or "")[:900],
            "latest_meaningful_change": str(cu.get("latest_meaningful_change") or "")[:600],
            "attention_reason": str(cu.get("attention_reason") or "")[:600],
            "blockers": list(cu.get("blockers") or [])[:12],
            "review_required": bool(cu.get("review_required")),
        },
        "missing_info": {
            "summary_pl": str(missing_info.get("summary_pl") or "")[:800],
            "items_preview": {
                "critical": list(missing_info.get("critical") or [])[:8],
                "important": list(missing_info.get("important") or [])[:8],
                "helpful": list(missing_info.get("helpful") or [])[:8],
            },
        },
        "risk_assessment": {
            "summary_pl": str(risk_assessment.get("summary_pl") or "")[:800],
            "risks": [
                {
                    "risk_type": str(r.get("risk_type") or ""),
                    "severity": str(r.get("severity") or ""),
                    "reason_pl": str(r.get("reason_pl") or "")[:400],
                }
                for r in (risk_assessment.get("risks") or [])[:6]
                if isinstance(r, dict)
            ],
        },
        "next_best_action": {
            "primary_next_action": {
                "action_type": str(primary.get("action_type") or ""),
                "title_pl": str(primary.get("title_pl") or "")[:300],
                "reason_pl": str(primary.get("reason_pl") or "")[:500],
            }
        },
        "desk_composition": {
            "surface_zone": str(desk.get("surface_zone") or ""),
            "presence_mode": str(desk.get("presence_mode") or ""),
            "day_bucket": str(desk.get("day_bucket") or ""),
        },
        "lifecycle_revision": {
            "lifecycle_intent": str(lifecycle_revision.get("lifecycle_intent") or ""),
        },
    }

    thread_context = {
        "canonical_thread_summary": str(tm.get("canonical_thread_summary") or "")[:1200],
        "unresolved_questions": list(tm.get("unresolved_questions") or [])[:12],
        "last_decision": str(tm.get("last_decision") or "")[:600],
        "commitments_made": list(tm.get("commitments_made") or [])[:12],
        "key_facts_so_far": list(tm.get("key_facts_so_far") or [])[:16],
        "thread_state": str(tm.get("thread_state") or ""),
        "message_count": int(tm.get("message_count") or 0),
    }

    combined_flags = list(att.get("combined_risk_flags") or [])[:12]
    attachment_context = {
        "summary_pl": str(att.get("summary_pl") or "")[:900],
        "has_significant_attachments": bool(att.get("has_significant_attachments")),
        "attachment_count": len(att.get("attachments") or []) if isinstance(att.get("attachments"), list) else 0,
        "top_attachment_business_types": att_types[:8],
        "combined_risk_flags": combined_flags,
    }

    display_contract = {
        "guidance_role": "case_guidance opisuje stan sprawy i sens operacyjny; nie zastępuje primary_next_action.",
        "do_not_emit_execution_plan": True,
        "primary_next_action_channel_is_separate": True,
    }

    return {
        "message_context": message_context,
        "case_link_context": case_link_context,
        "base_case_intelligence": base_case_intelligence,
        "thread_context": thread_context,
        "attachment_context": attachment_context,
        "remote_state_context": remote_state_context if isinstance(remote_state_context, dict) else {},
        "display_contract": display_contract,
    }


def fetch_remote_state_for_guidance(
    client: Any | None,
    *,
    settings: Settings,
    case_id_hint: str,
    desk_note_id_hint: str,
    remote_enabled: bool,
) -> tuple[dict[str, Any], dict[str, bool]]:
    """Read-only Daszek hydration for guidance context. Never raises."""
    flags = {"used_remote_case_state": False, "used_remote_note_state": False}
    out: dict[str, Any] = {
        "current_note_state": {},
        "remote_case_detail": {},
        "remote_note_detail": {},
        "feedback_state": {},
        "latest_provenance": {},
    }
    if not remote_enabled or client is None:
        return out, flags
    cid = str(case_id_hint or "").strip()
    nid = str(desk_note_id_hint or "").strip()
    try:
        if cid:
            detail = client.get_v2_case_detail(cid)
            if isinstance(detail, dict) and detail.get("ok", True) is not False:
                case_payload = detail.get("case") if isinstance(detail.get("case"), dict) else detail
                out["remote_case_detail"] = _bounded_remote_case(case_payload)
                out["feedback_state"] = _pick_feedback(case_payload)
                out["latest_provenance"] = _pick_provenance(case_payload)
                flags["used_remote_case_state"] = True
    except (DaszekClientError, OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning("Failed to fetch remote state for guidance (best-effort)", extra={"x": {
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
        }})
    try:
        if nid:
            nd = client.get_v2_note_detail(nid)
            if isinstance(nd, dict) and nd.get("ok", True) is not False:
                note = nd.get("note") if isinstance(nd.get("note"), dict) else {}
                out["remote_note_detail"] = _bounded_remote_note(note)
                out["current_note_state"] = {
                    "desk_note_id": nid,
                    "case_id": str(note.get("case_id") or cid or ""),
                    "presence_mode": str(note.get("presence_mode") or ""),
                    "lifecycle_state": str(note.get("lifecycle_state") or ""),
                    "updated_at": str(note.get("updated_at") or ""),
                }
                if not out["feedback_state"]:
                    out["feedback_state"] = _pick_feedback(note)
                flags["used_remote_note_state"] = True
    except (DaszekClientError, OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning("Failed to fetch remote state for guidance (best-effort)", extra={"x": {
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
        }})
    return out, flags


def _bounded_remote_case(case_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": str(case_payload.get("case_id") or ""),
        "status": str(case_payload.get("status") or ""),
        "current_state": str(case_payload.get("current_state") or ""),
        "title": str(case_payload.get("title") or case_payload.get("title_pl") or "")[:240],
        "operator_brief_pl": str(case_payload.get("operator_brief_pl") or "")[:600],
        "primary_next_action_title_pl": str(case_payload.get("primary_next_action_title_pl") or "")[:240],
        "review_mode": str(case_payload.get("review_mode") or ""),
    }


def _bounded_remote_note(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "note_id": str(note.get("note_id") or note.get("desk_note_id") or ""),
        "summary_pl": str(note.get("summary_pl") or note.get("summary") or "")[:500],
        "why_on_desk": str(note.get("why_on_desk") or note.get("why_now_pl") or "")[:500],
        "presence_mode": str(note.get("presence_mode") or ""),
        "lifecycle_state": str(note.get("lifecycle_state") or ""),
    }


def _pick_feedback(obj: dict[str, Any]) -> dict[str, Any]:
    fs = obj.get("feedback_state")
    return fs if isinstance(fs, dict) else {}


def _pick_provenance(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "latest_change_source": str(obj.get("latest_change_source") or ""),
        "latest_change_reason_pl": str(obj.get("latest_change_reason_pl") or "")[:400],
    }


def parse_and_validate_case_guidance(raw_text: str) -> dict[str, Any]:
    """Parse JSON from model output and normalize to the case_guidance contract."""
    try:
        candidate = extract_json_candidate(raw_text)
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise GroqClientError(f"Case guidance did not return valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GroqClientError("Case guidance JSON must be an object.")
    return _normalize_case_guidance(data, source_mode="llm_reasoned")


def fallback_case_guidance(*, reason: str, base_intelligence: dict[str, Any]) -> dict[str, Any]:
    """Conservative guidance when the LLM stage fails."""
    _ = base_intelligence
    cg = _normalize_case_guidance(
        {
            "reason_summary_pl": f"Nie udało się bezpiecznie zinterpretować stanu sprawy ({reason[:200]}).",
            "source_mode": "fallback",
            "confidence": 0.0,
        },
        source_mode="fallback",
    )
    return cg


def build_skipped_case_guidance(*, reason: str, base_intelligence: dict[str, Any]) -> dict[str, Any]:
    """Placeholder guidance when the stage is disabled or intentionally skipped."""
    _ = base_intelligence
    _ = reason
    return _normalize_case_guidance({}, source_mode="skipped")


def run_case_guidance_reasoning(
    *,
    settings: Settings,
    prompt_input: dict[str, Any],
    context_bundle: dict[str, Any] | None = None,
    model: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Invoke structured Groq stage; returns case_guidance + execution envelope."""
    instructions = load_prompt_text("case_guidance_system_prompt.md")
    schema = load_case_guidance_schema()
    query_text = str(prompt_input.get("intake_summary_short") or prompt_input.get("query") or "case guidance")
    stage_call = run_central_structured_stage(
        settings,
        stage_name="case_guidance",
        task_instructions=instructions,
        prompt_input=prompt_input,
        query_text=query_text,
        json_schema=schema,
        schema_name="case_guidance_v1",
        case_id=str(prompt_input.get("case_id") or "").strip() or None,
        model=model,
        verbose=verbose,
        output_model=CaseGuidanceResult,
        context_bundle=context_bundle,
    )
    if stage_call is None:
        raise GroqClientError("case_guidance central_llm_stage_unavailable")
    if str(stage_call.get("parse_status") or "") == "pydantic_failed":
        errors = (stage_call.get("request_meta") or {}).get("pydantic_errors")
        logger.warning("Pydantic ValidationError in case guidance", extra={"x": {
            "error": str(errors)[:500],
            "raw_preview": str(stage_call.get("response_text", ""))[:200],
        }})
    raw_text = str(stage_call.get("response_text") or "")
    validated = parse_and_validate_case_guidance(raw_text)
    return {
        "case_guidance": validated,
        "raw_response_text": raw_text,
        "stage_call": stage_call,
    }
