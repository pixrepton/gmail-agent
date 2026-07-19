"""Hooks: record agent proposals for divergence loop (Mechanism A)."""

from __future__ import annotations

from typing import Any
from _protocols import DatabaseConnection


def hook_record_action_proposal_v2(
    conn: DatabaseConnection,
    *,
    proposal: dict[str, Any],
    case_id: str,
    engagement_id: str = "",
    case_family: str = "",
) -> str | None:
    if conn is None:
        return None
    try:
        from divergence_loop import record_agent_proposal

        action_type = str(proposal.get("action_type") or proposal.get("proposal_type") or "").strip()
        return record_agent_proposal(
            conn,
            engagement_id=engagement_id,
            case_id=case_id,
            proposal_type=action_type,
            proposal_content=dict(proposal),
            proposal_reasoning_pl=str(proposal.get("reasoning_pl") or proposal.get("summary_pl") or ""),
            source_pipeline="action_proposal_v2",
            proposal_id=str(proposal.get("proposal_id") or ""),
        )
    except Exception:
        return None


def hook_record_agent_draft(
    conn: DatabaseConnection,
    *,
    case_id: str,
    engagement_id: str = "",
    draft_text: str = "",
    tool_name: str = "generate_draft_reply",
) -> str | None:
    if conn is None:
        return None
    try:
        from divergence_loop import record_agent_proposal

        return record_agent_proposal(
            conn,
            engagement_id=engagement_id,
            case_id=case_id,
            proposal_type=tool_name,
            proposal_content={"draft_text": draft_text, "action_type": tool_name},
            proposal_reasoning_pl="Draft wygenerowany przez agent runtime",
            source_pipeline="agent_draft",
        )
    except Exception:
        return None


def hook_process_operator_action(
    conn: DatabaseConnection,
    *,
    case_id: str,
    case_family: str,
    operator_action_type: str,
    operator_payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if conn is None:
        return []
    try:
        from divergence_loop import process_operator_action

        return process_operator_action(
            conn,
            case_id=case_id,
            case_family=case_family,
            operator_action_type=operator_action_type,
            operator_payload=operator_payload,
        )
    except Exception:
        return []
