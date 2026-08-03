"""SLICE-3B: policy-to-execution action spine, detection-only."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from action_proposal_v2 import build_action_proposals_v2
from agent_runtime.agent_reconcile import build_policy_action_envelope_handoff
from agent_runtime.constitution import load_constitution
from agent_runtime.graph import AgentGraphEngine, _apply_tool_result, _ground_current_signal
from agent_runtime.mcp_service import AgentMcpService
from agent_runtime.openai_agent_client import _compact_view
from agent_runtime.policy_action_spine import (
    ACTION_INTENT_TOOL_MAPPING_CLASSIFICATION,
    annotate_action_parent_refs,
    correlate_tool_plan,
    evaluate_semantic_policy_plan_consistency,
    persist_policy_action_spine,
    project_policy_action_envelope,
)
from agent_runtime.settings import load_agent_runtime_settings
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import InMemoryOperatorEngagementStore, build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from agent_runtime.tools_registry import MockToolRegistry
from agent_runtime.turn_journal import InMemoryAgentTurnJournal
from llm_contracts.engagement_snapshot_v2 import (
    ActionItem,
    CaseUnderstandingProjection,
    PolicyActionEnvelopeV1,
    SemanticPolicyPlanConsistencyV1,
)
from mailbox_memory import InMemoryMailboxMemoryStore
from policy_decision import build_policy_decision
from agent_hitl_bridge import execute_hitl_send_from_bridge_row


def _case_intelligence(
    *,
    case_id: str = "case_3b",
    source_message_id: str = "msg_3b_1",
    candidate_suffix: str = "1",
    created_at: str = "2026-07-27T12:00:00Z",
    expires_at: str = "",
    policy_status: str = "APPROVED",
) -> dict:
    candidate = {
        "schema_version": "decision_candidate.v1",
        "decision_candidate_id": f"dc_3b_{candidate_suffix}",
        "case_id": case_id,
        # The current producer names this source_signal_id but fills it with message_id.
        "source_signal_id": source_message_id,
        "next_best_action": "answer_customer",
        "evidence_refs": [{"evidence_id": f"ev_{candidate_suffix}", "source_ref": source_message_id}],
    }
    report = {
        "status": policy_status,
        "effective_risk_class": "low",
        "policy_basis": ["policy_test_basis"],
        "failed_rules": [] if policy_status == "APPROVED" else ["blocked_for_test"],
        "warnings": [],
        "requires_review": policy_status != "APPROVED",
    }
    decision = build_policy_decision(
        policy_report=report,
        decision_candidate_id=candidate["decision_candidate_id"],
        decision_candidate=candidate,
        dry_run_only=False,
    )
    proposal = build_action_proposals_v2(
        decision_candidate=candidate,
        policy_decision=decision,
        primary_action_type="prepare_reply",
        dry_run_only=False,
    )[0]
    decision["created_at"] = created_at
    proposal["created_at"] = created_at
    proposal["expires_at"] = expires_at
    return {
        "decision_candidate": candidate,
        "policy_decision": decision,
        "action_proposals_v2": [proposal],
    }


def _persist(
    store: InMemoryMailboxMemoryStore,
    intelligence: dict,
    *,
    case_id: str = "case_3b",
    source_signal_id: str = "sig_3b_1",
    source_message_id: str = "msg_3b_1",
) -> dict:
    return persist_policy_action_spine(
        store,
        case_intelligence_result=intelligence,
        case_id=case_id,
        source_signal_id=source_signal_id,
        source_message_id=source_message_id,
    )


def _current_envelope(
    store: InMemoryMailboxMemoryStore | None = None,
) -> PolicyActionEnvelopeV1:
    target = store or InMemoryMailboxMemoryStore()
    if store is None:
        _persist(target, _case_intelligence())
    return project_policy_action_envelope(
        target,
        case_id="case_3b",
        source_signal_id="sig_3b_1",
        source_message_id="msg_3b_1",
        now="2026-07-27T12:30:00Z",
    )


def test_mapping_truth_is_explicitly_no_safe_mapping() -> None:
    assert ACTION_INTENT_TOOL_MAPPING_CLASSIFICATION == "NO_SAFE_MAPPING_EXISTS"


def test_policy_and_apv2_persist_idempotently_and_survive_snapshot_loss() -> None:
    store = InMemoryMailboxMemoryStore()
    intelligence = _case_intelligence()

    first = _persist(store, intelligence)
    second = _persist(store, intelligence)

    assert first == {"policy_decision_inserted": True, "action_proposals_v2_inserted": 1}
    assert second == {"policy_decision_inserted": False, "action_proposals_v2_inserted": 0}
    decision_id = intelligence["policy_decision"]["policy_decision_id"]
    proposal_id = intelligence["action_proposals_v2"][0]["proposal_id"]
    assert store.fetch_policy_decision(decision_id)["source_signal_id"] == "sig_3b_1"
    assert store.fetch_action_proposal_v2(proposal_id)["source_message_id"] == "msg_3b_1"

    # No EngagementSnapshot is needed to reconstruct the envelope.
    envelope = _current_envelope(store)
    assert envelope.freshness == "current"
    assert envelope.policy_decision_id == decision_id
    assert envelope.action_proposal_id == proposal_id


def test_another_signal_or_case_never_overwrites_the_existing_record() -> None:
    store = InMemoryMailboxMemoryStore()
    first = _case_intelligence(candidate_suffix="1")
    second = _case_intelligence(
        case_id="case_3b_other",
        source_message_id="msg_3b_2",
        candidate_suffix="2",
        created_at="2026-07-27T13:00:00Z",
    )
    _persist(store, first)
    _persist(
        store,
        second,
        case_id="case_3b_other",
        source_signal_id="sig_3b_2",
        source_message_id="msg_3b_2",
    )

    assert len(store.fetch_policy_decisions(limit=20)) == 2
    assert len(store.fetch_action_proposals_v2(limit=20)) == 2
    assert _current_envelope(store).decision_candidate_id == "dc_3b_1"


def test_foreign_case_or_message_correlation_is_rejected_without_a_write() -> None:
    store = InMemoryMailboxMemoryStore()
    result = _persist(
        store,
        _case_intelligence(),
        case_id="foreign_case",
        source_message_id="foreign_message",
    )
    assert result == {"policy_decision_inserted": False, "action_proposals_v2_inserted": 0}
    assert store.fetch_policy_decisions(limit=20) == []
    assert store.fetch_action_proposals_v2(limit=20) == []


def test_projection_distinguishes_current_stale_and_unavailable() -> None:
    current = _current_envelope()
    assert current.freshness == "current"
    assert current.reason_codes == []

    stale_store = InMemoryMailboxMemoryStore()
    _persist(
        stale_store,
        _case_intelligence(expires_at="2026-07-27T12:15:00Z"),
    )
    stale = _current_envelope(stale_store)
    assert stale.freshness == "stale"
    assert "proposal_expired" in stale.reason_codes

    unavailable = project_policy_action_envelope(
        InMemoryMailboxMemoryStore(),
        case_id="case_3b",
        source_signal_id="sig_3b_1",
        source_message_id="msg_3b_1",
        now="2026-07-27T12:30:00Z",
    )
    assert unavailable.freshness == "unavailable"
    assert unavailable.action_intent == ""
    assert "canonical_action_proposal_v2_not_found" in unavailable.reason_codes


def test_later_decision_replaces_the_earlier_envelope_for_the_same_source() -> None:
    store = InMemoryMailboxMemoryStore()
    _persist(store, _case_intelligence(candidate_suffix="old", created_at="2026-07-27T12:00:00Z"))
    _persist(store, _case_intelligence(candidate_suffix="new", created_at="2026-07-27T13:00:00Z"))

    envelope = project_policy_action_envelope(
        store,
        case_id="case_3b",
        source_signal_id="sig_3b_1",
        source_message_id="msg_3b_1",
        now="2026-07-27T13:01:00Z",
    )
    assert envelope.decision_candidate_id == "dc_3b_new"


def test_reconcile_handoff_persists_before_it_projects() -> None:
    store = InMemoryMailboxMemoryStore()
    persisted, envelope = build_policy_action_envelope_handoff(
        store=store,
        case_intelligence_result=_case_intelligence(),
        case_id="case_3b",
        source_signal_id="sig_3b_1",
        source_message_id="msg_3b_1",
    )
    assert persisted["policy_decision_inserted"] is True
    assert persisted["action_proposals_v2_inserted"] == 1
    assert envelope["freshness"] == "current"
    assert envelope["source_signal_id"] == "sig_3b_1"
    assert envelope["source_message_id"] == "msg_3b_1"


def test_new_signal_sets_or_clears_policy_envelope_and_old_telemetry() -> None:
    base = build_initial_snapshot(
        case_id="case_3b",
        engagement_id="eng_3b",
        signal_id="sig_3b_1",
        trace_id="trace_3b",
    )
    grounded = _ground_current_signal(
        base,
        {"policy_action_envelope": _current_envelope().model_dump(mode="python")},
    )
    assert grounded.policy_action_envelope is not None
    observed = grounded.model_copy(
        update={
            "semantic_policy_plan_consistency": SemanticPolicyPlanConsistencyV1(
                status="not_evaluable",
                reason_codes=["no_formal_action_intent_tool_mapping"],
            )
        }
    )
    next_signal = _ground_current_signal(observed, {"subject": "Nowsza wiadomosc"})
    assert next_signal.policy_action_envelope is None
    assert next_signal.semantic_policy_plan_consistency is None


def test_envelope_is_a_separate_structured_planner_input_and_keeps_brain1_context() -> None:
    snapshot = build_initial_snapshot(
        case_id="case_3b",
        engagement_id="eng_3b",
        signal_id="sig_3b_1",
        trace_id="trace_3b",
    ).model_copy(
        update={
            "policy_action_envelope": _current_envelope(),
            "case_understanding": CaseUnderstandingProjection(
                source_signal_id="msg_3b_1",
                essence_pl="Klient oczekuje odpowiedzi.",
            ),
        }
    )
    view = _compact_view(snapshot)

    assert view["policy_action_envelope"]["action_intent"] == "prepare_reply_draft"
    assert view["brain1_context"]["understanding"]["essence_pl"] == "Klient oczekuje odpowiedzi."
    assert "policy_action_envelope" not in " ".join(view["recent_steps"])


def test_tool_plan_correlation_is_additive_and_does_not_change_tool_behavior() -> None:
    raw = ToolCallPlan(tool_name="generate_draft_reply", arguments={"intent": "quote"})
    correlated = correlate_tool_plan(raw, _current_envelope())

    assert correlated.tool_name == raw.tool_name
    assert correlated.arguments == raw.arguments
    assert correlated.policy_decision_id
    assert correlated.action_proposal_id
    assert correlated.correlation_status == "correlated"

    without_envelope = correlate_tool_plan(raw, None)
    assert without_envelope.tool_name == raw.tool_name
    assert without_envelope.arguments == raw.arguments
    assert without_envelope.policy_decision_id == ""
    assert without_envelope.correlation_status == "missing_policy_envelope"


def test_graph_captures_structured_input_ids_and_telemetry_without_changing_tool() -> None:
    class _CapturePlanner:
        def __init__(self) -> None:
            self.view: dict = {}

        def plan_next_tool(self, *, snapshot, available_tools, constitution):
            self.view = _compact_view(snapshot)
            return ToolCallPlan(tool_name="report_gaps_and_stop", arguments={})

    envelope = _current_envelope()
    planner = _CapturePlanner()
    journal = InMemoryAgentTurnJournal()
    constitution = load_constitution()
    engine = AgentGraphEngine(
        planner=planner,
        constitution=constitution,
        tool_registry=MockToolRegistry(),
        turn_journal=journal,
    )
    snapshot = build_initial_snapshot(
        case_id="case_3b",
        engagement_id="eng_3b",
        signal_id="sig_3b_1",
        trace_id="trace_3b",
    )
    context = ToolExecutionContext.from_snapshot(
        snapshot,
        signal_payload={
            "policy_action_envelope": envelope.model_dump(mode="python"),
        },
        constitution=constitution,
    )
    result = engine.run(snapshot, context=context)

    assert result.turns[0].tool_name == "report_gaps_and_stop"
    assert result.snapshot.hitl_gate.required is True
    assert planner.view["policy_action_envelope"]["action_proposal_id"] == (
        envelope.action_proposal_id
    )
    assert result.snapshot.semantic_policy_plan_consistency.status == "not_evaluable"
    journal_row = journal.list_turns("eng_3b")[0]
    assert journal_row["plan_correlation"] == {
        "policy_decision_id": envelope.policy_decision_id,
        "action_proposal_id": envelope.action_proposal_id,
        "status": "correlated",
    }


def test_policy_conflict_blocks_actionable_tool_execution() -> None:
    class _DraftPlanner:
        def plan_next_tool(self, *, snapshot, available_tools, constitution):
            return ToolCallPlan(
                tool_name="generate_draft_reply",
                arguments={"intent": "quote"},
            )

    class _DraftRegistry:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, plan, *, context):
            self.calls += 1
            return ToolResult(
                status="ok",
                turn_summary_pl="Draft fixture.",
                snapshot_delta={
                    "actions": [
                        {
                            "id": "draft_reply",
                            "enabled": True,
                            "payload_pl": "Draft fixture",
                        }
                    ],
                    "hitl_gate": {
                        "required": True,
                        "reason": "draft_ready_for_approval",
                    },
                    "operational_status": {"code": "pending_operator"},
                },
            )

    blocked = _current_envelope().model_copy(
        update={"policy_status": "blocked", "allowed_by_policy": False}
    )
    registry = _DraftRegistry()
    constitution = load_constitution()
    snapshot = build_initial_snapshot(
        case_id="case_3b",
        engagement_id="eng_3b_blocked",
        signal_id="sig_3b_1",
        trace_id="trace_3b",
    )
    result = AgentGraphEngine(
        planner=_DraftPlanner(),
        constitution=constitution,
        tool_registry=registry,
    ).run(
        snapshot,
        context=ToolExecutionContext.from_snapshot(
            snapshot,
            signal_payload={
                "policy_action_envelope": blocked.model_dump(mode="python"),
            },
            constitution=constitution,
        ),
    )

    assert registry.calls == 0, "RP-30 enforces policy_blocks_actionable_tool"
    assert result.turns[0].tool_name == "generate_draft_reply"
    assert result.turns[0].tool_status == "error"
    assert result.snapshot.hitl_gate.required is True
    assert result.snapshot.semantic_policy_plan_consistency.status == "conflicting"
    assert "policy_blocks_actionable_tool" in (
        result.snapshot.semantic_policy_plan_consistency.reason_codes
    )


def test_detection_is_deterministic_and_never_rewrites_the_plan() -> None:
    envelope = _current_envelope()
    raw = ToolCallPlan(tool_name="search_gmail_thread", arguments={"thread_id": "t"})
    plan = correlate_tool_plan(raw, envelope)
    telemetry = evaluate_semantic_policy_plan_consistency(envelope, plan)

    assert telemetry.status == "not_evaluable"
    assert telemetry.reason_codes == ["no_formal_action_intent_tool_mapping"]
    assert plan.tool_name == raw.tool_name
    assert plan.arguments == raw.arguments

    wrong = plan.model_copy(update={"action_proposal_id": "apv2_wrong"})
    conflict = evaluate_semantic_policy_plan_consistency(envelope, wrong)
    assert conflict.status == "conflicting"
    assert "action_proposal_id_mismatch" in conflict.reason_codes


def test_detection_reports_missing_stale_and_policy_blocked_states() -> None:
    envelope = _current_envelope()
    missing = evaluate_semantic_policy_plan_consistency(
        envelope,
        ToolCallPlan(tool_name="search_gmail_thread", arguments={}),
    )
    assert missing.status == "missing_plan_correlation"

    stale = envelope.model_copy(update={"freshness": "stale", "reason_codes": ["proposal_expired"]})
    stale_result = evaluate_semantic_policy_plan_consistency(stale, correlate_tool_plan(
        ToolCallPlan(tool_name="search_gmail_thread", arguments={}), stale
    ))
    assert stale_result.status == "stale_policy_envelope"

    blocked = envelope.model_copy(
        update={"policy_status": "blocked", "allowed_by_policy": False}
    )
    blocked_plan = correlate_tool_plan(
        ToolCallPlan(tool_name="generate_draft_reply", arguments={"intent": "quote"}),
        blocked,
    )
    blocked_result = evaluate_semantic_policy_plan_consistency(blocked, blocked_plan)
    assert blocked_result.status == "conflicting"
    assert "policy_blocks_actionable_tool" in blocked_result.reason_codes

    unavailable = evaluate_semantic_policy_plan_consistency(None, ToolCallPlan(
        tool_name="search_gmail_thread", arguments={}
    ))
    assert unavailable.status == "missing_policy_envelope"


def test_tool_delta_cannot_overwrite_envelope_or_consistency_telemetry() -> None:
    envelope = _current_envelope()
    consistency = SemanticPolicyPlanConsistencyV1(
        status="not_evaluable",
        reason_codes=["no_formal_action_intent_tool_mapping"],
        policy_decision_id=envelope.policy_decision_id,
        action_proposal_id=envelope.action_proposal_id,
        tool_name="search_gmail_thread",
        mapping_classification=ACTION_INTENT_TOOL_MAPPING_CLASSIFICATION,
    )
    snapshot = build_initial_snapshot(
        case_id="case_3b",
        engagement_id="eng_3b",
        signal_id="sig_3b_1",
        trace_id="trace_3b",
    ).model_copy(
        update={
            "policy_action_envelope": envelope,
            "semantic_policy_plan_consistency": consistency,
        }
    )
    hostile = ToolResult(
        status="ok",
        snapshot_delta={
            "policy_action_envelope": {
                "freshness": "current",
                "action_intent": "FORGED",
            },
            "semantic_policy_plan_consistency": {
                "status": "consistent",
                "reason_codes": [],
            },
            "operational_status": {"code": "ready_for_quote"},
        },
    )
    updated = _apply_tool_result(
        snapshot,
        ToolCallPlan(tool_name="search_gmail_thread", arguments={}),
        hostile,
    )
    assert updated.policy_action_envelope == envelope
    assert updated.semantic_policy_plan_consistency == consistency
    assert updated.operational_status.code == "ready_for_quote"


def test_action_parent_refs_are_additive_and_legacy_actions_still_validate() -> None:
    envelope = _current_envelope()
    plan = correlate_tool_plan(
        ToolCallPlan(tool_name="generate_draft_reply", arguments={"intent": "quote"}),
        envelope,
    )
    delta = {
        "actions": [
            {
                "id": "draft_reply",
                "enabled": True,
                "payload_pl": "Draft",
                "disabled_reason_pl": None,
            }
        ]
    }
    annotated = annotate_action_parent_refs(delta, plan=plan, envelope=envelope)
    action = ActionItem.model_validate(annotated["actions"][0])

    assert action.parent_policy_decision_id == envelope.policy_decision_id
    assert action.parent_action_proposal_v2_id == envelope.action_proposal_id
    assert action.parent_decision_candidate_id == envelope.decision_candidate_id
    assert action.source_signal_id == envelope.source_signal_id

    legacy = ActionItem(id="draft_reply", enabled=True, payload_pl="Legacy")
    assert legacy.parent_policy_decision_id == ""
    assert annotate_action_parent_refs(delta, plan=ToolCallPlan(
        tool_name="generate_draft_reply", arguments={}
    ), envelope=None) == delta


def test_parent_refs_survive_approval_hitl_state_and_execution_result_replay() -> None:
    envelope = _current_envelope()
    action = ActionItem(
        id="draft_reply",
        enabled=True,
        payload_pl="Draft",
        parent_policy_decision_id=envelope.policy_decision_id,
        parent_action_proposal_v2_id=envelope.action_proposal_id,
        parent_decision_candidate_id=envelope.decision_candidate_id,
        source_signal_id=envelope.source_signal_id,
    )
    snapshot = build_initial_snapshot(
        case_id="case_3b",
        engagement_id="eng_3b",
        signal_id="sig_3b_1",
        trace_id="trace_3b",
    )
    gated = apply_snapshot_delta(
        snapshot,
        {
            "actions": [action.model_dump(mode="python")],
            "hitl_gate": {"required": True, "reason": "draft_ready_for_approval"},
        },
    )
    operator_store = InMemoryOperatorEngagementStore()
    operator_store.insert_snapshot(gated)
    service = AgentMcpService(
        store=operator_store,
        settings=load_agent_runtime_settings(),
    )

    approval = service.approve_hitl_action(
        engagement_id="eng_3b",
        action_id="draft_reply",
        operator_id="operator_3b",
    )
    assert approval["ok"] is True
    assert approval["adjudication"]["parent_policy_decision_id"] == (
        envelope.policy_decision_id
    )
    assert approval["adjudication"]["parent_action_proposal_v2_id"] == (
        envelope.action_proposal_id
    )

    mailbox_store = InMemoryMailboxMemoryStore()
    mailbox_store.upsert_case(
        {
            "case_id": "case_3b",
            "case_family": "mail_case",
            "status": "open",
            "metadata": {},
        }
    )
    mailbox_runtime = SimpleNamespace(store=mailbox_store, bootstrap=lambda: None)
    settings = SimpleNamespace(
        daszek_operational_feed_auto_push_enabled=False,
        mailbox_memory_database_url="",
    )
    row = {
        "queue_id": "queue_3b",
        "engagement_id": "eng_3b",
        "case_id": "case_3b",
        "action_id": "draft_reply",
        "operator_id": "operator_3b",
    }

    def _execute(**kwargs):
        kwargs["on_effect_start"]()
        return {
            "executed": True,
            "effect_started": True,
            "decision_status": "executed",
            "mode": "bounded_dry_run",
        }

    with patch("agent_hitl_bridge.AgentMcpService.from_env", return_value=service):
        with patch(
            "agent_hitl_bridge.build_mailbox_memory_runtime",
            return_value=mailbox_runtime,
        ):
            with patch(
                "agent_hitl_bridge.best_effort_push_engagement_feed_after_hitl",
                return_value={"skipped": True},
            ):
                with patch(
                    "agent_hitl_bridge.execute_hitl_gmail_send",
                    side_effect=_execute,
                ) as executor:
                    first = execute_hitl_send_from_bridge_row(
                        row=row,
                        settings=settings,
                    )
                    second = execute_hitl_send_from_bridge_row(
                        row=row,
                        settings=settings,
                    )

    assert first["parent_refs"] == second["parent_refs"]
    assert first["parent_refs"]["parent_policy_decision_id"] == (
        envelope.policy_decision_id
    )
    assert executor.call_count == 1
    state = mailbox_store.fetch_case("case_3b")["metadata"]["agent_hitl_send_states"][
        "queue_3b"
    ]
    assert state["parent_refs"]["parent_action_proposal_v2_id"] == (
        envelope.action_proposal_id
    )
    execution_results = mailbox_store.fetch_execution_results(
        case_id="case_3b",
        limit=20,
    )
    assert len(execution_results) == 1
    assert execution_results[0]["result_payload"]["parent_refs"] == first["parent_refs"]
    assert execution_results[0]["policy_result"] == first["parent_refs"]
