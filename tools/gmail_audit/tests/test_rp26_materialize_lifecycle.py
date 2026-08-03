"""RP-26 / RC-09: canonical materialize lifecycle (DQ-02) + idempotency hardening."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.materialize_bridge import approve_materialize_proposal
from agent_runtime.store import InMemoryOperatorEngagementStore
from agent_runtime.tools.write_executors import _with_idempotency
from llm_contracts.engagement_snapshot_v2 import (
    AgentMemory,
    EngagementSnapshotV2,
    MaterializeProposalItem,
    OperationalStatus,
)
from mailbox_memory_store import InMemoryMailboxMemoryStore


def _seed(
    store: InMemoryOperatorEngagementStore,
    *,
    proposal_id: str,
    engagement_id: str,
    proposal_type: str = "create_case",
    payload: dict[str, Any] | None = None,
) -> None:
    snapshot = EngagementSnapshotV2(
        engagement_id=engagement_id,
        case_id="",
        version=1,
        operational_status=OperationalStatus(code="pending_operator", steps_remaining=1),
        agent_memory=AgentMemory(
            materialize_proposals=[
                MaterializeProposalItem(
                    proposal_id=proposal_id,
                    proposal_type=proposal_type,  # type: ignore[arg-type]
                    status="pending",
                    payload_json=dict(payload or {
                        "customer_email": "rp26@example.com",
                        "customer_name": "RP26",
                        "subject": "RP26 lifecycle",
                    }),
                )
            ]
        ),
    )
    store.insert_snapshot(snapshot)


def _proposal(store: InMemoryOperatorEngagementStore, engagement_id: str, proposal_id: str):
    snap = store.load_snapshot(engagement_id)
    assert snap is not None
    for item in snap.agent_memory.materialize_proposals:
        if item.proposal_id == proposal_id:
            return item, snap
    raise AssertionError(f"proposal {proposal_id} missing")


def test_rp26_red_effect_before_intent_is_forbidden() -> None:
    """Side effect must not run while proposal is still plain pending without intent marker."""
    engagement_id = f"eng_rp26_{uuid.uuid4().hex[:10]}"
    proposal_id = f"prop_{uuid.uuid4().hex[:8]}"
    mailbox = InMemoryMailboxMemoryStore()
    mailbox.bootstrap()
    op_store = InMemoryOperatorEngagementStore()
    _seed(op_store, proposal_id=proposal_id, engagement_id=engagement_id)

    seen_status: list[str] = []
    seen_lifecycle: list[Any] = []

    real_execute = None
    import agent_runtime.materialize as mat_mod

    real_execute = mat_mod.execute_materialize_proposal

    def wrapped_execute(**kwargs):
        snap = kwargs["engagement_snapshot"]
        prop = kwargs["proposal"]
        # Re-load from store to see durable state at execute time
        loaded = op_store.load_snapshot(engagement_id)
        assert loaded is not None
        live, _ = _proposal(op_store, engagement_id, proposal_id)
        seen_status.append(str(live.status))
        seen_lifecycle.append((live.payload_json or {}).get("_dq02_lifecycle"))
        return real_execute(**kwargs)

    settings = MagicMock()
    settings.signal_runtime_mode = "shadow"
    settings.mailbox_memory_database_url = ""

    with patch("agent_runtime.materialize_bridge.execute_materialize_proposal", side_effect=wrapped_execute):
        result = approve_materialize_proposal(
            op_store,
            engagement_id=engagement_id,
            proposal_id=proposal_id,
            operator_id="rp26",
            mailbox_store=mailbox,
            settings=settings,
        )

    assert result.get("ok") is True, result
    assert seen_lifecycle, "execute was not called"
    # Intent must be durable before effect
    assert seen_lifecycle[0] is not None
    assert str(seen_lifecycle[0].get("phase") or "") == "intent_persisted"


def test_rp26_post_effect_save_failure_leaves_receipt_not_blind_pending() -> None:
    """If projection save fails after effect, proposal must not look like a fresh pending approve."""
    engagement_id = f"eng_rp26_{uuid.uuid4().hex[:10]}"
    proposal_id = f"prop_{uuid.uuid4().hex[:8]}"
    mailbox = InMemoryMailboxMemoryStore()
    mailbox.bootstrap()
    op_store = InMemoryOperatorEngagementStore()
    _seed(op_store, proposal_id=proposal_id, engagement_id=engagement_id)

    settings = MagicMock()
    settings.signal_runtime_mode = "shadow"
    settings.mailbox_memory_database_url = ""

    original_save = op_store.save_snapshot

    def flaky_save(snap, expected_version):
        prop = next(p for p in snap.agent_memory.materialize_proposals if p.proposal_id == proposal_id)
        if prop.status == "approved":
            raise RuntimeError("simulated_projection_failure")
        return original_save(snap, expected_version=expected_version)

    with patch.object(op_store, "save_snapshot", side_effect=flaky_save):
        result = approve_materialize_proposal(
            op_store,
            engagement_id=engagement_id,
            proposal_id=proposal_id,
            operator_id="rp26",
            mailbox_store=mailbox,
            settings=settings,
        )

    assert result.get("ok") is False, result
    prop, _ = _proposal(op_store, engagement_id, proposal_id)
    lifecycle = (prop.payload_json or {}).get("_dq02_lifecycle") or {}
    assert lifecycle.get("effect_receipt"), lifecycle
    assert str(lifecycle.get("phase") or "") == "effect_recorded"
    assert prop.status == "pending"
    assert not (prop.status == "pending" and not lifecycle.get("effect_receipt"))


def test_rp26_idempotency_fail_closed_without_db_url() -> None:
    def boom(args, **kwargs):
        return {"status": "ok", "summary": "should_not_run"}

    wrapped = _with_idempotency(boom, name="create_case")
    result = wrapped({}, idempotency_key="k1", db_url=None)
    assert result.get("status") == "error"
    assert "idempotency" in str(result.get("summary") or result.get("error") or "").lower()


def test_rp26_idempotency_records_non_ok_results() -> None:
    recorded: list[tuple] = []

    def fail_fn(args, **kwargs):
        return {"status": "error", "summary": "boom"}

    wrapped = _with_idempotency(fail_fn, name="add_case_note")

    def fake_record(db_url, key, operation, result):
        recorded.append((key, operation, dict(result)))
        return True

    with patch("agent_runtime.idempotency.check_idempotency", return_value=None), patch(
        "agent_runtime.idempotency.record_idempotency", side_effect=fake_record
    ):
        result = wrapped({}, idempotency_key="k-err", db_url="postgresql://example/db")

    assert result.get("status") == "error"
    assert recorded, "non-ok result must be recorded for restart safety"
    assert recorded[0][2].get("status") == "error"


def test_rp26_restart_with_receipt_does_not_reexecute() -> None:
    engagement_id = f"eng_rp26_{uuid.uuid4().hex[:10]}"
    proposal_id = f"prop_{uuid.uuid4().hex[:8]}"
    mailbox = InMemoryMailboxMemoryStore()
    mailbox.bootstrap()
    op_store = InMemoryOperatorEngagementStore()
    _seed(op_store, proposal_id=proposal_id, engagement_id=engagement_id)

    settings = MagicMock()
    settings.signal_runtime_mode = "shadow"
    settings.mailbox_memory_database_url = ""

    # First approve completes fully
    first = approve_materialize_proposal(
        op_store,
        engagement_id=engagement_id,
        proposal_id=proposal_id,
        operator_id="rp26",
        mailbox_store=mailbox,
        settings=settings,
    )
    assert first.get("ok") is True, first
    case_id = str(first.get("case_id") or "")
    assert case_id

    # Force proposal back to pending-looking but with durable receipt (crash after receipt)
    snap = op_store.load_snapshot(engagement_id)
    assert snap is not None
    prop = snap.agent_memory.materialize_proposals[0]
    lifecycle = dict((prop.payload_json or {}).get("_dq02_lifecycle") or {})
    lifecycle["phase"] = "effect_recorded"
    lifecycle["effect_receipt"] = dict(first.get("materialize") or {"case_id": case_id, "action": "created"})
    payload = dict(prop.payload_json or {})
    payload["_dq02_lifecycle"] = lifecycle
    updated = prop.model_copy(update={"status": "pending", "payload_json": payload})
    memory = snap.agent_memory.model_copy(update={"materialize_proposals": [updated]})
    crashed = snap.model_copy(update={"agent_memory": memory, "case_id": ""})
    op_store.save_snapshot(crashed, expected_version=snap.version)

    execute_calls = {"n": 0}

    def counting_execute(**kwargs):
        execute_calls["n"] += 1
        raise AssertionError("effect must not re-run when receipt exists")

    with patch("agent_runtime.materialize_bridge.execute_materialize_proposal", side_effect=counting_execute):
        second = approve_materialize_proposal(
            op_store,
            engagement_id=engagement_id,
            proposal_id=proposal_id,
            operator_id="rp26-restart",
            mailbox_store=mailbox,
            settings=settings,
        )

    assert second.get("ok") is True, second
    assert execute_calls["n"] == 0
    prop2, snap2 = _proposal(op_store, engagement_id, proposal_id)
    assert prop2.status == "approved"
    assert str(snap2.case_id or "") == case_id
