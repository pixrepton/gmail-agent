"""P1.1P: durable decision revision lineage -- restart recovery proof.

The DecisionRevisionLedger is a projection/cache over the durable
MailboxMemoryStore seam. These tests prove that the canonical lineage,
supersession, current revision, request idempotency and stale-artifact guards
survive a process restart (in-memory ledger destroyed, projection rebuilt from
durable state).

Core invariants under test:

    - exactly one CURRENT revision per decision lineage after rebuild
    - revision ordering by integer, never by timestamp
    - duplicate request after restart creates no new revision
    - stale request after restart stays stale
    - old approval / old ToolPlan stay DENY after restart
    - zero or multiple CURRENT durable revisions fail closed
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution import AgentConstitution
from agent_runtime.graph import AgentGraphEngine
from agent_runtime.policy_action_spine import (
    evaluate_semantic_policy_plan_consistency,
)
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan
from canonical_action_decision import (
    DecisionRevisionError,
    DecisionRevisionLedger,
    approval_binds_revision,
    artifact_version_matches,
    build_business_decision_proposal,
    canonicalize,
    evaluate_decision_revision,
    request_decision_revision,
    stale_artifact_reason,
)
from execution_runtime import normalize_action_proposal
from llm_contracts.engagement_snapshot_v2 import PolicyActionEnvelopeV1
from mailbox_memory import InMemoryMailboxMemoryStore


def _br(*, missing: list[str] | None = None) -> dict[str, object]:
    return {
        "recommended_next_action": "collect_data",
        "missing_information": missing or ["error_code", "exact_symptoms"],
        "recommended_action_reason": "Brak danych diagnostycznych.",
        "urgency": "normal",
        "confidence": {"action_confidence": 0.8, "business_confidence": 0.7},
    }


def _situation(missing: list[str] | None = None) -> dict[str, object]:
    return {
        "missing_information": missing or ["error_code", "exact_symptoms"],
        "missing_critical_fields": missing or ["error_code", "exact_symptoms"],
    }


def _register_r1(
    *,
    ledger: DecisionRevisionLedger,
    case_id: str = "case_durable",
    situation_version: str = "sv_1",
) -> dict[str, object]:
    proposal = build_business_decision_proposal(_br())
    assert proposal is not None
    cad = canonicalize(
        proposal=proposal,
        situation_understanding=_situation(),
        case_id=case_id,
        situation_version=situation_version,
    )
    assert cad["semantic_status"] == "FROZEN"
    ledger.register_cad(cad)
    return cad


def _accept_to_r2(
    *,
    ledger: DecisionRevisionLedger,
    cad_r1: dict[str, object],
    missing: list[str] | None = None,
    reason_code: str = "CANONICAL_FACT_CHANGED",
) -> dict[str, object]:
    emitted = request_decision_revision(
        decision_id=cad_r1["decision_id"],
        current_revision=cad_r1["revision"],
        reason_code=reason_code,
        source_layer="case_intelligence",
        ledger=ledger,
    )
    assert emitted["status"] == "PENDING"
    return evaluate_decision_revision(
        request=emitted["request"],
        current_cad=cad_r1,
        business_reasoning_result=_br(missing=missing or ["exact_symptoms"]),
        situation_understanding=_situation(missing or ["exact_symptoms"]),
        ledger=ledger,
    )


def _envelope(*, decision_id: str, version_id: str) -> PolicyActionEnvelopeV1:
    return PolicyActionEnvelopeV1(
        canonical_decision_id=decision_id,
        decision_version_id=version_id,
        source_semantic_hash="sh_x",
        policy_decision_id="pdec_1",
        action_proposal_id="apv2_1",
        action_intent="ask_for_missing_data",
        action_target="customer",
        action_channel="mail",
        allowed_action_tools=["generate_draft_reply"],
        forbidden_tools=["request_operator_clarification"],
        freshness="current",
    )


# --------------------------------------------------------------------------
# restart recovery
# --------------------------------------------------------------------------


def test_revision_and_supersession_survive_restart() -> None:
    store = InMemoryMailboxMemoryStore()
    ledger = DecisionRevisionLedger(store=store)
    cad_r1 = _register_r1(ledger=ledger)
    result = _accept_to_r2(ledger=ledger, cad_r1=cad_r1)
    assert result["outcome"] == "ACCEPTED"

    # Destroy the in-memory ledger entirely (process restart).
    ledger = None  # type: ignore[assignment]

    ledger2 = DecisionRevisionLedger.from_store(store)
    decision_id = cad_r1["decision_id"]
    assert ledger2.current_revision(decision_id) == 2
    current = ledger2.current_cad(decision_id)
    assert current is not None
    assert current["decision_version_id"] == f"{decision_id}:r2"
    assert current["revision_status"] == "CURRENT"
    assert current["semantic_status"] == "FROZEN"

    revisions = ledger2.revisions(decision_id)
    assert [r["revision_status"] for r in revisions] == ["SUPERSEDED", "CURRENT"]
    assert revisions[0]["decision_version_id"] == f"{decision_id}:r1"
    assert revisions[0]["superseded_by_version_id"] == f"{decision_id}:r2"
    assert revisions[1]["supersedes_version_id"] == f"{decision_id}:r1"
    assert revisions[1]["semantic_hash"] == current["semantic_hash"]


def test_duplicate_request_after_restart_creates_no_revision() -> None:
    store = InMemoryMailboxMemoryStore()
    ledger = DecisionRevisionLedger(store=store)
    cad_r1 = _register_r1(ledger=ledger)
    result = _accept_to_r2(ledger=ledger, cad_r1=cad_r1)
    accepted_request = result["request"]

    ledger2 = DecisionRevisionLedger.from_store(store)
    replay = ledger2.record_request(dict(accepted_request))
    assert replay["status"] == "DUPLICATE_REVISION_REQUEST"
    assert ledger2.current_revision(cad_r1["decision_id"]) == 2
    assert len(ledger2.revisions(cad_r1["decision_id"])) == 2


def test_stale_request_after_restart_remains_stale() -> None:
    store = InMemoryMailboxMemoryStore()
    ledger = DecisionRevisionLedger(store=store)
    cad_r1 = _register_r1(ledger=ledger)
    _accept_to_r2(ledger=ledger, cad_r1=cad_r1)

    ledger2 = DecisionRevisionLedger.from_store(store)
    stale = request_decision_revision(
        decision_id=cad_r1["decision_id"],
        current_revision=1,  # durable current is r2
        reason_code="NEW_CONFLICTING_EVIDENCE",
        ledger=ledger2,
    )
    assert stale["status"] == "STALE_REVISION_REQUEST"
    assert ledger2.current_revision(cad_r1["decision_id"]) == 2


def test_rejected_request_survives_restart() -> None:
    store = InMemoryMailboxMemoryStore()
    ledger = DecisionRevisionLedger(store=store)
    cad_r1 = _register_r1(ledger=ledger)
    emitted = request_decision_revision(
        decision_id=cad_r1["decision_id"],
        current_revision=1,
        reason_code="NEW_CONFLICTING_EVIDENCE",
        ledger=ledger,
    )
    outcome = evaluate_decision_revision(
        request=emitted["request"],
        current_cad=cad_r1,
        business_reasoning_result=_br(missing=["device_model"]),
        situation_understanding=_situation(["error_code", "exact_symptoms"]),
        ledger=ledger,
    )
    assert outcome["outcome"] == "REJECTED"

    ledger2 = DecisionRevisionLedger.from_store(store)
    stored = ledger2.request_status(emitted["request"]["request_id"])
    assert stored is not None
    assert stored["status"] == "REJECTED"
    assert ledger2.current_revision(cad_r1["decision_id"]) == 1


def test_audit_trail_reconstructed_after_restart() -> None:
    store = InMemoryMailboxMemoryStore()
    ledger = DecisionRevisionLedger(store=store)
    cad_r1 = _register_r1(ledger=ledger)
    _accept_to_r2(ledger=ledger, cad_r1=cad_r1)

    ledger2 = DecisionRevisionLedger.from_store(store)
    audit = ledger2.audit_trail(cad_r1["decision_id"])
    assert any(row["outcome"] == "ACCEPTED" for row in audit)
    accepted = [row for row in audit if row["outcome"] == "ACCEPTED"][0]
    assert accepted["old_version_id"] == f"{cad_r1['decision_id']}:r1"
    assert accepted["new_version_id"] == f"{cad_r1['decision_id']}:r2"


# --------------------------------------------------------------------------
# stale guards after restart
# --------------------------------------------------------------------------


def test_old_approval_and_tool_plan_denied_after_restart() -> None:
    store = InMemoryMailboxMemoryStore()
    ledger = DecisionRevisionLedger(store=store)
    cad_r1 = _register_r1(ledger=ledger)
    result = _accept_to_r2(ledger=ledger, cad_r1=cad_r1)
    decision_id = cad_r1["decision_id"]

    ledger2 = DecisionRevisionLedger.from_store(store)
    current = ledger2.current_cad(decision_id)
    assert current is not None

    old_approval = {"approval_id": "appr_1", "decision_version_id": f"{decision_id}:r1"}
    assert approval_binds_revision(old_approval, current) is False

    old_plan = {
        "tool_name": "generate_draft_reply",
        "decision_version_id": f"{decision_id}:r1",
    }
    assert artifact_version_matches(old_plan, current) is False
    assert stale_artifact_reason(old_plan, current) == "STALE_DECISION_REVISION"

    # Reference-monitor level: envelope bound to r2 denies the r1 plan.
    envelope_r2 = _envelope(decision_id=decision_id, version_id=f"{decision_id}:r2")
    stale_tool_plan = ToolCallPlan(
        tool_name="generate_draft_reply",
        arguments={"intent": "missing_info"},
        policy_decision_id="pdec_1",
        action_proposal_id="apv2_1",
        decision_version_id=f"{decision_id}:r1",
    )
    consistency = evaluate_semantic_policy_plan_consistency(envelope_r2, stale_tool_plan)
    assert consistency.status == "conflicting"
    assert consistency.reason_codes == ["STALE_DECISION_REVISION"]

    # New artifacts built from the rebuilt current revision stay consistent.
    current_plan = ToolCallPlan(
        tool_name="generate_draft_reply",
        arguments={"intent": "missing_info"},
        policy_decision_id="pdec_1",
        action_proposal_id="apv2_1",
        decision_version_id=f"{decision_id}:r2",
    )
    consistency_ok = evaluate_semantic_policy_plan_consistency(envelope_r2, current_plan)
    assert consistency_ok.status == "consistent"


# --------------------------------------------------------------------------
# fail-closed rebuild invariants
# --------------------------------------------------------------------------


def test_multiple_current_revisions_fail_closed_on_rebuild() -> None:
    store = InMemoryMailboxMemoryStore()
    store.append_decision_revision(
        {
            "decision_id": "dec_dup",
            "revision": 1,
            "decision_version_id": "dec_dup:r1",
            "semantic_hash": "sh_a",
            "revision_status": "CURRENT",
        }
    )
    store.append_decision_revision(
        {
            "decision_id": "dec_dup",
            "revision": 2,
            "decision_version_id": "dec_dup:r2",
            "semantic_hash": "sh_b",
            "revision_status": "CURRENT",
        }
    )
    with pytest.raises(DecisionRevisionError) as exc:
        DecisionRevisionLedger.from_store(store)
    assert exc.value.code == "rebuild_one_current_violation"


def test_zero_current_revisions_fail_closed_on_rebuild() -> None:
    store = InMemoryMailboxMemoryStore()
    store.append_decision_revision(
        {
            "decision_id": "dec_zero",
            "revision": 1,
            "decision_version_id": "dec_zero:r1",
            "semantic_hash": "sh_a",
            "revision_status": "SUPERSEDED",
        }
    )
    with pytest.raises(DecisionRevisionError) as exc:
        DecisionRevisionLedger.from_store(store)
    assert exc.value.code == "rebuild_one_current_violation"


def test_timestamp_permutation_does_not_determine_current_revision() -> None:
    store = InMemoryMailboxMemoryStore()
    ledger = DecisionRevisionLedger(store=store)
    cad_r1 = _register_r1(ledger=ledger)
    result = _accept_to_r2(ledger=ledger, cad_r1=cad_r1)
    decision_id = cad_r1["decision_id"]

    # Scramble timestamps: r1 gets NEWER timestamps than r2. Canonical
    # ordering must stay driven by the revision integer.
    r1_row = store.decision_revisions[f"{decision_id}:r1"]
    r2_row = store.decision_revisions[f"{decision_id}:r2"]
    r1_row["created_at"] = "2099-01-01T00:00:00Z"
    r2_row["created_at"] = "1999-01-01T00:00:00Z"
    for req in store.decision_revision_requests.values():
        if req.get("status") == "ACCEPTED":
            req["requested_at"] = "1999-01-01T00:00:00Z"

    ledger2 = DecisionRevisionLedger.from_store(store)
    current = ledger2.current_cad(decision_id)
    assert current is not None
    assert current["decision_version_id"] == f"{decision_id}:r2"
    assert current["revision"] == 2
    assert [r["revision"] for r in ledger2.revisions(decision_id)] == [1, 2]


# --------------------------------------------------------------------------
# bounded production-faithful restart trajectory
# --------------------------------------------------------------------------


def test_restart_trajectory_full_flow() -> None:
    store = InMemoryMailboxMemoryStore()
    ledger = DecisionRevisionLedger(store=store)
    cad_r1 = _register_r1(ledger=ledger, case_id="case_flow")
    result = _accept_to_r2(ledger=ledger, cad_r1=cad_r1)
    assert result["outcome"] == "ACCEPTED"
    decision_id = cad_r1["decision_id"]

    # Process restart: only durable state remains.
    ledger = None  # type: ignore[assignment]
    ledger2 = DecisionRevisionLedger.from_store(store)
    current = ledger2.current_cad(decision_id)
    assert current is not None

    # Old execution artifacts from r1 are DENIED.
    old_plan = {
        "tool_name": "generate_draft_reply",
        "decision_version_id": f"{decision_id}:r1",
    }
    assert stale_artifact_reason(old_plan, current) == "STALE_DECISION_REVISION"
    old_approval = {"approval_id": "appr_1", "decision_version_id": f"{decision_id}:r1"}
    assert approval_binds_revision(old_approval, current) is False

    # Duplicate replay of the accepted request: no r3.
    replay = ledger2.record_request(dict(result["request"]))
    assert replay["status"] == "DUPLICATE_REVISION_REQUEST"
    assert ledger2.current_revision(decision_id) == 2
    assert len(ledger2.revisions(decision_id)) == 2

    # New downstream artifacts built from r2 stay consistent and reach HITL
    # with prepare_only semantics (no live send).
    envelope = _envelope(decision_id=decision_id, version_id=f"{decision_id}:r2")
    plan = ToolCallPlan(
        tool_name="generate_draft_reply",
        arguments={"intent": "missing_info"},
        policy_decision_id="pdec_1",
        action_proposal_id="apv2_1",
        decision_version_id=f"{decision_id}:r2",
    )
    consistency = evaluate_semantic_policy_plan_consistency(envelope, plan)
    assert consistency.status == "consistent"
    assert "generate_draft_reply" in envelope.allowed_action_tools
    assert "request_operator_clarification" in envelope.forbidden_tools

    proposal = normalize_action_proposal(
        {
            "proposal_id": "apv2_flow",
            "case_id": "case_flow",
            "action_type": "ask_for_missing_data",
            "decision_version_id": f"{decision_id}:r2",
            "payload": {"action_target": "customer", "action_channel": "mail"},
        }
    )
    assert proposal.decision_version_id == f"{decision_id}:r2"
    assert proposal.action_type == "ask_for_missing_data"

    # Reference-monitor execution guard: stale plan is denied before the tool
    # registry is reached (no execution, no fallback tool).
    class _Planner:
        def __init__(self, tool_plan: ToolCallPlan) -> None:
            self.tool_plan = tool_plan

        def plan_next_tool(self, **_: object) -> ToolCallPlan:
            return self.tool_plan

    class _Registry:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, plan: ToolCallPlan, *, context: ToolExecutionContext):
            self.calls += 1
            from agent_runtime.tool_result import ToolResult

            return ToolResult(status="ok", turn_summary_pl="executed")

    constitution = AgentConstitution(
        hvac_rules="",
        company_context="",
        forbidden_actions=(),
        tool_allowlist=("generate_draft_reply",),
        tool_budget={},
    )
    snapshot = build_initial_snapshot(
        case_id="case_flow",
        engagement_id="eng_flow",
        trace_id="trace_flow",
    )
    snapshot = snapshot.model_copy(update={"policy_action_envelope": envelope})
    registry = _Registry()
    stale_tool_plan = ToolCallPlan(
        tool_name="generate_draft_reply",
        arguments={"intent": "missing_info"},
        decision_version_id=f"{decision_id}:r1",
    )
    result = AgentGraphEngine(
        planner=_Planner(stale_tool_plan),
        constitution=constitution,
        tool_registry=registry,
    ).run(
        snapshot,
        context=ToolExecutionContext.from_snapshot(
            snapshot,
            signal_payload={
                "harness_mode": True,
                "source_kind": "gmail",
                "policy_action_envelope": envelope.model_dump(mode="python"),
            },
            constitution=constitution,
        ),
    )
    assert registry.calls == 0
    assert result.snapshot.hitl_gate.required is True
    consistency_guard = result.snapshot.semantic_policy_plan_consistency
    assert consistency_guard is not None
    assert "STALE_DECISION_REVISION" in consistency_guard.reason_codes
