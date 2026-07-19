"""Preview-only mapping from intake decisions to Daszek task/item payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from intake_policy import (
    CASE_KEY_SOURCE_DERIVED,
    CASE_KEY_SOURCE_LINKED,
    CASE_KEY_SOURCE_NONE,
    DASZEK_EXTERNAL_REF_KEYS,
    DASZEK_INTAKE_KEYS,
    DASZEK_KIND_BY_DECISION,
    DASZEK_KINDS,
    DASZEK_PRIORITY_BY_INTAKE_PRIORITY,
    DASZEK_SOURCE,
    DASZEK_TASKS_ENDPOINT,
    PREVIEW_ADAPTER_VERSION,
    PREVIEW_TARGET_DASZEK,
    SOURCE_CHANNEL_GMAIL,
    case_key_allowed_for_action,
    derive_case_key,
    extract_best_case_key,
)


def build_dash_preview(
    intake_output: dict[str, Any],
    *,
    stage_outputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map one validated intake output to preview-only Daszek API requests."""
    action = intake_output["decision"]["action"]
    requests = _build_requests(intake_output)
    preview = {
        "adapter_version": PREVIEW_ADAPTER_VERSION,
        "target": PREVIEW_TARGET_DASZEK,
        "decision_action": action,
        "message_id": intake_output["message"]["message_id"],
        "thread_id": intake_output["thread"]["thread_id"],
        "ignored": not requests,
        "requests": requests,
        "metadata": _build_preview_metadata(intake_output, stage_outputs=stage_outputs or {}),
    }
    validate_dash_preview(preview)
    return preview


def validate_dash_preview(preview: dict[str, Any]) -> None:
    """Validate that a preview payload matches the real local Daszek contract."""
    if preview.get("ignored"):
        if preview.get("requests"):
            raise ValueError("Ignored previews must not contain requests.")
        return

    requests = preview.get("requests")
    if not isinstance(requests, list) or not requests:
        raise ValueError("Preview must contain at least one request when ignored=false.")

    if preview.get("decision_action") == "create_case_and_task":
        object_types = [request.get("object_type") for request in requests]
        if object_types != ["case", "task"]:
            raise ValueError("create_case_and_task preview must create exactly one case followed by one task.")

    metadata = preview.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("Preview metadata must be an object when present.")

    for index, request in enumerate(requests, start=1):
        _validate_request(request, index=index)


def _build_requests(intake_output: dict[str, Any]) -> list[dict[str, Any]]:
    action = intake_output["decision"]["action"]
    if action not in DASZEK_KIND_BY_DECISION:
        raise ValueError(f"Unsupported decision.action for Daszek preview: {action}")
    payload_builders = {
        "case": _build_case_payload,
        "case_update": _build_case_update_payload,
        "task": _build_task_payload,
        "reference": _build_reference_payload,
        "watchlist": _build_watchlist_payload,
        "review": _build_review_payload,
    }
    object_types = DASZEK_KIND_BY_DECISION.get(action, ())
    requests: list[dict[str, Any]] = []
    for request_index, object_type in enumerate(object_types, start=1):
        payload = payload_builders[object_type](intake_output)
        requests.append(_request_for_payload(intake_output, payload, request_index=request_index))
    return requests


def _request_for_payload(
    intake_output: dict[str, Any],
    payload: dict[str, Any],
    *,
    request_index: int,
) -> dict[str, Any]:
    return {
        "request_id": f"preview:{intake_output['message']['message_id']}:{payload['kind']}:{request_index}",
        "rest_endpoint": DASZEK_TASKS_ENDPOINT,
        "method": "POST",
        "object_type": payload["kind"],
        "message_id": intake_output["message"]["message_id"],
        "payload": payload,
    }


def _build_case_payload(intake_output: dict[str, Any]) -> dict[str, Any]:
    case_key_info = resolve_case_key_metadata(intake_output)
    return _base_payload(
        intake_output,
        kind="case",
        title=_build_title(intake_output, prefix="Case"),
        due_at=_extract_due_at(intake_output),
        case_key_info=case_key_info,
        tags=_build_tags(intake_output, extra=["case", "new_case"]),
    )


def _build_case_update_payload(intake_output: dict[str, Any]) -> dict[str, Any]:
    action = intake_output["decision"]["action"]
    extra_tags = ["case_update", "existing_case"]
    if action == "update_case_state":
        extra_tags.append("state_change")
    else:
        extra_tags.append("append")

    return _base_payload(
        intake_output,
        kind="case_update",
        title=_build_title(intake_output, prefix="Case update"),
        due_at=_extract_due_at(intake_output),
        case_key_info=resolve_case_key_metadata(intake_output),
        tags=_build_tags(intake_output, extra=extra_tags),
    )


def _build_task_payload(intake_output: dict[str, Any]) -> dict[str, Any]:
    return _base_payload(
        intake_output,
        kind="task",
        title=_build_title(intake_output, prefix="Task"),
        due_at=_extract_due_at(intake_output),
        case_key_info=resolve_case_key_metadata(intake_output),
        tags=_build_tags(intake_output, extra=["task"]),
    )


def _build_reference_payload(intake_output: dict[str, Any]) -> dict[str, Any]:
    return _base_payload(
        intake_output,
        kind="reference",
        title=_build_title(intake_output, prefix="Reference"),
        due_at=None,
        case_key_info=resolve_case_key_metadata(intake_output),
        tags=_build_tags(intake_output, extra=["reference"]),
    )


def _build_watchlist_payload(intake_output: dict[str, Any]) -> dict[str, Any]:
    return _base_payload(
        intake_output,
        kind="watchlist",
        title=_build_title(intake_output, prefix="Watchlist"),
        due_at=_extract_due_at(intake_output),
        case_key_info=resolve_case_key_metadata(intake_output),
        tags=_build_tags(intake_output, extra=["watchlist"]),
    )


def _build_review_payload(intake_output: dict[str, Any]) -> dict[str, Any]:
    return _base_payload(
        intake_output,
        kind="review",
        title=_build_title(intake_output, prefix="Review"),
        due_at=_extract_due_at(intake_output),
        case_key_info=resolve_case_key_metadata(intake_output),
        tags=_build_tags(intake_output, extra=["review", "manual_review"]),
    )


def _base_payload(
    intake_output: dict[str, Any],
    *,
    kind: str,
    title: str,
    due_at: str | None,
    case_key_info: dict[str, str | None],
    tags: list[str],
) -> dict[str, Any]:
    priority = intake_output["priority"]
    return {
        "title": title,
        "due_at": due_at,
        "amount": _extract_amount(intake_output),
        "source": DASZEK_SOURCE,
        "kind": kind,
        "priority": DASZEK_PRIORITY_BY_INTAKE_PRIORITY[priority],
        "note": _build_note(intake_output),
        "tags": tags,
        "external_ref": _build_external_ref(
            intake_output,
            str(case_key_info.get("case_key") or "").strip() or None,
        ),
        "intake": _build_intake_metadata(intake_output, case_key_info=case_key_info),
    }


def _build_note(intake_output: dict[str, Any]) -> str:
    lines = [
        intake_output["reason"].strip(),
        "",
        f"Decision: {intake_output['decision']['action']}",
        f"Rationale: {intake_output['decision']['action_rationale']}".strip(),
    ]

    owner = intake_output["decision"].get("suggested_owner")
    if owner:
        lines.append(f"Owner hint: {owner}")

    sla = intake_output["decision"].get("sla_hint")
    if sla:
        lines.append(f"SLA hint: {sla}")

    review_flags = intake_output["review"]["flags"]
    if review_flags:
        lines.append(f"Review flags: {', '.join(review_flags)}")

    return "\n".join(line for line in lines if line is not None).strip()


def _build_external_ref(intake_output: dict[str, Any], case_key: str | None) -> dict[str, str]:
    external_ref = {
        "channel": SOURCE_CHANNEL_GMAIL,
        "mailbox": intake_output["source"]["mailbox"],
        "message_id": intake_output["message"]["message_id"],
        "thread_id": intake_output["thread"]["thread_id"],
        "received_at": intake_output["message"]["date"],
    }
    if case_key:
        external_ref["case_key"] = case_key
    return external_ref


def _build_intake_metadata(
    intake_output: dict[str, Any],
    *,
    case_key_info: dict[str, str | None],
) -> dict[str, Any]:
    return {
        "decision_action": intake_output["decision"]["action"],
        "business_area": intake_output["business_area"],
        "case_family": intake_output["case_assessment"]["case_family"],
        "case_key": str(case_key_info.get("case_key") or "").strip(),
        "case_key_source": str(case_key_info.get("case_key_source") or CASE_KEY_SOURCE_NONE),
        "primary_signal_code": intake_output["primary_signal"]["code"],
        "primary_signal_name": intake_output["primary_signal"]["name"],
        "review_required": intake_output["review"]["required"],
        "review_flags": intake_output["review"]["flags"],
        "confidence": intake_output["confidence"],
        "reason": intake_output["reason"],
        "action_rationale": intake_output["decision"]["action_rationale"],
        "state_detected": intake_output["case_assessment"]["state_detected"],
        "state_change": intake_output["case_assessment"]["state_change"],
        "extracted_data": intake_output["extracted_data"],
    }


def _build_preview_metadata(
    intake_output: dict[str, Any],
    *,
    stage_outputs: dict[str, Any],
) -> dict[str, Any]:
    business_result = stage_outputs.get("business_reasoning_result") or {}
    reply_result = stage_outputs.get("reply_draft_result") or {}
    action_plan = stage_outputs.get("action_plan_result") or {}
    case_link_result = stage_outputs.get("case_link_result") or {}
    preclassification = stage_outputs.get("preclassification_result") or {}
    intelligence_result = stage_outputs.get("case_intelligence_result") or {}
    case_understanding = intelligence_result.get("case_understanding") or {}
    desk_composition = intelligence_result.get("desk_composition") or {}
    operator_brief = intelligence_result.get("operator_brief") or {}
    next_best_action = (intelligence_result.get("next_best_action") or {}).get("primary_next_action") or {}
    missing_info = intelligence_result.get("missing_info") or {}
    risk_assessment = intelligence_result.get("risk_assessment") or {}
    business_confidence = business_result.get("confidence") if isinstance(business_result.get("confidence"), dict) else {}

    return {
        "preclassification_lane": str(preclassification.get("lane") or "intake_llm"),
        "business_interpretation_summary": str(
            business_result.get("business_summary_short")
            or business_result.get("business_interpretation")
            or ""
        ).strip(),
        "recommended_next_action": str(
            action_plan.get("primary_action")
            or business_result.get("recommended_next_action")
            or ""
        ).strip(),
        "operator_note": str(business_result.get("operator_note") or "").strip(),
        "reply_draft_available": bool(reply_result.get("draft_enabled")),
        "business_confidence": float(business_confidence.get("business_confidence") or 0.0),
        "action_confidence": float(
            action_plan.get("confidence")
            if action_plan.get("confidence") is not None
            else business_confidence.get("action_confidence")
            if business_confidence.get("action_confidence") is not None
            else 0.0
        ),
        "case_link_confidence": float(
            case_link_result.get("confidence")
            if case_link_result.get("confidence") is not None
            else intake_output["confidence"]["case_link_confidence"]
        ),
        "review_reason_summary": ", ".join(intake_output["review"]["flags"]),
        "operator_brief_pl": str(operator_brief.get("brief_pl") or ""),
        "intelligence_summary_short": str(case_understanding.get("summary_short") or ""),
        "intelligence_attention_reason": str(case_understanding.get("attention_reason") or ""),
        "next_best_action_title_pl": str(next_best_action.get("title_pl") or ""),
        "missing_info_summary_pl": str(missing_info.get("summary_pl") or ""),
        "risk_summary_pl": str(risk_assessment.get("summary_pl") or ""),
        "presence_mode": str(desk_composition.get("presence_mode") or ""),
        "surface_zone": str(desk_composition.get("surface_zone") or ""),
        "lifecycle_intent": str(desk_composition.get("lifecycle_intent") or ""),
    }


def _build_title(intake_output: dict[str, Any], *, prefix: str) -> str:
    subject = intake_output["message"]["subject"].strip()
    signal_name = intake_output["primary_signal"]["name"].strip()
    business_area = intake_output["business_area"].replace("_", " ")

    if subject:
        return f"{prefix}: {subject}"

    return f"{prefix}: {signal_name} ({business_area})"


def _build_tags(intake_output: dict[str, Any], *, extra: list[str] | None = None) -> list[str]:
    tags = [
        intake_output["business_area"],
        intake_output["case_assessment"]["case_family"],
        intake_output["primary_signal"]["code"],
        intake_output["decision"]["action"],
    ]

    if intake_output["review"]["required"]:
        tags.append("review_required")
    tags.extend(intake_output["review"]["flags"])

    if extra:
        tags.extend(extra)

    return sorted({tag for tag in tags if tag})


def resolve_case_key_metadata(intake_output: dict[str, Any]) -> dict[str, str | None]:
    """Return the stable case-key projection used by preview, eval, and live dedupe."""
    action = str(intake_output["decision"]["action"] or "")
    allow_derived = action in {"create_case", "create_case_and_task", "create_task", "mark_watchlist"}
    return _select_case_key_info(intake_output, allow_derived=allow_derived)


def _select_case_key_info(intake_output: dict[str, Any], *, allow_derived: bool) -> dict[str, str | None]:
    candidates = intake_output["thread"].get("linked_case_candidates") or []
    best_candidate = extract_best_case_key(candidates)
    if best_candidate:
        return {
            "case_key": best_candidate,
            "case_key_source": CASE_KEY_SOURCE_LINKED,
        }

    if not allow_derived:
        return {
            "case_key": None,
            "case_key_source": CASE_KEY_SOURCE_NONE,
        }

    case_family = intake_output["case_assessment"]["case_family"]
    thread_id = intake_output["thread"]["thread_id"]
    derived = derive_case_key(case_family=case_family, thread_id=thread_id)
    if derived:
        return {
            "case_key": derived,
            "case_key_source": CASE_KEY_SOURCE_DERIVED,
        }
    return {
        "case_key": None,
        "case_key_source": CASE_KEY_SOURCE_NONE,
    }


def _extract_due_at(intake_output: dict[str, Any]) -> str | None:
    deadlines = intake_output["extracted_data"]["deadlines"]
    for deadline in deadlines:
        value = deadline.get("date")
        if isinstance(value, str):
            normalized = _normalize_date(value)
            if normalized:
                return normalized
    return None


def _extract_amount(intake_output: dict[str, Any]) -> float | None:
    amounts = intake_output["extracted_data"]["amounts"]
    for amount in amounts:
        value = amount.get("value")
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _normalize_date(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None

    try:
        return datetime.fromisoformat(candidate.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue

    return None


def _validate_request(request: dict[str, Any], *, index: int) -> None:
    request_id = request.get("request_id")
    if request.get("rest_endpoint") != DASZEK_TASKS_ENDPOINT:
        raise ValueError(f"Preview request #{index} targets an unexpected Daszek endpoint.")
    if request.get("method") != "POST":
        raise ValueError(f"Preview request #{index} must use POST.")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError(f"Preview request #{index} must contain a non-empty request_id.")

    payload = request.get("payload")
    if not isinstance(payload, dict):
        raise ValueError(f"Preview request #{index} is missing a payload object.")

    title = payload.get("title")
    kind = payload.get("kind")
    priority = payload.get("priority")
    source = payload.get("source")
    note = payload.get("note")
    tags = payload.get("tags")
    external_ref = payload.get("external_ref")
    intake = payload.get("intake")
    object_type = request.get("object_type")

    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"Preview request #{index} must contain a non-empty title.")
    if object_type != kind:
        raise ValueError(f"Preview request #{index} object_type must match payload.kind.")
    if kind not in DASZEK_KINDS:
        raise ValueError(f"Preview request #{index} uses unsupported kind `{kind}`.")
    if priority not in DASZEK_PRIORITY_BY_INTAKE_PRIORITY.values():
        raise ValueError(f"Preview request #{index} uses unsupported priority `{priority}`.")
    if source != DASZEK_SOURCE:
        raise ValueError(f"Preview request #{index} uses unsupported source `{source}`.")
    if note is not None and not isinstance(note, str):
        raise ValueError(f"Preview request #{index} note must be string or null.")
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError(f"Preview request #{index} tags must be a string list.")
    if external_ref is not None:
        if not isinstance(external_ref, dict):
            raise ValueError(f"Preview request #{index} external_ref must be object or null.")
        extra_keys = set(external_ref.keys()) - set(DASZEK_EXTERNAL_REF_KEYS)
        if extra_keys:
            raise ValueError(f"Preview request #{index} external_ref contains unsupported keys: {sorted(extra_keys)}")
        if external_ref.get("channel") not in {None, SOURCE_CHANNEL_GMAIL}:
            raise ValueError(f"Preview request #{index} external_ref.channel must be gmail when present.")
        if object_type == "case_update" and not str(external_ref.get("case_key") or "").strip():
            raise ValueError(f"Preview request #{index} case_update payload must include external_ref.case_key.")
    if intake is not None:
        if not isinstance(intake, dict):
            raise ValueError(f"Preview request #{index} intake must be object or null.")
        extra_keys = set(intake.keys()) - set(DASZEK_INTAKE_KEYS)
        if extra_keys:
            raise ValueError(f"Preview request #{index} intake contains unsupported keys: {sorted(extra_keys)}")
        case_key_source = intake.get("case_key_source")
        if case_key_source not in {None, CASE_KEY_SOURCE_LINKED, CASE_KEY_SOURCE_DERIVED, CASE_KEY_SOURCE_NONE}:
            raise ValueError(f"Preview request #{index} intake.case_key_source uses unsupported vocabulary.")
        if case_key_allowed_for_action(str(intake.get("decision_action") or "")):
            external_case_key = str((external_ref or {}).get("case_key") or "").strip()
            intake_case_key = str(intake.get("case_key") or "").strip()
            if external_case_key != intake_case_key:
                raise ValueError(
                    f"Preview request #{index} case-key projection must agree between intake and external_ref."
                )

        confidence = intake.get("confidence")
        if confidence is not None:
            if not isinstance(confidence, dict):
                raise ValueError(f"Preview request #{index} intake.confidence must be an object.")
            for field in ("signal_confidence", "case_link_confidence", "decision_confidence", "extraction_confidence"):
                if field in confidence:
                    value = confidence[field]
                    if not isinstance(value, (int, float)) or value < 0 or value > 1:
                        raise ValueError(f"Preview request #{index} intake.confidence.{field} must be in 0..1.")
