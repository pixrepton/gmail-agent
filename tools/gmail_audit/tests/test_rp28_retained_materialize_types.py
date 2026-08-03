"""RP-28 / RC-14: retain only composite_plan materialize proposal types."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.materialize import (
    RETAINED_MATERIALIZE_PROPOSAL_TYPES,
    UNRETAINED_MATERIALIZE_PROPOSAL_TYPES,
    append_materialize_proposal,
    execute_materialize_proposal,
    is_retained_materialize_proposal_type,
)
from llm_contracts.engagement_snapshot_v2 import (
    EngagementSnapshotV2,
    MaterializeProposalItem,
    OperationalStatus,
)


def test_rp28_retained_set_is_composite_only() -> None:
    assert RETAINED_MATERIALIZE_PROPOSAL_TYPES == frozenset({"composite_plan"})
    assert "create_case" in UNRETAINED_MATERIALIZE_PROPOSAL_TYPES
    assert "link_existing" in UNRETAINED_MATERIALIZE_PROPOSAL_TYPES
    assert "create_artifact" in UNRETAINED_MATERIALIZE_PROPOSAL_TYPES
    assert "defer_operator" in UNRETAINED_MATERIALIZE_PROPOSAL_TYPES
    assert is_retained_materialize_proposal_type("composite_plan")
    assert not is_retained_materialize_proposal_type("create_case")


@pytest.mark.parametrize("ptype", sorted(UNRETAINED_MATERIALIZE_PROPOSAL_TYPES))
def test_rp28_append_rejects_unretained_types(ptype: str) -> None:
    snap = EngagementSnapshotV2(
        engagement_id="eng_rp28",
        case_id="",
        version=1,
        operational_status=OperationalStatus(code="pending_operator", steps_remaining=1),
    )
    with pytest.raises(ValueError, match="not retained"):
        append_materialize_proposal(snap, proposal_type=ptype, payload={"x": 1})


@pytest.mark.parametrize("ptype", sorted(UNRETAINED_MATERIALIZE_PROPOSAL_TYPES))
def test_rp28_execute_rejects_unretained_types(ptype: str) -> None:
    snap = EngagementSnapshotV2(
        engagement_id="eng_rp28e",
        case_id="",
        version=1,
        operational_status=OperationalStatus(code="pending_operator", steps_remaining=1),
    )
    proposal = MaterializeProposalItem(
        proposal_id="prop_dead",
        proposal_type=ptype,  # type: ignore[arg-type]
        status="pending",
        payload_json={"case_id": "case_x"},
    )
    result = execute_materialize_proposal(
        mailbox_store=None,
        proposal=proposal,
        engagement_snapshot=snap,
    )
    assert result.get("status") == "error"
    assert "not retained" in str(result.get("error") or "")


def test_rp28_append_allows_composite_plan() -> None:
    snap = EngagementSnapshotV2(
        engagement_id="eng_rp28ok",
        case_id="",
        version=1,
        operational_status=OperationalStatus(code="pending_operator", steps_remaining=1),
    )
    updated = append_materialize_proposal(
        snap,
        proposal_type="composite_plan",
        payload={"steps": [{"operation": "add_case_note", "args": {"note": "x"}}]},
    )
    assert len(updated.agent_memory.materialize_proposals) == 1
    assert updated.agent_memory.materialize_proposals[0].proposal_type == "composite_plan"
