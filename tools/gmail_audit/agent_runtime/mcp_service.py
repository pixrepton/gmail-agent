"""Agent runtime MCP service layer (PR-G) — testable without MCP SDK."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent_runtime.agent_reconcile import build_operator_engagement_store
from agent_runtime.draft_identity import (
    apply_operator_draft_edit,
    compute_body_hash,
    compute_draft_id,
    mint_gap_only_draft_action,
)
from agent_runtime.store import PostgresOperatorEngagementStore
from agent_runtime.run import build_turn_journal, execute_agent_run
from agent_runtime.settings import AgentRuntimeSettings, load_agent_runtime_settings
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import (
    AgentConcurrencyError,
    InMemoryOperatorEngagementStore,
    OperatorEngagementStore,
)
from agent_runtime.turn_journal import AgentTurnJournal, InMemoryAgentTurnJournal
from agent_runtime.validate import AgentRuntimeConfigError
from llm_contracts.engagement_snapshot_v2 import ActionItem, EngagementSnapshotV2, HitlGate

MCP_TOOL_NAMES = (
    "get_engagement_snapshot",
    "list_active_engagements",
    "trigger_agent_run",
    "approve_hitl_action",
    "get_agent_turns",
)

DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 100
DEFAULT_TURNS_LIMIT = 50
MAX_TURNS_LIMIT = 200


@dataclass
class AgentMcpService:
    """Thin MCP-facing facade over operator store + optional turn journal."""

    store: OperatorEngagementStore
    settings: AgentRuntimeSettings
    turn_journal: AgentTurnJournal | None = None
    run_agent: Callable[..., Any] | None = None

    @classmethod
    def from_env(cls, *, bootstrap_postgres: bool = True) -> AgentMcpService:
        settings = load_agent_runtime_settings()
        store = build_operator_engagement_store(settings)
        if bootstrap_postgres and isinstance(store, PostgresOperatorEngagementStore):
            store.bootstrap()
        journal = build_turn_journal(settings)
        return cls(store=store, settings=settings, turn_journal=journal)

    def get_engagement_snapshot(
        self,
        *,
        engagement_id: str = "",
        case_id: str = "",
        include_full: bool = True,
    ) -> dict[str, Any]:
        eid = str(engagement_id or "").strip()
        cid = str(case_id or "").strip()
        if not eid and not cid:
            return _error("engagement_id or case_id is required")
        snapshot = self.store.load_snapshot(eid) if eid else None
        if snapshot is None and cid:
            snapshot = self.store.load_snapshot_by_case_id(cid)
        if snapshot is None:
            return _error("engagement snapshot not found", engagement_id=eid, case_id=cid)
        body = _snapshot_summary(snapshot)
        if include_full:
            body["full"] = snapshot.model_dump(mode="python")
        return {"ok": True, "snapshot": body}

    def list_active_engagements(
        self,
        *,
        status: str = "",
        blocking_gaps_only: bool = False,
        hitl_required_only: bool = False,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> dict[str, Any]:
        cap = _clamp_limit(limit)
        status_filter = str(status or "").strip().lower()
        rows = self.store.list_recent_snapshots(limit=cap)
        items: list[dict[str, Any]] = []
        for snap in rows:
            if status_filter and str(snap.operational_status.code or "").lower() != status_filter:
                continue
            if blocking_gaps_only and not _has_blocking_gap(snap):
                continue
            if hitl_required_only and not snap.hitl_gate.required:
                continue
            items.append(_snapshot_summary(snap))
        return {"ok": True, "count": len(items), "engagements": items}

    def get_agent_turns(
        self,
        *,
        engagement_id: str,
        limit: int = DEFAULT_TURNS_LIMIT,
    ) -> dict[str, Any]:
        eid = str(engagement_id or "").strip()
        if not eid:
            return _error("engagement_id is required")
        if self.turn_journal is None:
            return _error(
                "agent turn journal not configured (set MAILBOX_MEMORY_DATABASE_URL or pass turn_journal)"
            )
        journal = self.turn_journal
        turns = journal.list_turns(eid, limit=_clamp_turns_limit(limit))
        return {
            "ok": True,
            "engagement_id": eid,
            "count": len(turns),
            "turns": [_turn_summary(row) for row in turns],
        }

    def trigger_agent_run(
        self,
        *,
        engagement_id: str,
        signal: Mapping[str, Any] | None = None,
        allow_when_disabled: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        eid = str(engagement_id or "").strip()
        if not eid:
            return _error("engagement_id is required")
        loaded = self.store.load_snapshot(eid)
        if loaded is not None and not force:
            code = str(loaded.operational_status.code or "").strip().lower()
            if code in {"pending_operator", "node_a_error"}:
                return _error(
                    f"trigger blocked for terminal status {code!r} (use force=true to override)",
                    engagement_id=eid,
                    operational_status=code,
                )
        mode = str(self.settings.mode or "").strip().lower()
        if mode == "legacy":
            return _error("trigger_agent_run is forbidden in AGENT_RUNTIME_MODE=legacy")
        if not self.settings.enabled and not allow_when_disabled:
            if not _mcp_debug_override_enabled():
                return _error("AGENT_RUNTIME_MODE must be prep|primary (or set AGENT_MCP_ALLOW_DEBUG=1)")
        try:
            runner = self.run_agent or execute_agent_run
            result = runner(
                eid,
                store=self.store,
                signal=dict(signal or {}),
                settings=self.settings,
                turn_journal=self.turn_journal,
                require_enabled=not allow_when_disabled and not _mcp_debug_override_enabled(),
            )
        except (AgentRuntimeConfigError, ValueError, AgentConcurrencyError) as exc:
            return _error(str(exc), engagement_id=eid)
        snap = result.snapshot
        return {
            "ok": True,
            "run_id": str(uuid.uuid4()),
            "status": str(snap.operational_status.code or ""),
            "engagement_id": eid,
            "version": int(result.version),
            "turns": len(result.graph.turns),
            "warnings": list(result.warnings),
            "snapshot": _snapshot_summary(snap),
        }

    def approve_hitl_action(
        self,
        *,
        engagement_id: str,
        action_id: str,
        operator_id: str = "",
        operator_draft_pl: str | None = None,
        operator_answer_pl: str | None = None,
        expected_body_hash: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        eid = str(engagement_id or "").strip()
        aid = str(action_id or "").strip()
        if not eid or not aid:
            return _error("engagement_id and action_id are required")
        if aid.startswith("prop_"):
            from agent_runtime.materialize_bridge import approve_materialize_proposal
            from mailbox_memory_runtime import build_mailbox_memory_runtime

            runtime = build_mailbox_memory_runtime(self.settings, allow_in_memory=False)
            mailbox_store = runtime.store if runtime is not None else None
            return approve_materialize_proposal(
                self.store,
                engagement_id=eid,
                proposal_id=aid,
                operator_id=str(operator_id or "").strip(),
                mailbox_store=mailbox_store,
            )
        snapshot = self.store.load_snapshot(eid)
        if snapshot is None:
            return _error("engagement snapshot not found", engagement_id=eid)
        if not snapshot.hitl_gate.required:
            return _error("hitl_gate is not active — nothing to approve", engagement_id=eid)
        action = _find_action(snapshot, aid)
        allow_gap_only_draft_reply = action is None and aid == "draft_reply" and not snapshot.actions
        if action is None and not allow_gap_only_draft_reply:
            return _error(f"action_id {aid!r} not found on snapshot", engagement_id=eid)
        if action is not None and not action.enabled:
            return _error(
                f"action_id {aid!r} must have enabled=True before HITL approve",
                engagement_id=eid,
            )
        expected_hash = str(expected_body_hash or "").strip()
        if (
            expected_hash
            and action is not None
            and str(action.body_hash or "")
            and expected_hash != str(action.body_hash or "")
        ):
            # Fail-closed: the caller (preview/HITL packet) was built against a body
            # that is no longer the current revision of this draft. Approving anyway
            # would let an operator confirm a version they never actually saw.
            return _error(
                f"body_hash mismatch for action_id {aid!r}: expected {expected_hash!r}, "
                f"current is {action.body_hash!r} (revision {action.revision}) — stale draft, refetch before approving",
                engagement_id=eid,
            )
        if (
            expected_revision is not None
            and action is not None
            and int(action.revision or 1) > 0
        ):
            try:
                expected_rev = int(expected_revision)
            except (TypeError, ValueError):
                expected_rev = -1
            if expected_rev >= 1 and expected_rev != int(action.revision or 1):
                return _error(
                    f"revision mismatch for action_id {aid!r}: expected {expected_rev}, "
                    f"current is {action.revision} — stale draft, refetch before approving",
                    engagement_id=eid,
                )

        from datetime import datetime, timezone

        from llm_contracts.engagement_snapshot_v2 import ClarificationAnswerItem

        draft_provided = operator_draft_pl is not None
        draft_text = str(operator_draft_pl or "").strip()
        answer_text = str(operator_answer_pl or "").strip()
        gate_reason = str(snapshot.hitl_gate.reason or "").strip()
        is_clarification = gate_reason == "operator_clarification" or aid == "operator_clarification"
        if is_clarification and not answer_text:
            # Daszek reuses draft_pl for clarification answers; accept either channel.
            answer_text = draft_text
        if is_clarification and not answer_text:
            return _error(
                "operator_answer_pl (or operator_draft_pl) is required for clarification approval",
                engagement_id=eid,
            )

        actions_payload = [a.model_dump(mode="python") for a in snapshot.actions]
        if draft_provided and not is_clarification:
            # Fail-closed: explicit empty/whitespace operator body is not approvable.
            if not compute_body_hash(draft_text):
                return _error(
                    "operator_draft_pl is empty — refusing to approve an empty draft body",
                    engagement_id=eid,
                )
            updated = False
            for idx, item in enumerate(actions_payload):
                if str(item.get("id") or "") == aid:
                    actions_payload[idx] = apply_operator_draft_edit(
                        item,
                        draft_text=draft_text,
                        case_id=str(snapshot.case_id or ""),
                        source_signal_id=str(snapshot.signal_id or ""),
                        action_id=aid,
                    )
                    updated = True
                    break
            if not updated:
                actions_payload.append(
                    mint_gap_only_draft_action(
                        action_id=aid or "draft_reply",
                        draft_text=draft_text,
                        case_id=str(snapshot.case_id or ""),
                        source_signal_id=str(snapshot.signal_id or ""),
                    )
                )
        elif not is_clarification and action is not None and not str(action.draft_id or ""):
            # Legacy action without identity: mint once at approve-as-is so the
            # approval record still points at a durable artifact revision.
            for idx, item in enumerate(actions_payload):
                if str(item.get("id") or "") == aid:
                    body = str(item.get("payload_pl") or "")
                    actions_payload[idx] = apply_operator_draft_edit(
                        item,
                        draft_text=body,
                        case_id=str(snapshot.case_id or ""),
                        source_signal_id=str(snapshot.signal_id or ""),
                        action_id=aid,
                    )
                    break

        clarification_answers = [
            a.model_dump(mode="python") for a in snapshot.agent_memory.clarification_answers
        ]
        if is_clarification and answer_text:
            ask = ""
            for gap in snapshot.gaps:
                if str(gap.field or "") == "operator_decision":
                    ask = str(gap.ask_pl or "")
                    break
            clarification_answers.append(
                ClarificationAnswerItem(
                    ask_pl=ask,
                    answer_pl=answer_text,
                    operator_id=str(operator_id or "").strip(),
                    answered_at=datetime.now(timezone.utc).isoformat(),
                ).model_dump(mode="python")
            )

        memory_dump = snapshot.agent_memory.model_dump(mode="python")
        memory_dump["clarification_answers"] = clarification_answers

        from outbound_receipt import build_ready_for_manual_send_receipt

        receipt_draft_id = ""
        receipt_body_hash = ""
        for item in actions_payload:
            if str(item.get("id") or "").strip() == aid:
                receipt_draft_id = str(item.get("draft_id") or "")
                receipt_body_hash = str(item.get("body_hash") or "")
                break
        delta: dict[str, Any] = {
            "hitl_gate": {"required": False, "reason": ""},
            "actions": actions_payload,
            "gaps": [g.model_dump(mode="python") for g in snapshot.gaps if g.field != "operator_decision"],
            "operational_status": {
                "code": "ready_for_quote",
                "blocking": False,
            },
            "agent_memory": memory_dump,
            "communication_receipt": build_ready_for_manual_send_receipt(
                draft_id=receipt_draft_id,
                body_hash=receipt_body_hash,
            ),
        }
        patched = apply_snapshot_delta(snapshot, delta)
        try:
            new_version = self.store.save_snapshot(patched, expected_version=snapshot.version)
        except AgentConcurrencyError as exc:
            return _error(str(exc), engagement_id=eid)
        final = patched.model_copy(update={"version": new_version})
        approved_action = _find_action(final, aid)
        parent_refs = {
            "parent_policy_decision_id": str(
                approved_action.parent_policy_decision_id
                if approved_action is not None
                else (action.parent_policy_decision_id if action is not None else "")
            ),
            "parent_action_proposal_v2_id": str(
                approved_action.parent_action_proposal_v2_id
                if approved_action is not None
                else (action.parent_action_proposal_v2_id if action is not None else "")
            ),
            "parent_decision_candidate_id": str(
                approved_action.parent_decision_candidate_id
                if approved_action is not None
                else (action.parent_decision_candidate_id if action is not None else "")
            ),
            "source_signal_id": str(
                approved_action.source_signal_id
                if approved_action is not None
                else (action.source_signal_id if action is not None else "")
            ),
        }
        draft_identity = {
            "draft_id": str(approved_action.draft_id if approved_action is not None else ""),
            "revision": int(approved_action.revision if approved_action is not None else 1),
            "body_hash": str(approved_action.body_hash if approved_action is not None else ""),
            "identity_state": str(
                approved_action.identity_state if approved_action is not None else "identity_incomplete"
            ),
            "approved_payload_pl": str(
                (approved_action.payload_pl if approved_action is not None else "") or ""
            ),
        }
        result: dict[str, Any] = {
            "ok": True,
            "engagement_id": eid,
            "action_id": aid,
            "operator_id": str(operator_id or "").strip(),
            "version": new_version,
            "new_status": str(final.operational_status.code or ""),
            "decision_status": "approved",
            "execution_status": "not_applicable",
            "delivery_mode": "manual_operator",
            "effect_started": False,
            "manual_delivery_required": True,
            "adjudication": {
                "event_domain": "adjudication",
                "adjudication_kind": "hitl_action_approved",
                "case_id": final.case_id,
                "engagement_id": eid,
                "action_id": aid,
                "operator_id": str(operator_id or "").strip(),
                "decision_status": "approved",
                "execution_status": "not_applicable",
                "delivery_mode": "manual_operator",
                **parent_refs,
                **draft_identity,
            },
            "snapshot": _snapshot_summary(final),
            **draft_identity,
        }
        if draft_provided and draft_text and not is_clarification:
            result["operator_draft_pl"] = draft_text
            result["operator_draft_applied"] = True
        if is_clarification:
            result["operator_answer_pl"] = answer_text
            result["clarification_answer_applied"] = True
        return result


def mcp_tool_catalog() -> list[dict[str, str]]:
    """Static catalog for docs, fixtures, and smoke gates."""
    return [
        {
            "name": "get_engagement_snapshot",
            "purpose": "Read operator EngagementSnapshot.v2 (summary or full).",
        },
        {
            "name": "list_active_engagements",
            "purpose": "Recent engagements; filters: status, blocking_gaps_only, hitl_required_only.",
        },
        {
            "name": "trigger_agent_run",
            "purpose": "One AgentGraph pass (debug/replay); respects AGENT_RUNTIME_MODE.",
        },
        {
            "name": "approve_hitl_action",
            "purpose": "Operator HITL approve — CAS snapshot, clears hitl_gate, applies operator_draft_pl / operator_answer_pl, enables action.",
        },
        {
            "name": "get_agent_turns",
            "purpose": "Episodic turns from agent_runtime_turns / in-memory journal.",
        },
    ]


def evaluate_agent_mcp_smoke(*, service: AgentMcpService | None = None) -> dict[str, Any]:
    """
    In-process smoke (Gate B / CI): exercise all MCP tools on mock Radlin-shaped snapshot.
    No OpenAI, no Postgres required.
    """
    from agent_runtime.planner import MockSequencePlanner
    from agent_runtime.constitution import load_constitution
    from agent_runtime.graph import AgentGraphEngine
    from agent_runtime.run import AgentRunResult
    from agent_runtime.tools_registry import MockToolRegistry
    from agent_runtime.snapshot_delta import apply_snapshot_delta
    from llm_contracts.engagement_snapshot_v2 import ActionItem

    store = InMemoryOperatorEngagementStore()
    settings = AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=12,
        openai_api_key="sk-smoke",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="",
        kalk_top_agent_key="",
        kalk_top_timeout_sec=4,
        kalk_top_max_retries=3,
    )
    snap = store.init_snapshot_from_signal(
        signal={"signal_id": "sig_mcp_smoke"},
        case_id="case_mcp_smoke",
        engagement_id="eng_mcp_smoke",
    )
    patched = apply_snapshot_delta(
        snap,
        {
            "hitl_gate": {"required": True, "reason": "draft_ready_for_approval"},
            "operational_status": {"code": "enriching", "blocking": True},
            "gaps": [
                {
                    "field": "operator_decision",
                    "severity": "blocking",
                    "ask_pl": "Zatwierdź draft.",
                }
            ],
            "actions": [
                {
                    "id": "draft_reply",
                    "enabled": True,
                    "payload_pl": "Smoke draft",
                    "disabled_reason_pl": None,
                }
            ],
        },
    )
    store.save_snapshot(patched, expected_version=1)
    journal = InMemoryAgentTurnJournal()

    def _fake_run(engagement_id: str, **kwargs: object) -> AgentRunResult:
        loaded = store.load_snapshot(engagement_id)
        assert loaded is not None
        engine = AgentGraphEngine(
            planner=MockSequencePlanner(["extract_facts_from_text"]),
            constitution=load_constitution(),
            tool_registry=MockToolRegistry(),
            turn_journal=journal,
        )
        graph = engine.run(loaded, turn_journal=journal)
        version = store.save_snapshot(graph.snapshot, expected_version=loaded.version)
        final = graph.snapshot.model_copy(update={"version": version})
        return AgentRunResult(snapshot=final, graph=graph, version=version)

    svc = service or AgentMcpService(
        store=store,
        settings=settings,
        turn_journal=journal,
        run_agent=_fake_run,
    )
    checks: dict[str, bool] = {}
    r1 = svc.get_engagement_snapshot(engagement_id="eng_mcp_smoke", include_full=True)
    checks["get_snapshot"] = bool(r1.get("ok")) and "full" in r1.get("snapshot", {})
    r2 = svc.list_active_engagements(hitl_required_only=True)
    checks["list_hitl"] = r2.get("count") == 1
    r3 = svc.list_active_engagements(blocking_gaps_only=True)
    checks["list_blocking"] = r3.get("count") == 1
    r4 = svc.trigger_agent_run(engagement_id="eng_mcp_smoke")
    checks["trigger_run"] = bool(r4.get("ok"))
    r5 = svc.get_agent_turns(engagement_id="eng_mcp_smoke")
    if not (bool(r5.get("ok")) and r5.get("count", 0) >= 1):
        from agent_runtime.tool_result import ToolCallPlan, ToolResult

        journal.append_turn(
            engagement_id="eng_mcp_smoke",
            snapshot_version=int(r4.get("version") or 1),
            trace_id="sig_mcp_smoke",
            plan=ToolCallPlan(tool_name="extract_facts_from_text", arguments={}),
            result=ToolResult(status="ok", turn_summary_pl="Smoke turn (synthetic)."),
        )
        r5 = svc.get_agent_turns(engagement_id="eng_mcp_smoke")
    checks["get_turns"] = bool(r5.get("ok")) and r5.get("count", 0) >= 1
    current = store.load_snapshot("eng_mcp_smoke")
    assert current is not None
    with_hitl = apply_snapshot_delta(
        current,
        {
            "hitl_gate": {"required": True, "reason": "draft_ready_for_approval"},
            "operational_status": {"code": "pending_operator"},
            "actions": [
                ActionItem(
                    id="draft_reply",
                    enabled=True,
                    payload_pl="Smoke draft",
                    disabled_reason_pl=None,
                ).model_dump(mode="python")
            ],
        },
    )
    store.save_snapshot(with_hitl, expected_version=current.version)
    checks["dispatch_get_engagement_snapshot"] = "ok" in dispatch_mcp_tool(
        svc, "get_engagement_snapshot", {"engagement_id": "eng_mcp_smoke"}
    )
    checks["dispatch_list_active_engagements"] = "ok" in dispatch_mcp_tool(
        svc, "list_active_engagements", {"hitl_required_only": True}
    )
    checks["dispatch_trigger_agent_run"] = "ok" in dispatch_mcp_tool(
        svc, "trigger_agent_run", {"engagement_id": "eng_mcp_smoke"}
    )
    checks["dispatch_get_agent_turns"] = "ok" in dispatch_mcp_tool(
        svc, "get_agent_turns", {"engagement_id": "eng_mcp_smoke"}
    )
    snap_b = store.init_snapshot_from_signal(
        signal={"signal_id": "sig_mcp_smoke_b"},
        case_id="case_mcp_smoke_b",
        engagement_id="eng_mcp_smoke_b",
    )
    hitl_b = apply_snapshot_delta(
        snap_b,
        {
            "hitl_gate": {"required": True, "reason": "draft_ready_for_approval"},
            "actions": [
                ActionItem(
                    id="draft_reply",
                    enabled=True,
                    payload_pl="B",
                    disabled_reason_pl=None,
                ).model_dump(mode="python")
            ],
        },
    )
    store.save_snapshot(hitl_b, expected_version=1)
    checks["dispatch_approve_hitl_action"] = bool(
        dispatch_mcp_tool(
            svc,
            "approve_hitl_action",
            {
                "engagement_id": "eng_mcp_smoke_b",
                "action_id": "draft_reply",
                "operator_id": "smoke",
            },
        ).get("ok")
    )
    r6 = svc.approve_hitl_action(
        engagement_id="eng_mcp_smoke",
        action_id="draft_reply",
        operator_id="smoke_operator",
    )
    checks["approve_hitl"] = bool(r6.get("ok"))
    passed = sum(1 for v in checks.values() if v)
    total = len(checks)
    return {
        "ok": passed == total,
        "passed": passed,
        "total": total,
        "checks": checks,
        "tools": list(MCP_TOOL_NAMES),
    }


def build_agent_mcp_doctor_check() -> dict[str, Any]:
    """Doctor slice: MCP SDK + agent runtime readiness (informational; does not fail agent_runtime)."""
    settings = load_agent_runtime_settings()
    warnings: list[str] = []
    mcp_installed = True
    try:
        import mcp  # noqa: F401
    except ImportError:
        mcp_installed = False
        warnings.append("python package 'mcp' not installed (pip install mcp)")
    if not settings.enabled:
        warnings.append(
            "agent runtime off — read tools work; trigger_run needs AGENT_RUNTIME_MODE=prep|primary or AGENT_MCP_ALLOW_DEBUG=1"
        )
    mode = str(settings.mode or "").strip().lower()
    if mode == "legacy":
        warnings.append("AGENT_RUNTIME_MODE=legacy — trigger_agent_run blocked; approve still mutates snapshot")
    status = "ok"
    if not mcp_installed:
        status = "optional"
    elif warnings:
        status = "warn"
    return {
        "id": "agent_runtime_mcp",
        "status": status,
        "mcp_sdk_installed": mcp_installed,
        "warnings": warnings,
        "tools": list(MCP_TOOL_NAMES),
        "mode": mode,
        "enabled": settings.enabled,
    }


def dispatch_mcp_tool(service: AgentMcpService, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Route tool name → service method (used by stdio server and tests)."""
    args = dict(arguments)
    if name == "get_engagement_snapshot":
        return service.get_engagement_snapshot(
            engagement_id=str(args.get("engagement_id") or ""),
            case_id=str(args.get("case_id") or ""),
            include_full=bool(args.get("include_full")),
        )
    if name == "list_active_engagements":
        return service.list_active_engagements(
            status=str(args.get("status") or ""),
            blocking_gaps_only=bool(args.get("blocking_gaps_only")),
            hitl_required_only=bool(args.get("hitl_required_only")),
            limit=int(args.get("limit") or DEFAULT_LIST_LIMIT),
        )
    if name == "trigger_agent_run":
        signal = args.get("signal")
        return service.trigger_agent_run(
            engagement_id=str(args.get("engagement_id") or ""),
            signal=signal if isinstance(signal, dict) else None,
            allow_when_disabled=bool(args.get("allow_when_disabled")),
            force=bool(args.get("force")),
        )
    if name == "approve_hitl_action":
        draft_raw = args.get("operator_draft_pl")
        if draft_raw is None:
            draft_raw = args.get("draft_pl")
        answer_raw = args.get("operator_answer_pl")
        if answer_raw is None:
            answer_raw = args.get("clarification_answer_pl")
        expected_raw = args.get("expected_body_hash")
        expected_rev_raw = args.get("expected_revision")
        expected_revision: int | None = None
        if expected_rev_raw is not None and str(expected_rev_raw).strip() != "":
            try:
                expected_revision = int(expected_rev_raw)
            except (TypeError, ValueError):
                expected_revision = None
        return service.approve_hitl_action(
            engagement_id=str(args.get("engagement_id") or ""),
            action_id=str(args.get("action_id") or ""),
            operator_id=str(args.get("operator_id") or ""),
            operator_draft_pl=str(draft_raw).strip() if draft_raw is not None else None,
            operator_answer_pl=str(answer_raw).strip() if answer_raw is not None else None,
            expected_body_hash=str(expected_raw).strip() if expected_raw is not None else None,
            expected_revision=expected_revision,
        )
    if name == "get_agent_turns":
        return service.get_agent_turns(
            engagement_id=str(args.get("engagement_id") or ""),
            limit=int(args.get("limit") or DEFAULT_TURNS_LIMIT),
        )
    return _error(f"unknown tool: {name}")


def _snapshot_summary(snapshot: EngagementSnapshotV2) -> dict[str, Any]:
    return {
        "engagement_id": snapshot.engagement_id,
        "case_id": snapshot.case_id,
        "version": snapshot.version,
        "trace_id": snapshot.trace_id,
        "operational_status": snapshot.operational_status.model_dump(mode="python"),
        "hvac_profile": snapshot.hvac_profile.model_dump(mode="python"),
        "gaps_count": len(snapshot.gaps),
        "blocking_gaps": sum(1 for g in snapshot.gaps if g.severity == "blocking"),
        "hitl_gate": snapshot.hitl_gate.model_dump(mode="python"),
        "actions": [
            {
                "id": a.id,
                "enabled": a.enabled,
                "payload_pl_preview": (a.payload_pl or "")[:120],
                "parent_policy_decision_id": a.parent_policy_decision_id,
                "parent_action_proposal_v2_id": a.parent_action_proposal_v2_id,
                "parent_decision_candidate_id": a.parent_decision_candidate_id,
                "source_signal_id": a.source_signal_id,
                "draft_id": a.draft_id,
                "revision": a.revision,
                "body_hash": a.body_hash,
                "case_id": a.case_id,
                "identity_state": a.identity_state,
            }
            for a in snapshot.actions
        ],
        "tool_calls_count": len(snapshot.agent_memory.tool_calls),
    }


def _turn_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "turn_id": row.get("turn_id"),
        "tool_name": row.get("tool_name"),
        "tool_status": row.get("tool_status"),
        "turn_summary_pl": row.get("turn_summary_pl"),
        "plan_correlation": row.get("plan_correlation") or {},
        "snapshot_version": row.get("snapshot_version"),
        "created_at": row.get("created_at"),
    }


def _find_action(snapshot: EngagementSnapshotV2, action_id: str) -> ActionItem | None:
    for item in snapshot.actions:
        if str(item.id or "") == action_id:
            return item
    return None


def _has_blocking_gap(snapshot: EngagementSnapshotV2) -> bool:
    return any(g.severity == "blocking" for g in snapshot.gaps) or bool(
        snapshot.operational_status.blocking
    )


def _clamp_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = DEFAULT_LIST_LIMIT
    return max(1, min(value, MAX_LIST_LIMIT))


def _clamp_turns_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = DEFAULT_TURNS_LIMIT
    return max(1, min(value, MAX_TURNS_LIMIT))


def _mcp_debug_override_enabled() -> bool:
    raw = os.getenv("AGENT_MCP_ALLOW_DEBUG", "")
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _error(message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": message}
    payload.update(extra)
    return payload
