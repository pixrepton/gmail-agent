"""AI-OS-CANONICAL-DRAFT-IDENTITY-01.

Reproduces the defect found live in the post-Repair-baseline 38-case run
(`INT-04`, `generate_draft_reply`, real LLM call, no mock): the operator-facing
draft action reached `final_actions` with real content but with
`parent_policy_decision_id`/`parent_action_proposal_v2_id`/
`parent_decision_candidate_id`/`source_signal_id` all empty strings, and (before
this fix) no `draft_id`/`body_hash`/`case_id`/`identity_state` at all.

This does not require a live LLM call: `generate_draft_reply`'s handler is a
deterministic template composer (Model A -- see
`test_generate_draft_reply_contract.py`'s docstring), so calling it directly with
the same `ToolCallPlan`/`ToolExecutionContext` shape the real turn loop uses is a
faithful, production-shaped reproduction of the real INT-04 turn.
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.draft_identity import compute_body_hash, compute_draft_id
from agent_runtime.mcp_service import AgentMcpService
from agent_runtime.policy_action_spine import annotate_action_parent_refs
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import InMemoryOperatorEngagementStore
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan
from agent_runtime.tools.handlers import generate_draft_reply
from llm_contracts.engagement_snapshot_v2 import (
    ActionItem,
    EngagementSnapshotV2,
    HitlGate,
    OperationalStatus,
    PolicyActionEnvelopeV1,
)


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=12,
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="",
        kalk_top_agent_key="",
        kalk_top_timeout_sec=4,
        kalk_top_max_retries=3,
    )


def _snapshot(**kwargs: object) -> EngagementSnapshotV2:
    base = {
        "engagement_id": "eng_int04",
        "case_id": "case_recovery_INT-04",
        "signal_id": "case_recovery_INT-04_current",
        "version": 1,
        "trace_id": "trace_recovery_INT-04",
        "operational_status": {"code": "enriching", "steps_remaining": 8},
        "hvac_profile": {"location": {}},
        "gaps": [],
        "agent_memory": {
            "reasoning_trace": [],
            "tool_calls": [],
            "constitution_sections_used": [],
        },
        "actions": [],
        "hitl_gate": {"required": False, "reason": ""},
    }
    base.update(kwargs)
    return EngagementSnapshotV2.model_validate(base)


def _plan(**kwargs: object) -> ToolCallPlan:
    base = {"tool_name": "generate_draft_reply", "arguments": {"intent": "missing_info"}}
    base.update(kwargs)
    return ToolCallPlan(**base)


def _materialize_draft(
    snapshot: EngagementSnapshotV2,
    plan: ToolCallPlan,
    envelope: PolicyActionEnvelopeV1 | None = None,
) -> EngagementSnapshotV2:
    """Production-shaped turn merge: handler → annotate_action_parent_refs → apply_snapshot_delta."""
    ctx = ToolExecutionContext.from_snapshot(snapshot)
    result = generate_draft_reply(plan, ctx)
    assert result.status == "ok", result.turn_summary_pl
    annotated = annotate_action_parent_refs(
        result.snapshot_delta, plan=plan, envelope=envelope
    )
    return apply_snapshot_delta(snapshot, annotated)


class TestDraftIdentityPopulatedAtCreation:
    def test_int04_shaped_call_now_gets_a_stable_draft_id_and_body_hash(self) -> None:
        snapshot = _snapshot()
        merged = _materialize_draft(snapshot, _plan())
        action = merged.actions[0]
        assert action.draft_id, "a real draft must always get a draft_id"
        assert action.draft_id.startswith("draft_")
        assert action.body_hash == compute_body_hash(action.payload_pl or "")
        assert action.case_id == "case_recovery_INT-04"
        assert action.source_signal_id == "case_recovery_INT-04_current"
        assert action.revision == 1

    def test_draft_id_is_stable_across_identical_re_runs(self) -> None:
        snapshot = _snapshot()
        first = _materialize_draft(snapshot, _plan())
        second = _materialize_draft(snapshot, _plan())
        assert first.actions[0].draft_id == second.actions[0].draft_id

    def test_draft_id_matches_the_deterministic_helper_directly(self) -> None:
        snapshot = _snapshot()
        merged = _materialize_draft(snapshot, _plan())
        expected = compute_draft_id(
            case_id="case_recovery_INT-04",
            source_signal_id="case_recovery_INT-04_current",
            action_id="draft_reply",
        )
        assert merged.actions[0].draft_id == expected

    def test_without_a_correlated_policy_envelope_identity_is_honestly_incomplete(self) -> None:
        """INT-04/harness shape: no correlated envelope → parent refs empty, state explicit."""
        snapshot = _snapshot()
        merged = _materialize_draft(snapshot, _plan(), envelope=None)
        action = merged.actions[0]
        assert action.parent_policy_decision_id == ""
        assert action.parent_action_proposal_v2_id == ""
        assert action.parent_decision_candidate_id == ""
        assert action.identity_state == "identity_incomplete"
        # source_signal_id from snapshot is available and must be preserved (not fabricated)
        assert action.source_signal_id == "case_recovery_INT-04_current"

    def test_correlated_envelope_sets_complete_identity_and_parent_refs(self) -> None:
        envelope = PolicyActionEnvelopeV1(
            decision_candidate_id="dc_1",
            policy_decision_id="pd_1",
            action_proposal_id="apv2_1",
            source_signal_id="sig_from_envelope",
            freshness="current",
        )
        plan = _plan(policy_decision_id="pd_1", action_proposal_id="apv2_1")
        merged = _materialize_draft(_snapshot(), plan, envelope=envelope)
        action = merged.actions[0]
        assert action.identity_state == "complete"
        assert action.parent_policy_decision_id == "pd_1"
        assert action.parent_action_proposal_v2_id == "apv2_1"
        assert action.parent_decision_candidate_id == "dc_1"
        assert action.source_signal_id == "sig_from_envelope"


class TestEmptyDraftIsFailClosed:
    def test_a_draft_with_boilerplate_never_has_empty_hash(self) -> None:
        snapshot = _snapshot(hvac_profile={"location": {}})
        merged = _materialize_draft(snapshot, _plan())
        action = merged.actions[0]
        assert (action.payload_pl or "").strip()
        assert action.body_hash != ""


class TestPreviewHitlApprovalIdentityChain:
    """GREEN: preview body = HITL/approved body for same draft_id/revision/hash."""

    def test_approve_as_is_preserves_identity(self) -> None:
        created = _materialize_draft(_snapshot(), _plan())
        action = created.actions[0]
        store = InMemoryOperatorEngagementStore()
        pending = created.model_copy(
            update={
                "hitl_gate": HitlGate(required=True, reason="draft_ready_for_approval"),
                "operational_status": OperationalStatus(
                    code="pending_operator", steps_remaining=1, blocking=True
                ),
            }
        )
        store.insert_snapshot(pending)
        svc = AgentMcpService(settings=_settings(), store=store)

        out = svc.approve_hitl_action(
            engagement_id="eng_int04",
            action_id="draft_reply",
            operator_id="op1",
            expected_body_hash=action.body_hash,
        )
        assert out.get("ok") is True, out
        assert out["draft_id"] == action.draft_id
        assert out["revision"] == action.revision
        assert out["body_hash"] == action.body_hash
        assert out["approved_payload_pl"] == action.payload_pl
        assert out["adjudication"]["draft_id"] == action.draft_id
        assert out["adjudication"]["body_hash"] == action.body_hash

        loaded = store.load_snapshot("eng_int04")
        assert loaded is not None
        assert loaded.actions[0].draft_id == action.draft_id
        assert loaded.actions[0].revision == action.revision
        assert loaded.actions[0].body_hash == action.body_hash
        assert loaded.actions[0].payload_pl == action.payload_pl

    def test_operator_edit_bumps_revision_and_hash_keeps_draft_id(self) -> None:
        created = _materialize_draft(_snapshot(), _plan())
        action = created.actions[0]
        store = InMemoryOperatorEngagementStore()
        pending = created.model_copy(
            update={
                "hitl_gate": HitlGate(required=True, reason="draft_ready_for_approval"),
                "operational_status": OperationalStatus(
                    code="pending_operator", steps_remaining=1, blocking=True
                ),
            }
        )
        store.insert_snapshot(pending)
        svc = AgentMcpService(settings=_settings(), store=store)

        edited = "Dzień dobry,\n\nprosimy o metraż i OZC.\n\nZespół TOP-INSTAL"
        out = svc.approve_hitl_action(
            engagement_id="eng_int04",
            action_id="draft_reply",
            operator_id="op1",
            operator_draft_pl=edited,
            expected_body_hash=action.body_hash,  # hash of what operator SAW
        )
        assert out.get("ok") is True, out
        new_hash = compute_body_hash(edited)
        assert out["draft_id"] == action.draft_id
        assert out["revision"] == action.revision + 1
        assert out["body_hash"] == new_hash
        assert out["body_hash"] != action.body_hash
        assert out["approved_payload_pl"] == edited

        loaded = store.load_snapshot("eng_int04")
        assert loaded is not None
        assert loaded.actions[0].draft_id == action.draft_id
        assert loaded.actions[0].revision == action.revision + 1
        assert loaded.actions[0].body_hash == new_hash
        assert loaded.actions[0].payload_pl == edited

    def test_stale_expected_body_hash_is_rejected(self) -> None:
        created = _materialize_draft(_snapshot(), _plan())
        store = InMemoryOperatorEngagementStore()
        pending = created.model_copy(
            update={
                "hitl_gate": HitlGate(required=True, reason="draft_ready_for_approval"),
                "operational_status": OperationalStatus(
                    code="pending_operator", steps_remaining=1, blocking=True
                ),
            }
        )
        store.insert_snapshot(pending)
        svc = AgentMcpService(settings=_settings(), store=store)

        out = svc.approve_hitl_action(
            engagement_id="eng_int04",
            action_id="draft_reply",
            operator_id="op1",
            expected_body_hash="deadbeefdeadbeef",
        )
        assert out.get("ok") is False
        assert "body_hash mismatch" in str(out.get("error") or "")

    def test_stale_expected_revision_is_rejected(self) -> None:
        created = _materialize_draft(_snapshot(), _plan())
        store = InMemoryOperatorEngagementStore()
        pending = created.model_copy(
            update={
                "hitl_gate": HitlGate(required=True, reason="draft_ready_for_approval"),
                "operational_status": OperationalStatus(
                    code="pending_operator", steps_remaining=1, blocking=True
                ),
            }
        )
        store.insert_snapshot(pending)
        svc = AgentMcpService(settings=_settings(), store=store)

        out = svc.approve_hitl_action(
            engagement_id="eng_int04",
            action_id="draft_reply",
            operator_id="op1",
            expected_revision=99,
        )
        assert out.get("ok") is False
        assert "revision mismatch" in str(out.get("error") or "")

    def test_empty_operator_draft_edit_is_rejected(self) -> None:
        created = _materialize_draft(_snapshot(), _plan())
        store = InMemoryOperatorEngagementStore()
        pending = created.model_copy(
            update={
                "hitl_gate": HitlGate(required=True, reason="draft_ready_for_approval"),
                "operational_status": OperationalStatus(
                    code="pending_operator", steps_remaining=1, blocking=True
                ),
            }
        )
        store.insert_snapshot(pending)
        svc = AgentMcpService(settings=_settings(), store=store)

        out = svc.approve_hitl_action(
            engagement_id="eng_int04",
            action_id="draft_reply",
            operator_id="op1",
            operator_draft_pl="   ",
        )
        assert out.get("ok") is False
        assert "empty" in str(out.get("error") or "").lower()

    def test_snapshot_summary_exposes_identity_fields(self) -> None:
        created = _materialize_draft(_snapshot(), _plan())
        store = InMemoryOperatorEngagementStore()
        pending = created.model_copy(
            update={
                "hitl_gate": HitlGate(required=True, reason="draft_ready_for_approval"),
                "operational_status": OperationalStatus(
                    code="pending_operator", steps_remaining=1, blocking=True
                ),
            }
        )
        store.insert_snapshot(pending)
        svc = AgentMcpService(settings=_settings(), store=store)
        out = svc.approve_hitl_action(
            engagement_id="eng_int04",
            action_id="draft_reply",
            operator_id="op1",
        )
        assert out.get("ok") is True, out
        summary_action = out["snapshot"]["actions"][0]
        assert summary_action["draft_id"] == created.actions[0].draft_id
        assert summary_action["body_hash"] == created.actions[0].body_hash
        assert summary_action["revision"] == created.actions[0].revision
        assert summary_action["identity_state"] == "identity_incomplete"

    def test_legacy_action_without_identity_is_minted_on_approve(self) -> None:
        store = InMemoryOperatorEngagementStore()
        snap = EngagementSnapshotV2(
            engagement_id="eng_legacy",
            case_id="case_legacy",
            signal_id="sig_legacy",
            version=1,
            operational_status=OperationalStatus(
                code="pending_operator", steps_remaining=1, blocking=True
            ),
            hitl_gate=HitlGate(required=True, reason="draft_ready_for_approval"),
            actions=[ActionItem(id="draft_reply", enabled=True, payload_pl="stary draft")],
        )
        store.insert_snapshot(snap)
        svc = AgentMcpService(settings=_settings(), store=store)
        out = svc.approve_hitl_action(
            engagement_id="eng_legacy",
            action_id="draft_reply",
            operator_id="op1",
        )
        assert out.get("ok") is True, out
        assert out["draft_id"].startswith("draft_")
        assert out["body_hash"] == compute_body_hash("stary draft")
        assert out["revision"] == 1
