"""P0.5 deterministic data-vs-authority suite.

Owns: source provenance / evidence authority / instruction authority
classification, execution argument binding, approval claims, adversarial
classes A-J and metamorphic control-plane mutation checks.

Central principle under test:

    external content may influence BUSINESS SEMANTICS through the canonical
    reasoning pipeline, but may NOT directly establish or override CONTROL /
    AUTHORITY / EXECUTION state.

Deterministic gate: no hypothesis, no guarded skip, no LLM call.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution import AgentConstitution
from agent_runtime.graph import AgentGraphEngine
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from evidence_authority import (
    attach_evidence_provenance,
    classify_source_origin,
    evidence_authority_for_origin,
    instruction_authority_for_origin,
    provenance_classification,
)


class _Planner:
    def __init__(self, plan: ToolCallPlan) -> None:
        self.plan = plan

    def plan_next_tool(self, *, snapshot, available_tools, constitution):
        return self.plan


class _Registry:
    def __init__(self, result: ToolResult | None = None) -> None:
        self.calls = 0
        self.result = result or ToolResult(
            status="ok",
            turn_summary_pl="Executed fixture.",
            snapshot_delta={
                "operational_status": {"code": "pending_operator", "blocking": True},
                "hitl_gate": {"required": True, "reason": "fixture_executed"},
            },
        )

    def execute(self, plan, *, context):
        self.calls += 1
        return self.result


def _constitution(*tools: str) -> AgentConstitution:
    return AgentConstitution(
        hvac_rules="",
        company_context="",
        forbidden_actions=(),
        tool_allowlist=tuple(tools),
        tool_budget={},
    )


def _run(plan: ToolCallPlan, *, signal_payload: dict) -> tuple[_Registry, object]:
    constitution = _constitution(
        "propose_mutation",
        "generate_draft_reply",
        "search_rag_knowledge",
        "report_gaps_and_stop",
    )
    snapshot = build_initial_snapshot(
        case_id="case_boundary",
        engagement_id="eng_boundary",
        signal_id="sig_boundary",
        trace_id="trace_boundary",
    )
    registry = _Registry()
    result = AgentGraphEngine(
        planner=_Planner(plan),
        constitution=constitution,
        tool_registry=registry,
    ).run(
        snapshot,
        context=ToolExecutionContext.from_snapshot(
            snapshot,
            signal_payload={"harness_mode": True, **signal_payload},
            constitution=constitution,
        ),
    )
    return registry, result


# --------------------------------------------------------------------------
# source provenance / evidence authority / instruction authority
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected_origin"),
    [
        ({"source_kind": "gmail"}, "CUSTOMER_EMAIL"),
        ({"signal_kind": "gmail_inbound"}, "CUSTOMER_EMAIL"),
        ({"signal_kind": "gmail_attachment_observed"}, "ATTACHMENT"),
        ({"attachment_id": "att_1"}, "ATTACHMENT"),
        ({"content_part": "quoted"}, "QUOTED_CONTENT"),
        ({"forwarded_content": True}, "FORWARDED_CONTENT"),
        ({"signal_kind": "rag"}, "RAG"),
        ({"produced_by": "rag_retriever"}, "RAG"),
        ({"signal_kind": "tool_result"}, "TOOL_RESULT"),
        ({"source_kind": "operator"}, "OPERATOR"),
        ({"source_kind": "system"}, "SYSTEM"),
        ({"source_kind": "drive"}, "ATTACHMENT"),
        ({}, "UNKNOWN"),
    ],
)
def test_source_origin_classification(payload: dict, expected_origin: str) -> None:
    assert classify_source_origin(payload) == expected_origin


@pytest.mark.parametrize(
    ("origin", "expected_evidence", "expected_instruction"),
    [
        ("CUSTOMER_EMAIL", "CUSTOMER_STATEMENT", "NONE"),
        ("QUOTED_CONTENT", "CUSTOMER_STATEMENT", "NONE"),
        ("FORWARDED_CONTENT", "CUSTOMER_STATEMENT", "NONE"),
        ("ATTACHMENT", "CUSTOMER_DOCUMENT", "NONE"),
        ("RAG", "AUTHORITATIVE_DOCUMENT", "NONE"),
        ("TOOL_RESULT", "DERIVED_LLM_CLAIM", "NONE"),
        ("DERIVED", "DERIVED_LLM_CLAIM", "NONE"),
        ("OPERATOR", "OPERATOR_STATEMENT", "OPERATOR"),
        ("SYSTEM", "INTERNAL_SOT", "SYSTEM"),
        ("INTERNAL_STATE", "INTERNAL_SOT", "SYSTEM"),
    ],
)
def test_authority_dimensions(
    origin: str,
    expected_evidence: str,
    expected_instruction: str,
) -> None:
    assert evidence_authority_for_origin(origin) == expected_evidence
    assert instruction_authority_for_origin(origin) == expected_instruction


def test_provenance_classification_full_bundle() -> None:
    payload = {
        "source_kind": "gmail",
        "source_message": {"from": "klient@example.test"},
        "body_text": "Numer błędu H70.",
    }
    bundle = provenance_classification(payload)
    assert bundle == {
        "source_origin": "CUSTOMER_EMAIL",
        "evidence_authority": "CUSTOMER_STATEMENT",
        "instruction_authority": "NONE",
        "produced_by": "",
    }


def test_tool_identity_is_not_output_authority() -> None:
    # A trusted execution mechanism (RAG retriever) does not make its output an
    # instruction source. The origin of the retrieved fragment survives.
    rag_fragment = provenance_classification(
        {"produced_by": "rag_retriever", "attachment_id": "att_customer_doc"},
    )
    assert rag_fragment["source_origin"] == "ATTACHMENT"
    assert rag_fragment["evidence_authority"] == "CUSTOMER_DOCUMENT"
    assert rag_fragment["instruction_authority"] == "NONE"

    web_result = provenance_classification(
        {"produced_by": "web_search", "evidence_part": "derived"},
    )
    assert web_result["source_origin"] == "TOOL_RESULT"
    assert web_result["instruction_authority"] == "NONE"


def test_attach_evidence_provenance_does_not_overwrite_explicit_trusted_origin() -> None:
    record = {"source_origin": "OPERATOR", "content": "operator command"}
    attached = attach_evidence_provenance(
        record,
        payload={"source_kind": "gmail"},
    )
    assert attached["source_origin"] == "OPERATOR"
    assert attached["instruction_authority"] == "OPERATOR"


def test_negative_control_external_business_information_still_usable() -> None:
    # Class J: useful external information (error code H70) is DATA, not an
    # instruction, and read-only tool use with that content is not blocked.
    registry, result = _run(
        ToolCallPlan(
            tool_name="search_rag_knowledge",
            arguments={"query": "pompa ciepła H70"},
        ),
        signal_payload={
            "source_kind": "gmail",
            "customer_email": "klient@example.test",
            "body_text": "Pompa wyświetla numer błędu H70.",
        },
    )
    assert registry.calls == 1
    assert result.turns[0].tool_status == "ok"
