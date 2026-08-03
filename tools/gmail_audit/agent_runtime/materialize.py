
"""Materialize executor — canonical HITL-approved case writer (RFC E2); other paths use case_write_gateway."""

from __future__ import annotations

from log_config import get_logger
import uuid
from typing import Any, Optional

from exceptions import CaseLookupError
try:
    from .._protocols import CorrelationStore
except ImportError:
    from _protocols import CorrelationStore  # type: ignore[no-redef]
from llm_contracts.engagement_snapshot_v2 import (
    EngagementSnapshotV2,
    HitlGate,
    MaterializeProposalItem,
    OperationalStatus,
)

log = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────
ERROR_TRUNCATION_LENGTH = 200

# RP-28 / DQ-03: only composite_plan has confirmed creators (propose_plan,
# propose_mutation). Other proposal_type literals remain for historical
# snapshot deserialization but must fail-closed on append/execute.
RETAINED_MATERIALIZE_PROPOSAL_TYPES = frozenset({"composite_plan"})
UNRETAINED_MATERIALIZE_PROPOSAL_TYPES = frozenset({
    "link_existing",
    "create_case",
    "create_artifact",
    "defer_operator",
})


def is_retained_materialize_proposal_type(proposal_type: str) -> bool:
    return str(proposal_type or "").strip() in RETAINED_MATERIALIZE_PROPOSAL_TYPES


def reject_unretained_materialize_proposal_type(proposal_type: str) -> None:
    ptype = str(proposal_type or "").strip()
    if is_retained_materialize_proposal_type(ptype):
        return
    raise ValueError(
        f"materialize proposal_type {ptype!r} is not retained "
        f"(DQ-03/RP-28); only {sorted(RETAINED_MATERIALIZE_PROPOSAL_TYPES)} may append/execute"
    )


# ── Poziom 1 identity helpers (graceful, never block materialize) ──


def _lookup_case_by_email(
    correlation_store: CorrelationStore,
    email: str,
) -> str | None:
    """Sprawdź czy ten email ma już case_id w correlation_registry."""
    try:
        email = str(email or "").strip().lower()
        if not email:
            return None
        find_fn = getattr(correlation_store, "find_identity_by_email", None)
        if not callable(find_fn):
            return None
        identity = find_fn(email)
        if not identity:
            return None
        identity_id = str(identity.get("identity_id") or "").strip()
        if not identity_id:
            return None
        # Szukamy engagementa dla tej tożsamości i stamtąd case_id
        find_eng = getattr(correlation_store, "find_engagement_for_identity_recent", None)
        if not callable(find_eng):
            return None
        engagement_id = find_eng(identity_id=identity_id)
        if not engagement_id:
            return None
        links = getattr(correlation_store, "list_links_for_engagement", None)
        if callable(links):
            for link in links(engagement_id) or []:
                if str(link.get("link_type") or "") == "mailbox_case":
                    case_id = str(link.get("target_id") or "").strip()
                    if case_id:
                        return case_id
        return None
    except Exception as exc:
        log.warning("Correlation store lookup failed – continuing without case_id match", extra={"x": {
            "error": str(exc)[:ERROR_TRUNCATION_LENGTH],
            "email": email,
        }})
        return None


def _register_engagement_link(
    correlation_store: CorrelationStore,
    *,
    engagement_id: str,
    case_id: str,
) -> bool:
    """Połącz engagement z case_id w correlation_registry."""
    try:
        upsert = getattr(correlation_store, "upsert_link", None)
        if not callable(upsert):
            return False
        upsert(
            engagement_id=engagement_id,
            link_type="mailbox_case",
            target_id=case_id,
            source_repo="gmail-agent",
            confidence=1.0,
        )
        return True
    except Exception:
        log.warning("materialize._register_engagement_link failed", exc_info=True)
        return False


def _register_email_identity(
    correlation_store: CorrelationStore,
    *,
    email: str,
    case_id: str,
    customer_name: str = "",
) -> bool:
    """Zarejestruj email jako tożsamość w correlation_registry."""
    try:
        from correlation_registry.heuristics import register_link_bundle

        email = str(email or "").strip()
        cid = str(case_id or "").strip()
        if not email or not cid:
            return False
        register_link_bundle(
            correlation_store,
            identity_email=email,
            display_name=customer_name,
            links=[
                {
                    "link_type": "mailbox_case",
                    "target_id": cid,
                    "source_repo": "gmail-agent",
                    "confidence": 1.0,
                },
            ],
        )
        return True
    except Exception:
        log.warning("materialize._register_email_identity failed", exc_info=True)
        return False


def new_proposal_id() -> str:
    return f"prop_{uuid.uuid4().hex[:16]}"


def append_materialize_proposal(
    snapshot: EngagementSnapshotV2,
    *,
    proposal_type: str,
    payload: dict[str, Any],
) -> EngagementSnapshotV2:
    reject_unretained_materialize_proposal_type(proposal_type)
    proposal = MaterializeProposalItem(
        proposal_id=new_proposal_id(),
        proposal_type=proposal_type,  # type: ignore[arg-type]
        payload_json=dict(payload or {}),
        status="pending",
    )
    memory = snapshot.agent_memory.model_copy(
        update={
            "materialize_proposals": list(snapshot.agent_memory.materialize_proposals) + [proposal],
        }
    )
    return snapshot.model_copy(
        update={
            "agent_memory": memory,
            "hitl_gate": HitlGate(required=True, reason=f"materialize_proposal:{proposal_type}"),
            "operational_status": OperationalStatus(
                code="pending_operator",
                steps_remaining=int(snapshot.operational_status.steps_remaining),
                blocking=True,
            ),
        }
    )


def _emit_os_event(
    *,
    event_type: str,
    engagement_id: str,
    payload: dict[str, Any],
    database_url: str = "",
) -> None:
    """Emituj os_event dla audytu — nie blokuje wykonania przy błędzie."""
    if not database_url:
        return
    try:
        from event_spine.emitter import publish_os_event

        publish_os_event(
            database_url=database_url,
            event_type=event_type,
            engagement_id=engagement_id,
            source_repo="gmail-agent",
            payload={
                "schema_version": "topinstal.os_event.v1",
                **payload,
            },
        )
    except Exception as exc:
        log.warning("_emit_os_event failed for %s: %s", event_type, exc)


def _execute_composite_step(
    payload: dict[str, Any],
    *,
    mailbox_store: Any | None = None,
    engagement_snapshot: EngagementSnapshotV2 | None = None,
    correlation_store: CorrelationStore | None = None,
    drive_client: Any | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Execute steps of a composite_plan after operator approval.

    Write operations are executed via WRITE_EXECUTORS (real, not deferred).
    Read steps are executed via tool handlers.
    Each write step emits an os_event for audit.
    """
    steps = list(payload.get("steps") or [])
    if not steps:
        return {"action": "empty_plan", "results": []}

    from agent_runtime.tools.write_executors import WRITE_EXECUTORS

    results: list[dict[str, Any]] = []
    eid = str(engagement_snapshot.engagement_id) if engagement_snapshot else ""
    # None = caller did not decide; empty string = explicitly no DB (do not probe settings).
    if db_url is None:
        resolved_db_url = ""
        if engagement_snapshot:
            try:
                from agent_runtime.settings import load_agent_runtime_settings

                settings = load_agent_runtime_settings()
                resolved_db_url = str(
                    getattr(settings, "mailbox_memory_database_url", "")
                    or ""
                ).strip()
            except Exception as exc:
                log.warning("materialize: failed to load settings for db_url exc=%s", exc)
    else:
        resolved_db_url = str(db_url or "").strip()

    # PR-5B / RP-26: per-step keys (never reuse one key across steps)
    global_idempotency_key = str(payload.get("idempotency_key") or "").strip() or None
    created_case_id: str | None = None  # P0.1: wyciągnij case_id z create_case executor

    for idx, step in enumerate(steps):
        operation = str(step.get("operation") or step.get("step_name_pl") or "").strip()
        args = dict(step.get("args") or {})
        step_target = str(step.get("target") or "").strip()
        if step_target:
            args.setdefault("case_id", step_target)
            args.setdefault("target", step_target)
        if created_case_id:
            args.setdefault("case_id", created_case_id)
            if operation != "create_case":
                args.setdefault("target", created_case_id)
        tool = str(step.get("tool") or "").strip()
        step_key = (
            f"{global_idempotency_key}:step:{idx}:{operation or tool or 'op'}"
            if global_idempotency_key
            else None
        )

        if operation in WRITE_EXECUTORS:
            # REAL EXECUTION — po HITL, wykonujemy naprawdę
            try:
                executor = WRITE_EXECUTORS[operation]
                result = executor(
                    args,
                    mailbox_store=mailbox_store,
                    correlation_store=correlation_store,
                    drive_client=drive_client,
                    db_url=resolved_db_url or None,
                    idempotency_key=step_key,
                    engagement_id=eid,
                )
                status = result.get("status", "ok")
                summary = result.get("summary", "")
                # P0.1: wyciągnij case_id z result executors (create_case go zwraca)
                step_case_id = str(result.get("case_id") or "").strip()
                if step_case_id:
                    created_case_id = step_case_id
                results.append({
                    "step": idx,
                    "operation": operation,
                    "status": status,
                    "summary": summary,
                    "case_id": step_case_id,  # P0.1: case_id w results dla caller flow
                })
                # Emituj os_event dla audytu
                _emit_os_event(
                    event_type=f"agent.write.{operation}",
                    engagement_id=eid,
                    payload={
                        "step": idx,
                        "operation": operation,
                        "args": args,
                        "result": result,
                    },
                    database_url=resolved_db_url,
                )
                if status == "error":
                    return {"action": "composite_failed", "results": results, "error": summary[:ERROR_TRUNCATION_LENGTH]}
            except Exception as exc:
                results.append({
                    "step": idx,
                    "operation": operation,
                    "status": "error",
                    "error": str(exc)[:ERROR_TRUNCATION_LENGTH],
                })
                return {"action": "composite_failed", "results": results, "error": str(exc)[:ERROR_TRUNCATION_LENGTH]}
        elif tool:
            # Read tools — wykonaj przez HANDLERS
            try:
                from agent_runtime.tools.handlers import HANDLERS
                from agent_runtime.tool_context import ToolExecutionContext
                from agent_runtime.tool_result import ToolCallPlan
                from agent_runtime.settings import load_agent_runtime_settings

                handler = HANDLERS.get(tool)
                if handler is None:
                    results.append({"step": idx, "tool": tool, "status": "unknown_handler"})
                    continue

                settings = load_agent_runtime_settings()
                ctx = ToolExecutionContext(
                    snapshot=engagement_snapshot,
                    signal_payload={},
                    settings=settings,
                    mailbox_store=mailbox_store,
                )
                plan = ToolCallPlan(tool_name=tool, arguments=args)
                tool_result = handler(plan, ctx)
                results.append({
                    "step": idx,
                    "tool": tool,
                    "status": str(tool_result.status),
                    "summary": str(tool_result.turn_summary_pl or "")[:ERROR_TRUNCATION_LENGTH],
                })
            except Exception as exc:
                results.append({"step": idx, "tool": tool, "status": "error", "error": str(exc)[:ERROR_TRUNCATION_LENGTH]})
        else:
            results.append({"step": idx, "operation": operation, "status": "skipped", "reason": "unknown_operation"})

    ret = {"action": "composite_executed", "results": results}
    if created_case_id:
        ret["case_id"] = created_case_id  # P0.1: caller (materialize_bridge) potrzebuje case_id
    return ret


def execute_materialize_proposal(
    *,
    mailbox_store: Any,
    proposal: MaterializeProposalItem,
    engagement_snapshot: EngagementSnapshotV2,
    correlation_store: CorrelationStore | None = None,
    drive_client: Any | None = None,
    idempotency_key: str | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Python-only executor after operator approve — never called from LLM."""
    ptype = str(proposal.proposal_type or "").strip()
    try:
        reject_unretained_materialize_proposal_type(ptype)
    except ValueError as exc:
        return {
            "action": "unretained_proposal_type",
            "status": "error",
            "error": str(exc),
            "summary": str(exc),
        }
    payload = dict(proposal.payload_json or {})
    # Strip internal lifecycle metadata from effect payload
    payload.pop("_dq02_lifecycle", None)
    key = str(idempotency_key or payload.get("idempotency_key") or "").strip() or None
    # Preserve explicit empty db_url from caller (do not re-probe settings).
    url = None if db_url is None else str(db_url).strip()
    if key:
        payload["idempotency_key"] = key
        if not url:
            return {
                "action": "idempotency_unavailable",
                "status": "error",
                "error": "idempotency_key requires db_url; refusing silent noop",
                "summary": "idempotency_key requires db_url; refusing silent noop",
            }
        from agent_runtime.idempotency import check_idempotency

        cached = check_idempotency(url, f"{key}:materialize:{ptype}")
        if cached is not None and isinstance(cached.get("result"), dict):
            return dict(cached["result"])

    def _record(result: dict[str, Any]) -> dict[str, Any]:
        if key and url:
            from agent_runtime.idempotency import record_idempotency

            record_idempotency(url, f"{key}:materialize:{ptype}", ptype, result)
        return result

    if ptype == "composite_plan":
        result = _execute_composite_step(
            payload,
            mailbox_store=mailbox_store,
            engagement_snapshot=engagement_snapshot,
            correlation_store=correlation_store,
            drive_client=drive_client,
            db_url="" if db_url is not None and not str(db_url).strip() else db_url,
        )
        return _record(result)
    # RP-28: unretained types are rejected above; keep defensive raise.
    raise ValueError(f"unknown proposal_type: {ptype!r}")


__all__ = [
    "append_materialize_proposal",
    "execute_materialize_proposal",
    "new_proposal_id",
    "RETAINED_MATERIALIZE_PROPOSAL_TYPES",
    "UNRETAINED_MATERIALIZE_PROPOSAL_TYPES",
    "is_retained_materialize_proposal_type",
    "reject_unretained_materialize_proposal_type",
]
