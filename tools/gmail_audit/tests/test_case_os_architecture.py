"""Case OS architecture phases P1–P2 unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_os_platform import resolve_feed_action_proposals, v2_action_proposal_to_feed_row
from skrzat_case_context import pack_lineage_from_contract, validate_operator_case_context_pack


def test_p1_validate_case_context_pack_requires_case_id() -> None:
    with pytest.raises(ValueError, match="case_id"):
        validate_operator_case_context_pack({})


def test_p1_pack_lineage_from_contract() -> None:
    contract = {
        "case_id": "case_p1",
        "pack_build": "case_context_pack.vnext.3",
        "contract_name": "CaseContextPack",
        "generated_at": "2026-06-18T12:00:00Z",
    }
    lineage = pack_lineage_from_contract(contract)
    assert lineage["case_id"] == "case_p1"
    assert lineage["pack_build"] == "case_context_pack.vnext.3"
    assert lineage["source"] == "mailbox_memory_case_context_pack"


def test_p2_prefers_v2_pipeline_proposals() -> None:
    ci = {
        "action_proposals_v2": [
            {
                "proposal_id": "prop-v2-1",
                "action_type": "prepare_reply_draft",
                "summary_pl": "Przygotuj odpowiedź",
                "status": "proposed",
                "requires_operator_approval": True,
            }
        ]
    }
    dv = {
        "decision_candidate_id": "dc-1",
        "policy_decision_id": "pd-1",
        "proposal_summary_pl": "Bo brakuje danych",
        "why_pl": "Polityka D2",
    }
    rows = resolve_feed_action_proposals(
        vnext_proposals=[{"proposal_id": "legacy-1", "title": "legacy"}],
        case_intelligence=ci,
        decision_view=dv,
    )
    assert len(rows) == 1
    assert rows[0]["schema_version"] == "action_proposal.v2"
    assert rows[0]["proposal_id"] == "prop-v2-1"
    assert rows[0]["decision_candidate_id"] == "dc-1"
    assert rows[0]["source_spine"] == "decision_pipeline_v2"


def test_p2_v2_mapper_includes_spine_fields() -> None:
    row = v2_action_proposal_to_feed_row(
        {"proposal_id": "p1", "action_type": "request_missing_info", "summary_pl": "Poproś o dane"},
        decision_view={"policy_decision_id": "pd-x"},
    )
    assert row["policy_decision_id"] == "pd-x"
    assert row["action_type"] == "request_missing_info"
