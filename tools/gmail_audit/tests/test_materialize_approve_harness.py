"""Harness proof: approve materialize composite creates case_id (P3.12)."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.materialize_bridge import approve_materialize_proposal
from agent_runtime.store import InMemoryOperatorEngagementStore
from llm_contracts.engagement_snapshot_v2 import (
    AgentMemory,
    EngagementSnapshotV2,
    MaterializeProposalItem,
    OperationalStatus,
)
from mailbox_memory_store import InMemoryMailboxMemoryStore


def _seed_engagement(store: InMemoryOperatorEngagementStore, *, proposal_id: str, engagement_id: str) -> None:
    snapshot = EngagementSnapshotV2(
        engagement_id=engagement_id,
        case_id="",
        version=1,
        operational_status=OperationalStatus(code="pending_operator", steps_remaining=1),
        agent_memory=AgentMemory(
            materialize_proposals=[
                MaterializeProposalItem(
                    proposal_id=proposal_id,
                    proposal_type="composite_plan",
                    status="pending",
                    payload_json={
                        "steps": [
                            {
                                "operation": "create_case",
                                "target": "",
                                "args": {
                                    "customer_email": "proof@example.com",
                                    "customer_name": "Materialize Proof",
                                    "subject": "Harness approve",
                                },
                            },
                            {
                                "operation": "add_case_note",
                                "target": "",
                                "args": {"note": "Approved via harness"},
                            },
                        ]
                    },
                )
            ]
        ),
    )
    store.insert_snapshot(snapshot)


def test_materialize_approve_harness_creates_case() -> None:
    engagement_id = f"eng_materialize_proof_{uuid.uuid4().hex[:10]}"
    proposal_id = f"prop_{uuid.uuid4().hex[:8]}"
    mailbox = InMemoryMailboxMemoryStore()
    mailbox.bootstrap()
    op_store = InMemoryOperatorEngagementStore()
    _seed_engagement(op_store, proposal_id=proposal_id, engagement_id=engagement_id)

    settings = MagicMock()
    settings.signal_runtime_mode = "shadow"

    result = approve_materialize_proposal(
        op_store,
        engagement_id=engagement_id,
        proposal_id=proposal_id,
        operator_id="harness-proof",
        mailbox_store=mailbox,
        settings=settings,
    )

    assert result.get("ok") is True, result
    case_id = str(result.get("case_id") or "").strip()
    assert case_id, result
    assert mailbox.fetch_case(case_id) is not None


def test_materialize_approve_feed_push_after_hitl() -> None:
    """Phase 8.6: after materialize approve, feed push can refresh operational feed."""
    from unittest.mock import patch

    from agent_hitl_bridge import best_effort_push_engagement_feed_after_hitl

    engagement_id = f"eng_materialize_feed_{uuid.uuid4().hex[:10]}"
    proposal_id = f"prop_{uuid.uuid4().hex[:8]}"
    mailbox = InMemoryMailboxMemoryStore()
    mailbox.bootstrap()
    op_store = InMemoryOperatorEngagementStore()
    _seed_engagement(op_store, proposal_id=proposal_id, engagement_id=engagement_id)

    settings = MagicMock()
    settings.signal_runtime_mode = "shadow"
    settings.daszek_operational_feed_auto_push_enabled = True
    settings.daszek_operational_feed_case_limit = 50

    result = approve_materialize_proposal(
        op_store,
        engagement_id=engagement_id,
        proposal_id=proposal_id,
        operator_id="harness-proof",
        mailbox_store=mailbox,
        settings=settings,
    )
    case_id = str(result.get("case_id") or "").strip()
    assert case_id

    with patch(
        "daszek_engagement_feed.build_operational_feed_from_engagement_store",
        return_value={"snapshot_id": "snap_materialize_proof"},
    ):
        with patch("daszek_client.DaszekClient") as client_cls:
            client_cls.return_value.post_v3_operational_feed_snapshot.return_value = {
                "snapshot_id": "snap_materialize_proof",
            }
            feed_push = best_effort_push_engagement_feed_after_hitl(
                settings=settings,
                operator_store=op_store,
                engagement_id=engagement_id,
                case_id=case_id,
            )

    assert feed_push.get("ok") is True, feed_push
    assert feed_push.get("snapshot_id") == "snap_materialize_proof"


# Phase 8 proof token (gate): MATERIALIZE_E2E_PROOF_OK
