"""AI-OS 3.2 — Brain1 → Brain2 canonical draft lineage transport.

When Brain 1 (`reply_drafter`) already produced a sendable draft, Brain 2 must
transfer that draft with stable identity instead of regenerating via
``generate_draft_reply``. The handler remains a controlled fallback only when
upstream truly did not supply a complete draft.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from agent_runtime.draft_identity import compute_body_hash, compute_draft_id
from agent_runtime.draft_lineage_provenance import build_draft_lineage_provenance, draft_origin_from_transport
from agent_runtime.tool_result import ToolResult

DraftSource = Literal["brain1", "brain2_fallback"]

_REQUIRED_TRANSPORT_KEYS = (
    "draft_id",
    "revision",
    "body",
    "body_hash",
    "source",
    "created_at",
    "action_id",
    "case_id",
    "source_signal_id",
)


class DraftLineageContractError(ValueError):
    """Upstream draft transport is present but incomplete or inconsistent."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _select_brain1_draft_body(reply_result: dict[str, Any]) -> str:
    if not bool(reply_result.get("draft_enabled")):
        return ""
    drafts = [item for item in (reply_result.get("drafts") or []) if isinstance(item, dict)]
    if not drafts:
        return ""
    recommended = str(reply_result.get("recommended_variant") or "").strip()
    if recommended:
        for draft in drafts:
            if str(draft.get("variant") or "").strip() == recommended:
                body = str(draft.get("body") or "").strip()
                if body:
                    return body
    for draft in drafts:
        body = str(draft.get("body") or "").strip()
        if body:
            return body
    return ""


def build_upstream_draft_transport(
    *,
    reply_result: dict[str, Any] | None,
    case_id: str,
    source_signal_id: str,
    action_id: str = "draft_reply",
    created_at: str | None = None,
) -> dict[str, Any] | None:
    """Build a transport envelope from Brain 1 ``reply_drafter`` output.

    Returns None when Brain 1 did not produce a complete draft (honest absence).
    """
    payload = reply_result if isinstance(reply_result, dict) else {}
    body = _select_brain1_draft_body(payload)
    if not body:
        return None
    if bool(payload.get("requires_manual_edit")):
        return None
    if payload.get("do_not_send_reasons"):
        return None

    cid = str(case_id or "").strip()
    sid = str(source_signal_id or "").strip()
    aid = str(action_id or "draft_reply").strip() or "draft_reply"
    if not cid or not sid:
        return None

    body_hash = compute_body_hash(body)
    if not body_hash:
        return None

    return {
        "draft_id": compute_draft_id(case_id=cid, source_signal_id=sid, action_id=aid),
        "revision": 1,
        "body": body,
        "body_hash": body_hash,
        "source": "brain1",
        "created_at": str(created_at or _utc_now_iso()),
        "action_id": aid,
        "case_id": cid,
        "source_signal_id": sid,
    }


def validate_upstream_draft_transport(transport: dict[str, Any]) -> None:
    """Fail-closed when transport exists but is incomplete or hash-mismatched."""
    if not isinstance(transport, dict) or not transport:
        raise DraftLineageContractError("upstream_draft_transport_missing")
    missing = [key for key in _REQUIRED_TRANSPORT_KEYS if not str(transport.get(key) or "").strip()]
    if missing:
        raise DraftLineageContractError(f"upstream_draft_transport_incomplete:{','.join(missing)}")
    body = str(transport.get("body") or "")
    expected_hash = str(transport.get("body_hash") or "")
    actual_hash = compute_body_hash(body)
    if not actual_hash:
        raise DraftLineageContractError("upstream_draft_transport_empty_body")
    if actual_hash != expected_hash:
        raise DraftLineageContractError("upstream_draft_transport_body_hash_mismatch")
    try:
        revision = int(transport.get("revision") or 0)
    except (TypeError, ValueError) as exc:
        raise DraftLineageContractError("upstream_draft_transport_invalid_revision") from exc
    if revision < 1:
        raise DraftLineageContractError("upstream_draft_transport_invalid_revision")


def upstream_draft_transport_from_signal(signal_payload: dict[str, Any] | None) -> dict[str, Any] | None:
    payload = signal_payload if isinstance(signal_payload, dict) else {}
    transport = payload.get("upstream_draft_transport")
    return transport if isinstance(transport, dict) and transport else None


def materialize_transferred_draft_action(
    transport: dict[str, Any],
    *,
    identity_state: str = "identity_incomplete",
) -> dict[str, Any]:
    validate_upstream_draft_transport(transport)
    return {
        "id": str(transport.get("action_id") or "draft_reply"),
        "enabled": True,
        "payload_pl": str(transport.get("body") or ""),
        "disabled_reason_pl": None,
        "draft_id": str(transport.get("draft_id") or ""),
        "revision": int(transport.get("revision") or 1),
        "body_hash": str(transport.get("body_hash") or ""),
        "case_id": str(transport.get("case_id") or ""),
        "source_signal_id": str(transport.get("source_signal_id") or ""),
        "identity_state": identity_state,
        "parent_policy_decision_id": "",
        "parent_action_proposal_v2_id": "",
        "parent_decision_candidate_id": "",
    }


def tool_result_from_upstream_transport(transport: dict[str, Any]) -> ToolResult:
    validate_upstream_draft_transport(transport)
    source = str(transport.get("source") or "brain1")
    origin = draft_origin_from_transport(transport)
    provenance = build_draft_lineage_provenance(
        draft_origin=origin,
        origin_correlation_id=str(transport.get("source_signal_id") or ""),
        origin_producer="reply_drafter" if origin == "brain1" else "generate_draft_reply",
        origin_created_at=str(transport.get("created_at") or ""),
    )
    return ToolResult(
        status="ok",
        turn_summary_pl=(
            "Draft Brain 1 przeniesiony bez regeneracji (lineage transport)."
            if source == "brain1"
            else "Draft utworzony przez Brain 2 (fallback)."
        ),
        snapshot_delta={
            "actions": [materialize_transferred_draft_action(transport)],
            "hitl_gate": {"required": True, "reason": "draft_ready_for_approval"},
            "operational_status": {"code": "pending_operator"},
            "draft_lineage_provenance": provenance,
        },
    )


def contract_error_tool_result(exc: DraftLineageContractError) -> ToolResult:
    return ToolResult(
        status="error",
        turn_summary_pl=f"Kontrakt draft lineage naruszony: {exc}",
        snapshot_delta={
            "hitl_gate": {"required": True, "reason": f"draft_lineage_contract:{exc}"},
            "operational_status": {"code": "pending_operator", "blocking": True},
        },
    )


def resolve_generate_draft_reply(
    signal_payload: dict[str, Any] | None,
) -> tuple[ToolResult | None, bool]:
    """Decide whether to transfer upstream draft or allow fallback.

    Returns ``(tool_result, allow_fallback)``:

    * ``(result, False)`` — transferred upstream draft; do not call handler.
    * ``(error_result, False)`` — contract violation; do not call handler.
    * ``(None, True)`` — no upstream draft; handler may run as fallback.
    """
    transport = upstream_draft_transport_from_signal(signal_payload)
    if transport is None:
        return None, True
    try:
        return tool_result_from_upstream_transport(transport), False
    except DraftLineageContractError as exc:
        return contract_error_tool_result(exc), False


__all__ = [
    "DraftLineageContractError",
    "DraftSource",
    "build_upstream_draft_transport",
    "contract_error_tool_result",
    "materialize_transferred_draft_action",
    "resolve_generate_draft_reply",
    "tool_result_from_upstream_transport",
    "upstream_draft_transport_from_signal",
    "validate_upstream_draft_transport",
]
