"""Deterministic Brain1 draft-path causal observability (no LLM, no product semantics)."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = "draft_path_observability.v1"

_NOREPLY_TOKENS = ("noreply", "no-reply", "mailer-daemon")
_RUN_ACTIONS = {
    "reply": "BR_ACTION_REPLY",
    "collect_data": "BR_ACTION_COLLECT_DATA",
    "call": "BR_ACTION_CALL",
}
_REVIEW_ALLOWED_ACTIONS = {"reply", "collect_data"}
_BOUNDED_LIST_CAP = 12
_BOUNDED_TEXT_CAP = 240


def canonical_identity(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _bounded_list(value: Any) -> list[Any]:
    items = list(value) if isinstance(value, list) else []
    out: list[Any] = []
    for item in items[:_BOUNDED_LIST_CAP]:
        if isinstance(item, str):
            out.append(item[:_BOUNDED_TEXT_CAP])
        elif isinstance(item, dict):
            out.append(
                {
                    str(k)[:80]: (
                        str(v)[:_BOUNDED_TEXT_CAP]
                        if not isinstance(v, (int, float, bool))
                        else v
                    )
                    for k, v in list(item.items())[:12]
                }
            )
        else:
            out.append(item)
    return out


def _sender_noreply_hit(snapshot: dict[str, Any] | None) -> bool:
    sender = str((snapshot or {}).get("source_message", {}).get("sender") or "").lower()
    return any(token in sender for token in _NOREPLY_TOKENS)


def _intake_action(intake_result: dict[str, Any] | None) -> str:
    return str((intake_result or {}).get("decision", {}).get("action") or "")


def evaluate_draft_gate(
    snapshot: dict[str, Any] | None,
    intake_result: dict[str, Any] | None,
    business_result: dict[str, Any] | None,
    *,
    lane_plan: dict[str, Any] | None = None,
    skip_draft_reply: bool = False,
) -> dict[str, Any]:
    """Map current pre-invocation branches onto RUN/SKIP + reason codes."""
    plan = lane_plan if isinstance(lane_plan, dict) else {}
    run_reply_drafter = bool(plan.get("run_reply_drafter", True))
    action = _intake_action(intake_result)
    business_action = str((business_result or {}).get("recommended_next_action") or "")
    review_required = bool((intake_result or {}).get("review_required"))
    reply_recommended = bool((business_result or {}).get("reply_recommended"))
    noreply = _sender_noreply_hit(snapshot)
    business_present = business_result is not None

    decision_inputs = {
        "intake_action": action,
        "review_required": review_required,
        "recommended_next_action": business_action,
        "reply_recommended": reply_recommended,
        "sender_noreply_hit": noreply,
        "run_reply_drafter": run_reply_drafter,
        "business_result_present": business_present,
        "skip_draft_reply": bool(skip_draft_reply),
    }

    decision = "SKIP"
    primary = ""

    if skip_draft_reply:
        primary = "DRIVE_SIGNAL_SKIP"
    elif not run_reply_drafter:
        primary = "LANE_REPLY_DRAFTER_DISABLED"
    elif not business_present:
        primary = "BUSINESS_REASONING_MISSING"
    elif action == "ignore":
        primary = "INTAKE_ACTION_IGNORE"
    elif action == "mark_watchlist":
        primary = "INTAKE_ACTION_WATCHLIST"
    elif review_required and business_action not in _REVIEW_ALLOWED_ACTIONS:
        primary = "REVIEW_REQUIRED_WITHOUT_REPLY_OR_COLLECT_DATA"
    elif noreply:
        primary = "NOREPLY_SENDER"
    elif business_action in _RUN_ACTIONS:
        decision = "RUN"
        primary = _RUN_ACTIONS[business_action]
    elif reply_recommended:
        decision = "RUN"
        primary = "BR_REPLY_RECOMMENDED"
    else:
        primary = "BR_ACTION_AND_REPLY_FLAG_NOT_ELIGIBLE"

    return {
        "stage": "DRAFT_GATE",
        "decision": decision,
        "primary_reason_code": primary,
        "reason_codes": [primary] if primary else [],
        "decision_inputs": decision_inputs,
        "input_identity": canonical_identity(decision_inputs),
    }


def evaluate_drafter_execution(
    *,
    status: str,
    draft_present: bool = False,
    reason_code: str | None = None,
    provider: str = "",
    model: str = "",
    attempt_count: int = 0,
    fallback_used: bool = False,
    latency_ms: int | None = None,
    draft_identity: str = "",
) -> dict[str, Any]:
    codes = [reason_code] if reason_code else []
    return {
        "stage": "DRAFTER_EXECUTION",
        "status": status,
        "draft_present": bool(draft_present),
        "reason_codes": codes,
        "provider": provider,
        "model": model,
        "attempt_count": attempt_count,
        "fallback_used": bool(fallback_used),
        "latency_ms": latency_ms,
        "draft_identity": draft_identity,
    }


def evaluate_draft_postcheck(
    parsed: dict[str, Any] | None,
    *,
    execution_status: str,
) -> dict[str, Any]:
    if execution_status != "SUCCESS" or not isinstance(parsed, dict):
        return {
            "stage": "DRAFT_POSTCHECK",
            "decision": "NOT_APPLICABLE",
            "reason_codes": [],
        }
    drafts = parsed.get("drafts") or []
    if not drafts:
        return {
            "stage": "DRAFT_POSTCHECK",
            "decision": "NOT_APPLICABLE",
            "reason_codes": [],
        }
    reasons = [str(item) for item in (parsed.get("do_not_send_reasons") or []) if str(item).strip()]
    if parsed.get("draft_enabled") is False:
        return {
            "stage": "DRAFT_POSTCHECK",
            "decision": "BLOCK",
            "reason_codes": reasons,
            "requires_manual_edit": bool(parsed.get("requires_manual_edit")),
        }
    return {
        "stage": "DRAFT_POSTCHECK",
        "decision": "ACCEPT",
        "reason_codes": reasons,
        "requires_manual_edit": bool(parsed.get("requires_manual_edit")),
    }


def derive_draft_path_outcome(
    *,
    gate: dict[str, Any],
    execution: dict[str, Any] | None = None,
    postcheck: dict[str, Any] | None = None,
) -> dict[str, Any]:
    execution = execution if isinstance(execution, dict) else {}
    postcheck = postcheck if isinstance(postcheck, dict) else {}
    if str(gate.get("decision") or "") != "RUN":
        return {
            "draft_path_outcome": "SKIPPED_PRE_DRAFTER",
            "first_terminal_reason_code": str(gate.get("primary_reason_code") or ""),
        }
    exec_status = str(execution.get("status") or "NOT_STARTED")
    if exec_status != "SUCCESS":
        codes = [str(c) for c in (execution.get("reason_codes") or []) if str(c).strip()]
        return {
            "draft_path_outcome": "DRAFTER_FAILED",
            "first_terminal_reason_code": codes[0] if codes else exec_status,
        }
    if str(postcheck.get("decision") or "") == "BLOCK":
        codes = [str(c) for c in (postcheck.get("reason_codes") or []) if str(c).strip()]
        return {
            "draft_path_outcome": "DRAFT_BLOCKED_POSTCHECK",
            "first_terminal_reason_code": codes[0] if codes else "DRAFT_POSTCHECK_BLOCK",
        }
    return {
        "draft_path_outcome": "DRAFT_ACCEPTED",
        "first_terminal_reason_code": str(gate.get("primary_reason_code") or ""),
    }


def bounded_business_reasoning_result(business_result: dict[str, Any] | None) -> dict[str, Any]:
    result = business_result if isinstance(business_result, dict) else {}
    meta = result.get("execution_metadata") if isinstance(result.get("execution_metadata"), dict) else {}
    bounded = {
        "stage": "BUSINESS_REASONING",
        "recommended_next_action": str(result.get("recommended_next_action") or ""),
        "reply_recommended": bool(result.get("reply_recommended")),
        "human_review_bias": str(result.get("human_review_bias") or "")[:_BOUNDED_TEXT_CAP],
        "missing_information": _bounded_list(result.get("missing_information")),
        "risks": _bounded_list(result.get("risks")),
        "urgency": str(result.get("urgency") or "")[:80],
        "confidence": result.get("confidence") if isinstance(result.get("confidence"), dict) else {},
        "assumptions": _bounded_list(result.get("assumptions")),
        "unsupported_claims": _bounded_list(result.get("unsupported_claims")),
        "conflict_refs": _bounded_list(result.get("conflict_refs")),
        "provider": str(meta.get("central_llm_provider") or meta.get("provider") or ""),
        "model": str(meta.get("model_name") or meta.get("model") or ""),
        "attempt_count": int(meta.get("attempt_count") or 0),
        "fallback_used": bool(meta.get("fallback_used")),
        "latency_ms": meta.get("latency_ms"),
        "terminal_status": str(meta.get("parse_status") or meta.get("reasoning_status") or ""),
    }
    bounded["output_identity"] = canonical_identity(
        {
            "recommended_next_action": bounded["recommended_next_action"],
            "reply_recommended": bounded["reply_recommended"],
            "human_review_bias": bounded["human_review_bias"],
            "missing_information": bounded["missing_information"],
            "risks": bounded["risks"],
            "urgency": bounded["urgency"],
            "confidence": bounded["confidence"],
        }
    )
    return bounded


def _with_lineage(
    record: dict[str, Any],
    lineage: dict[str, Any],
    *,
    stage_record_id: str,
    parent_id: str = "",
) -> dict[str, Any]:
    out = dict(record)
    out["schema_version"] = SCHEMA_VERSION
    out["stage_record_id"] = stage_record_id
    out["parent_stage_record_id"] = parent_id
    out["lineage"] = dict(lineage)
    return out


def _mint_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def build_causal_observability(
    *,
    snapshot: dict[str, Any] | None,
    intake_result: dict[str, Any] | None,
    business_result: dict[str, Any] | None,
    gate: dict[str, Any],
    execution: dict[str, Any] | None = None,
    postcheck: dict[str, Any] | None = None,
    lineage: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    shared_lineage = {
        "run_id": "",
        "case_id": "",
        "engagement_id": "",
        "message_id": str(((snapshot or {}).get("source_message") or {}).get("message_id") or ""),
        "snapshot_version": str((snapshot or {}).get("snapshot_version") or ""),
    }
    if isinstance(lineage, dict):
        shared_lineage.update({key: lineage[key] for key in lineage if lineage.get(key) is not None})

    if execution is None and str(gate.get("decision") or "") != "RUN":
        execution = evaluate_drafter_execution(status="NOT_STARTED", draft_present=False)
    if postcheck is None:
        postcheck = evaluate_draft_postcheck(
            None,
            execution_status=str((execution or {}).get("status") or "NOT_STARTED"),
        )

    br = bounded_business_reasoning_result(business_result)
    br_id = _mint_id("br")
    gate_id = _mint_id("gate")
    exec_id = _mint_id("exec")
    post_id = _mint_id("post")

    br_rec = _with_lineage(br, shared_lineage, stage_record_id=br_id)
    br_rec["input_identity"] = gate.get("input_identity") or canonical_identity(gate.get("decision_inputs") or {})
    gate_rec = _with_lineage(gate, shared_lineage, stage_record_id=gate_id, parent_id=br_id)
    exec_rec = _with_lineage(execution or {}, shared_lineage, stage_record_id=exec_id, parent_id=gate_id)
    post_rec = _with_lineage(postcheck or {}, shared_lineage, stage_record_id=post_id, parent_id=exec_id)

    derived = derive_draft_path_outcome(gate=gate, execution=exec_rec, postcheck=post_rec)
    assembled: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "lineage": shared_lineage,
        "business_reasoning": br_rec,
        "draft_gate": gate_rec,
        "drafter_execution": exec_rec,
        "draft_postcheck": post_rec,
        "draft_path_outcome": derived["draft_path_outcome"],
        "first_terminal_reason_code": derived["first_terminal_reason_code"],
    }
    if extra:
        assembled.update(extra)
    return assembled


def lineage_from_context(
    snapshot: dict[str, Any] | None,
    context_bundle: dict[str, Any] | None = None,
    *,
    run_id: str = "",
    case_id: str = "",
    engagement_id: str = "",
) -> dict[str, Any]:
    bundle = context_bundle if isinstance(context_bundle, dict) else {}
    pack = bundle.get("case_context_pack") if isinstance(bundle.get("case_context_pack"), dict) else {}
    return {
        "run_id": str(run_id or bundle.get("run_id") or ""),
        "case_id": str(case_id or bundle.get("case_id") or pack.get("case_id") or ""),
        "engagement_id": str(
            engagement_id or bundle.get("engagement_id") or pack.get("engagement_id") or ""
        ),
        "message_id": str(((snapshot or {}).get("source_message") or {}).get("message_id") or ""),
        "snapshot_version": str((snapshot or {}).get("snapshot_version") or ""),
    }


def classify_drafter_failure(
    *,
    reason: str = "",
    exc: BaseException | None = None,
) -> tuple[str, str]:
    details = ""
    if exc is not None and isinstance(getattr(exc, "details", None), dict):
        details = str(exc.details.get("error_class") or "").lower()
    text = f"{reason} {exc or ''} {details}".lower()
    if details == "deadline_exhausted" or "timeout" in text or "deadline" in text:
        return "TIMEOUT", "STAGE_DEADLINE_OR_TIMEOUT"
    if "json" in text or "pydantic" in text:
        return "VALIDATION_FAILURE", "PARSE_OR_VALIDATION_FAILED"
    if "central_llm_stage_unavailable" in text:
        return "PROVIDER_FAILURE", "CENTRAL_LLM_STAGE_UNAVAILABLE"
    if not str(reason or "").strip() and exc is None:
        return "OTHER_ATTRIBUTABLE_FAILURE", "OTHER_ATTRIBUTABLE_FAILURE"
    return "PROVIDER_FAILURE", "GROQ_CLIENT_ERROR"


def execution_from_stage_call(
    stage_call: dict[str, Any] | None,
    parsed: dict[str, Any] | None,
) -> dict[str, Any]:
    meta = stage_call if isinstance(stage_call, dict) else {}
    drafts = list((parsed or {}).get("drafts") or []) if isinstance(parsed, dict) else []
    bodies = [str(item.get("body") or "") for item in drafts if isinstance(item, dict)]
    parse_status = str(meta.get("parse_status") or "")
    status = "SUCCESS"
    reason_code = None
    # pydantic_failed with a recovered draft body stays SUCCESS: the product path
    # still shipped a parsed draft. VALIDATION_FAILURE is only when no body remains.
    if parse_status == "pydantic_failed" and not bodies:
        status = "VALIDATION_FAILURE"
        reason_code = "PYDANTIC_FAILED"
    return evaluate_drafter_execution(
        status=status,
        draft_present=bool(bodies),
        reason_code=reason_code,
        provider=str(meta.get("central_llm_provider") or meta.get("provider") or ""),
        model=str(meta.get("model_name") or meta.get("model") or ""),
        attempt_count=int(meta.get("attempt_count") or 0),
        fallback_used=bool(meta.get("fallback_used")),
        latency_ms=meta.get("latency_ms"),
        draft_identity=canonical_identity(bodies) if bodies else "",
    )


def attach_causal_observability(
    product_result: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None = None,
    intake_result: dict[str, Any] | None = None,
    business_result: dict[str, Any] | None = None,
    gate: dict[str, Any] | None = None,
    execution: dict[str, Any] | None = None,
    postcheck: dict[str, Any] | None = None,
    lineage: dict[str, Any] | None = None,
    observability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach evidence without changing product draft fields. Failures are explicit."""
    out = dict(product_result)
    try:
        payload = observability
        if payload is None:
            resolved_gate = gate or evaluate_draft_gate(snapshot, intake_result, business_result)
            payload = build_causal_observability(
                snapshot=snapshot,
                intake_result=intake_result,
                business_result=business_result,
                gate=resolved_gate,
                execution=execution,
                postcheck=postcheck,
                lineage=lineage,
            )
        out["causal_observability"] = payload
        return out
    except Exception:
        out["observability_write_failed"] = True
        return out


__all__ = [
    "SCHEMA_VERSION",
    "attach_causal_observability",
    "bounded_business_reasoning_result",
    "build_causal_observability",
    "canonical_identity",
    "classify_drafter_failure",
    "derive_draft_path_outcome",
    "evaluate_draft_gate",
    "evaluate_draft_postcheck",
    "evaluate_drafter_execution",
    "execution_from_stage_call",
    "lineage_from_context",
]
