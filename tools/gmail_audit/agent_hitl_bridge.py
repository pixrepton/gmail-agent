"""Operator HITL approve/send bridge - Daszek UI -> Node B engagement store."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime
from typing import Any

from agent_runtime.mcp_service import AgentMcpService
from agent_runtime.store import OperatorEngagementStore
from config import Settings, load_settings
from event_spine.emitter import publish_os_event
from event_spine.gmail_telemetry import publish_gmail_feed_push_event
from hitl_gmail_send import execute_hitl_gmail_send
from mailbox_memory_runtime import build_mailbox_memory_runtime

logger = logging.getLogger(__name__)

_HITL_SEND_STATE_KEY = "agent_hitl_send_states"


class _HitlSendAlreadyStarted(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _stable_bridge_key(prefix: str, *parts: Any) -> str:
    raw = json.dumps([str(part or "") for part in parts], ensure_ascii=False)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def _hitl_send_decision_key(row: dict[str, Any]) -> str:
    queue_id = str(row.get("queue_id") or "").strip()
    if queue_id:
        return queue_id
    return _stable_bridge_key(
        "bq",
        row.get("engagement_id"),
        row.get("action_id"),
        "send",
    )


def _mailbox_store_from_settings(settings: Settings) -> Any:
    runtime = build_mailbox_memory_runtime(settings, allow_in_memory=False)
    if runtime is None or getattr(runtime, "store", None) is None:
        raise ValueError("mailbox memory runtime is not configured for HITL send")
    bootstrap = getattr(runtime, "bootstrap", None)
    if callable(bootstrap):
        bootstrap()
    return runtime.store


def _hitl_state_from_case(case_row: dict[str, Any], decision_key: str) -> dict[str, Any]:
    metadata = dict(case_row.get("metadata") or {}) if isinstance(case_row.get("metadata"), dict) else {}
    states = dict(metadata.get(_HITL_SEND_STATE_KEY) or {}) if isinstance(metadata.get(_HITL_SEND_STATE_KEY), dict) else {}
    state = states.get(decision_key)
    return dict(state) if isinstance(state, dict) else {}


def _mutate_hitl_state(
    mailbox_store: Any,
    *,
    case_id: str,
    decision_key: str,
    mutate,
) -> dict[str, Any]:
    def _mutator(case_row: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(case_row.get("metadata") or {}) if isinstance(case_row.get("metadata"), dict) else {}
        states = dict(metadata.get(_HITL_SEND_STATE_KEY) or {}) if isinstance(metadata.get(_HITL_SEND_STATE_KEY), dict) else {}
        current = states.get(decision_key)
        current_state = dict(current) if isinstance(current, dict) else {}
        next_state = mutate(current_state)
        if not isinstance(next_state, dict):
            raise RuntimeError("hitl send state mutator must return dict")
        states[decision_key] = next_state
        metadata[_HITL_SEND_STATE_KEY] = states
        case_row["metadata"] = metadata
        return case_row

    updated_case = mailbox_store.mutate_case(case_id, _mutator)
    return _hitl_state_from_case(updated_case, decision_key)


def _read_hitl_state(mailbox_store: Any, *, case_id: str, decision_key: str) -> dict[str, Any]:
    row = mailbox_store.fetch_case(case_id) if hasattr(mailbox_store, "fetch_case") else None
    if not isinstance(row, dict):
        return {}
    return _hitl_state_from_case(row, decision_key)


def _snapshot_action_parent_refs(snapshot: Any, action_id: str) -> dict[str, str]:
    for action in getattr(snapshot, "actions", []) or []:
        if str(getattr(action, "id", "") or "") != str(action_id or ""):
            continue
        return {
            "parent_policy_decision_id": str(
                getattr(action, "parent_policy_decision_id", "") or ""
            ),
            "parent_action_proposal_v2_id": str(
                getattr(action, "parent_action_proposal_v2_id", "") or ""
            ),
            "parent_decision_candidate_id": str(
                getattr(action, "parent_decision_candidate_id", "") or ""
            ),
            "source_signal_id": str(getattr(action, "source_signal_id", "") or ""),
        }
    return {
        "parent_policy_decision_id": "",
        "parent_action_proposal_v2_id": "",
        "parent_decision_candidate_id": "",
        "source_signal_id": "",
    }


def _normalized_parent_refs(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    return {
        "parent_policy_decision_id": str(
            raw.get("parent_policy_decision_id") or ""
        ),
        "parent_action_proposal_v2_id": str(
            raw.get("parent_action_proposal_v2_id") or ""
        ),
        "parent_decision_candidate_id": str(
            raw.get("parent_decision_candidate_id") or ""
        ),
        "source_signal_id": str(raw.get("source_signal_id") or ""),
    }


def _persist_hitl_send_result(
    mailbox_store: Any,
    *,
    case_id: str,
    engagement_id: str,
    action_id: str,
    operator_id: str,
    decision_key: str,
    state: dict[str, Any],
    execution: dict[str, Any],
    parent_refs: dict[str, str],
) -> str:
    decision_status = str(state.get("status") or execution.get("decision_status") or "").strip()
    execution_status = "executed" if decision_status == "executed" else ("blocked" if decision_status == "outcome_unknown" else "failed")
    execution_id = _stable_bridge_key("execution", decision_key, "agent_hitl_send")
    mailbox_store.upsert_execution_result(
        {
            "execution_id": execution_id,
            "proposal_id": decision_key,
            "case_id": case_id,
            "action_type": "agent_hitl_send",
            "approved_by": operator_id,
            "approved_at": str(state.get("accepted_at") or ""),
            "executed_by": "daszek_bridge_queue",
            "executed_at": str(state.get("completed_at") or state.get("started_at") or _now_iso()),
            "execution_status": execution_status,
            "error_code": str(execution.get("reason") or ""),
            "error_message": str(execution.get("error") or ""),
            "result_payload": {
                "decision_key": decision_key,
                "decision_status": decision_status,
                "engagement_id": engagement_id,
                "action_id": action_id,
                "operator_id": operator_id,
                "parent_refs": dict(parent_refs),
                "execution": execution,
            },
            "audit_trace_id": _stable_bridge_key("audit", decision_key, decision_status or "unknown"),
            "policy_result": dict(parent_refs),
        }
    )
    if decision_status == "executed":
        event_id = _stable_bridge_key("event", decision_key, "agent_hitl_send_executed")
        mailbox_store.append_event(
            {
                "event_id": event_id,
                "case_id": case_id,
                "message_id": "",
                "thread_id": "",
                "event_type": "agent_hitl_send_executed",
                "occurred_at": str(state.get("completed_at") or _now_iso()),
                "summary_text": "Operator HITL send executed exactly once",
                "payload": {
                    "decision_key": decision_key,
                    "engagement_id": engagement_id,
                    "action_id": action_id,
                    "operator_id": operator_id,
                    "parent_refs": dict(parent_refs),
                    "execution": execution,
                },
                "source_refs": [{"type": "case", "case_id": case_id}],
            }
        )
        return event_id
    return ""


def agent_hitl_payload_from_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "domain": "agent_hitl",
        "adjudication_kind": str(row.get("adjudication_kind") or "hitl_action_execute"),
        "engagement_id": str(row.get("engagement_id") or "").strip(),
        "case_id": str(row.get("case_id") or "").strip(),
        "action_id": str(row.get("action_id") or "draft_reply").strip(),
        "operator_id": str(row.get("operator_id") or "").strip(),
        "decision_key": _hitl_send_decision_key(row),
        "operator_draft_pl": str(
            row.get("operator_draft_pl") or row.get("draft_pl") or ""
        ).strip(),
    }
    expected_hash = str(row.get("expected_body_hash") or row.get("body_hash") or "").strip()
    if expected_hash:
        payload["expected_body_hash"] = expected_hash
    return payload


def best_effort_push_engagement_feed_after_hitl(
    *,
    settings: Settings,
    operator_store: OperatorEngagementStore,
    engagement_id: str = "",
    case_id: str = "",
) -> dict[str, Any]:
    if not bool(getattr(settings, "daszek_operational_feed_auto_push_enabled", False)):
        return {"ok": False, "skipped": True, "reason": "auto_push_disabled"}

    try:
        from daszek_client import DaszekClient, DaszekClientError
        from daszek_engagement_feed import build_operational_feed_from_engagement_store

        client = DaszekClient(settings)
        case_ids = [case_id] if case_id else None
        if case_ids is None and engagement_id:
            snap = operator_store.load_snapshot(engagement_id)
            if snap is not None and str(snap.case_id or "").strip():
                case_ids = [str(snap.case_id)]

        snapshot = build_operational_feed_from_engagement_store(
            operator_store,
            case_ids=case_ids,
            case_limit=max(1, int(getattr(settings, "daszek_operational_feed_case_limit", 50) or 50)),
            source={
                "trigger": "agent_hitl",
                "engagement_id": engagement_id,
                "case_id": case_id,
            },
        )
        response = client.post_v3_operational_feed_snapshot(snapshot)
        snapshot_id = str(response.get("snapshot_id") or snapshot.get("snapshot_id") or "")
        publish_gmail_feed_push_event(
            settings,
            ok=True,
            snapshot_id=snapshot_id,
            engagement_id=engagement_id,
            case_id=case_id,
            trigger="agent_hitl",
        )
        return {"ok": True, "snapshot_id": snapshot_id}
    except (ImportError, ValueError, DaszekClientError) as exc:
        publish_gmail_feed_push_event(
            settings,
            ok=False,
            error=str(exc),
            engagement_id=engagement_id,
            case_id=case_id,
            trigger="agent_hitl",
        )
        return {"ok": False, "skipped": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        publish_gmail_feed_push_event(
            settings,
            ok=False,
            error=str(exc),
            engagement_id=engagement_id,
            case_id=case_id,
            trigger="agent_hitl",
        )
        return {"ok": False, "skipped": False, "error": str(exc)}


def _project_execution_attention_best_effort(
    operator_store: Any,
    *,
    engagement_id: str,
    reason: str,
) -> dict[str, Any]:
    """SLICE-2B1: make an unresolved send visible to the operator without touching execution.

    After `approve_hitl_action` the snapshot has `hitl_gate.required=False` and
    `operational_status.code="ready_for_quote"`, and an `outcome_unknown` send raises before any
    snapshot write. The case therefore has no executive field left that says "an operator must
    look at this", even though the send may or may not have happened and auto-retry is forbidden.

    This writes only `feed_visibility.execution_attention`. It cannot re-send, cannot alter the
    `decision_key`, and cannot change the MailboxMemory send state -- and it is best-effort, so a
    CAS conflict or a store error never masks the original execution outcome.
    """
    eid = str(engagement_id or "").strip()
    if not eid or operator_store is None:
        return {"ok": False, "reason": "no_engagement_or_store"}
    try:
        from feed_visibility import mark_execution_attention
        from llm_contracts.engagement_snapshot_v2 import FeedVisibility

        snapshot = operator_store.load_snapshot(eid)
        if snapshot is None:
            return {"ok": False, "reason": "snapshot_not_found"}
        current = snapshot.feed_visibility
        if current is not None and bool(getattr(current, "execution_attention", False)):
            return {"ok": True, "reason": "already_flagged"}
        patched = snapshot.model_copy(
            update={"feed_visibility": FeedVisibility(**mark_execution_attention(current, reason=reason))}
        )
        operator_store.save_snapshot(patched, expected_version=snapshot.version)
        return {"ok": True, "reason": reason}
    except Exception as exc:  # noqa: BLE001 - visibility projection must never mask the send result
        logger.warning("EXECUTION_ATTENTION_PROJECTION_FAILED: %s", exc)
        return {"ok": False, "reason": str(exc)}


def approve_hitl_engagement(
    *,
    engagement_id: str,
    action_id: str,
    operator_id: str = "",
    settings: Settings | None = None,
    operator_draft_pl: str | None = None,
    operator_answer_pl: str | None = None,
    expected_body_hash: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    settings = settings or load_settings(require_groq=False, require_google=False)
    aid = str(action_id or "").strip()

    if aid.startswith("prop_"):
        from agent_runtime.agent_reconcile import build_operator_engagement_store
        from agent_runtime.materialize_bridge import approve_materialize_proposal
        from mailbox_memory_runtime import build_mailbox_memory_runtime

        operator_store = build_operator_engagement_store(settings)
        runtime = build_mailbox_memory_runtime(settings, allow_in_memory=False)
        mailbox_store = runtime.store if runtime is not None else None
        result = approve_materialize_proposal(
            operator_store,
            engagement_id=str(engagement_id or "").strip(),
            proposal_id=aid,
            operator_id=str(operator_id or "").strip(),
            mailbox_store=mailbox_store,
            settings=settings,
        )
        if not result.get("ok"):
            return result

        resolved_case_id = str(result.get("case_id") or "").strip()
        db_url = str(
            getattr(settings, "mailbox_memory_database_url", "")
            or os.environ.get("MAILBOX_MEMORY_DATABASE_URL")
            or ""
        ).strip()
        if db_url:
            publish_os_event(
                database_url=db_url,
                event_type="gmail.hitl.approved",
                engagement_id=str(engagement_id or "").strip(),
                source_repo="gmail-agent",
                payload={
                    "schema_version": "topinstal.os_event.v1",
                    "summary_pl": "Operator approved materialize proposal (HITL)",
                    "status": "ok",
                    "action_id": aid,
                    "operator_id": str(operator_id or "").strip(),
                },
                correlation={
                    "case_id": resolved_case_id,
                    "adjudication_kind": "hitl_action_approved",
                    "approve_key": f"{engagement_id}|{aid}|{operator_id}",
                    "proposal_type": "materialize",
                },
            )

        result["decision_key"] = _stable_bridge_key("approve", engagement_id, aid, operator_id)
        result.setdefault("decision_status", "accepted")
        result["feed_push"] = best_effort_push_engagement_feed_after_hitl(
            settings=settings,
            operator_store=operator_store,
            engagement_id=str(engagement_id or "").strip(),
            case_id=resolved_case_id,
        )
        return result

    service = AgentMcpService.from_env(bootstrap_postgres=True)
    result = service.approve_hitl_action(
        engagement_id=str(engagement_id or "").strip(),
        action_id=str(action_id or "draft_reply").strip(),
        operator_id=str(operator_id or "").strip(),
        operator_draft_pl=operator_draft_pl,
        operator_answer_pl=operator_answer_pl,
        expected_body_hash=expected_body_hash,
        expected_revision=expected_revision,
    )
    if not result.get("ok"):
        return result

    adjudication = result.get("adjudication") if isinstance(result.get("adjudication"), dict) else {}
    snapshot_payload = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else {}
    case_id = str(adjudication.get("case_id") or "").strip()
    engagement_id = str(engagement_id or "").strip()
    action_id = str(action_id or "draft_reply").strip()
    operator_id = str(operator_id or "").strip()
    trace_id = str(snapshot_payload.get("trace_id") or "").strip()

    db_url = str(
        getattr(settings, "mailbox_memory_database_url", "")
        or os.environ.get("MAILBOX_MEMORY_DATABASE_URL")
        or ""
    ).strip()
    os_event_id = None
    if db_url:
        os_event_id = publish_os_event(
            database_url=db_url,
            event_type="gmail.hitl.approved",
            engagement_id=engagement_id,
            source_repo="gmail-agent",
                payload={
                    "schema_version": "topinstal.os_event.v1",
                    "summary_pl": "Operator approved reply draft for manual delivery (HITL)",
                    "status": "ok",
                    "action_id": action_id,
                    "operator_id": operator_id,
                    "decision_status": str(result.get("decision_status") or "approved"),
                    "execution_status": str(result.get("execution_status") or "not_applicable"),
                    "delivery_mode": str(result.get("delivery_mode") or "manual_operator"),
                },
                correlation={
                    "case_id": case_id,
                    "adjudication_kind": "hitl_action_approved",
                    "approve_key": f"{engagement_id}|{action_id}|{operator_id}",
            },
            trace_id=trace_id,
            user_id=operator_id,
            case_id=case_id,
        )
    result["os_event_id"] = os_event_id
    result["decision_key"] = _stable_bridge_key("approve", engagement_id, action_id, operator_id)
    result["feed_push"] = best_effort_push_engagement_feed_after_hitl(
        settings=settings,
        operator_store=service.store,
        engagement_id=engagement_id,
        case_id=case_id,
    )
    return result


def execute_hitl_send_from_bridge_row(
    *,
    row: dict[str, Any],
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or load_settings(require_groq=False, require_google=False)
    engagement_id = str(row.get("engagement_id") or "").strip()
    action_id = str(row.get("action_id") or "draft_reply").strip()
    operator_id = str(row.get("operator_id") or "").strip()
    case_id = str(row.get("case_id") or "").strip()
    decision_key = _hitl_send_decision_key(row)

    if not engagement_id:
        raise ValueError("engagement_id is required for hitl_action_execute")

    service = AgentMcpService.from_env(bootstrap_postgres=True)
    snapshot = service.store.load_snapshot(engagement_id)
    if snapshot is None:
        raise ValueError(f"engagement snapshot not found: {engagement_id}")
    if snapshot.hitl_gate.required:
        raise ValueError("hitl_gate is still active - approve before send")

    if not case_id:
        case_id = str(snapshot.case_id or "").strip()
    snapshot_parent_refs = _snapshot_action_parent_refs(snapshot, action_id)

    mailbox_store = _mailbox_store_from_settings(settings)
    cached_state = _read_hitl_state(mailbox_store, case_id=case_id, decision_key=decision_key)
    cached_status = str(cached_state.get("status") or "").strip()
    if isinstance(cached_state.get("parent_refs"), dict):
        parent_refs = _normalized_parent_refs(cached_state.get("parent_refs"))
    elif cached_status in {"executed", "outcome_unknown"}:
        # A legacy terminal execution cannot be retroactively parented from a
        # snapshot that may have changed after the effect.
        parent_refs = _normalized_parent_refs({})
    else:
        parent_refs = snapshot_parent_refs

    if cached_status in {"executed", "outcome_unknown"}:
        execution = dict(cached_state.get("execution") or {})
        if cached_status == "outcome_unknown":
            # replay of an already-unresolved send: the flag is idempotent, so re-asserting it
            # here covers a projection that failed (or did not yet exist) on the first pass
            _project_execution_attention_best_effort(
                service.store, engagement_id=engagement_id, reason="hitl_send_outcome_unknown"
            )
        event_id = _persist_hitl_send_result(
            mailbox_store,
            case_id=case_id,
            engagement_id=engagement_id,
            action_id=action_id,
            operator_id=operator_id,
            decision_key=decision_key,
            state=cached_state,
            execution=execution,
            parent_refs=parent_refs,
        )
    else:
        accepted_at = _now_iso()
        _mutate_hitl_state(
            mailbox_store,
            case_id=case_id,
            decision_key=decision_key,
            mutate=lambda current: current
            if str(current.get("status") or "").strip() in {"executed", "outcome_unknown", "executing"}
            else {
                "decision_key": decision_key,
                "status": "accepted",
                "accepted_at": str(current.get("accepted_at") or accepted_at),
                "engagement_id": engagement_id,
                "action_id": action_id,
                "operator_id": operator_id,
                "parent_refs": dict(parent_refs),
            },
        )

        def _claim_effect_start() -> None:
            def _claim(current: dict[str, Any]) -> dict[str, Any]:
                status = str(current.get("status") or "").strip()
                if status in {"executing", "executed", "outcome_unknown"}:
                    raise _HitlSendAlreadyStarted(status or "executing")
                claimed = dict(current)
                claimed["status"] = "executing"
                claimed["started_at"] = _now_iso()
                return claimed

            _mutate_hitl_state(
                mailbox_store,
                case_id=case_id,
                decision_key=decision_key,
                mutate=_claim,
            )

        try:
            execution = execute_hitl_gmail_send(
                settings=settings,
                snapshot=snapshot,
                action_id=action_id,
                case_id=case_id,
                operator_id=operator_id,
                on_effect_start=_claim_effect_start,
                operator_draft_pl=str(
                    row.get("operator_draft_pl") or row.get("draft_pl") or ""
                ).strip()
                or None,
            )
        except _HitlSendAlreadyStarted:
            current = _read_hitl_state(mailbox_store, case_id=case_id, decision_key=decision_key)
            execution = dict(current.get("execution") or {})
            current_status = str(current.get("status") or "").strip()
            if current_status == "executing":
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and current_status == "executing":
                    time.sleep(0.01)
                    current = _read_hitl_state(mailbox_store, case_id=case_id, decision_key=decision_key)
                    execution = dict(current.get("execution") or {})
                    current_status = str(current.get("status") or "").strip()
            if current_status not in {"executed", "outcome_unknown"}:
                raise
            event_id = _persist_hitl_send_result(
                mailbox_store,
                case_id=case_id,
                engagement_id=engagement_id,
                action_id=action_id,
                operator_id=operator_id,
                decision_key=decision_key,
                state=current,
                execution=execution,
                parent_refs=parent_refs,
            )
        else:
            decision_status = str(
                execution.get("decision_status") or ("executed" if execution.get("executed") else "failed_before_execution")
            ).strip()
            final_state = _mutate_hitl_state(
                mailbox_store,
                case_id=case_id,
                decision_key=decision_key,
                mutate=lambda current: {
                    **dict(current),
                    "status": decision_status,
                    "completed_at": _now_iso(),
                    "execution": dict(execution),
                },
            )
            event_id = _persist_hitl_send_result(
                mailbox_store,
                case_id=case_id,
                engagement_id=engagement_id,
                action_id=action_id,
                operator_id=operator_id,
                decision_key=decision_key,
                state=final_state,
                execution=execution,
                parent_refs=parent_refs,
            )
            if decision_status == "outcome_unknown":
                # visibility only, and BEFORE the raise: the operator must be able to find this
                # case, and auto-retry stays forbidden (AGENTS.md) because nothing about the send
                # state, the decision_key, or operational_status is touched here
                _project_execution_attention_best_effort(
                    service.store, engagement_id=engagement_id, reason="hitl_send_outcome_unknown"
                )
                raise RuntimeError("hitl_send_outcome_unknown")
            if decision_status != "executed":
                raise RuntimeError(str(execution.get("reason") or "hitl_send_failed_before_execution"))

    feed_push = best_effort_push_engagement_feed_after_hitl(
        settings=settings,
        operator_store=service.store,
        engagement_id=engagement_id,
        case_id=case_id,
    )

    return {
        "ok": True,
        "engagement_id": engagement_id,
        "action_id": action_id,
        "operator_id": operator_id,
        "case_id": case_id,
        "decision_key": decision_key,
        "parent_refs": parent_refs,
        "event_id": event_id,
        "execution": execution,
        "feed_push": feed_push,
    }


__all__ = [
    "agent_hitl_payload_from_row",
    "approve_hitl_engagement",
    "best_effort_push_engagement_feed_after_hitl",
    "execute_hitl_send_from_bridge_row",
]
