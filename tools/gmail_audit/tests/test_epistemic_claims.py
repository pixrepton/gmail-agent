"""P1.3: epistemic claim contract + deterministic projection.

Proves: CONFIRMED requires evidence + no conflict; INFERRED requires a value
and derivation basis; UNKNOWN has no assertable value/basis; CONFLICTED is
never CONFIRMED; epistemic status is independent of the P0.5 provenance trio.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.epistemic_projection import (
    build_draft_claim_context,
    project_epistemic_claims,
)
from llm_contracts.epistemic_claims import (
    CONFIRMED,
    CONFLICTED,
    INFERRED,
    UNKNOWN,
    DraftClaimContext,
    EpistemicClaim,
)


def _fact(
    *,
    fact_key: str,
    value: str,
    source_type: str = "gmail_message",
    source_ref: str = "msg_1",
    status: str = "active",
    origin: str = "CUSTOMER_EMAIL",
    authority: str = "CUSTOMER_STATEMENT",
    instruction: str = "NONE",
    inference_basis: list[str] | None = None,
    confidence: float = 0.9,
) -> dict:
    metadata: dict = {
        "source_origin": origin,
        "evidence_authority": authority,
        "instruction_authority": instruction,
    }
    if inference_basis is not None:
        metadata["inference_basis"] = inference_basis
    return {
        "fact_id": f"fact_{fact_key}",
        "fact_key": fact_key,
        "normalized_value": value,
        "source_type": source_type,
        "source_ref": source_ref,
        "message_id": source_ref,
        "status": status,
        "confidence": confidence,
        "metadata": metadata,
    }


def test_confirmed_customer_report_requires_evidence_ref() -> None:
    claims = project_epistemic_claims(
        [
            _fact(fact_key="error_code", value="H70"),
            _fact(fact_key="error_code", value="H70", source_ref=""),
        ]
    )
    by_key = [c for c in claims if c.proposition_key == "error_code"]
    assert by_key[0].status == CONFIRMED
    assert by_key[0].evidence_refs
    assert "customer_reported" in by_key[0].reason_codes
    # Same value without an evidence ref cannot be CONFIRMED (fail safe).
    assert by_key[1].status == UNKNOWN
    assert "confirmed_without_evidence" in by_key[1].reason_codes


def test_inferred_requires_derivation_basis() -> None:
    claims = project_epistemic_claims(
        [
            _fact(
                fact_key="device_fault_cause",
                value="pompa obiegowa",
                source_type="inference",
                status="inferred",
                origin="DERIVED",
                authority="DERIVED_LLM_CLAIM",
                inference_basis=["error_code:H70", "symptom:no_heating"],
            ),
            _fact(
                fact_key="device_fault_cause_2",
                value="czujnik",
                source_type="inference",
                status="inferred",
                source_ref="",
                origin="DERIVED",
                authority="DERIVED_LLM_CLAIM",
            ),
        ]
    )
    by_key = {c.proposition_key: c for c in claims}
    assert by_key["device_fault_cause"].status == INFERRED
    assert by_key["device_fault_cause"].inference_basis == [
        "error_code:H70",
        "symptom:no_heating",
    ]
    # Derived without any basis -> UNKNOWN (never CONFIRMED, never guessed).
    assert by_key["device_fault_cause_2"].status == UNKNOWN
    assert "derived_without_basis" in by_key["device_fault_cause_2"].reason_codes


def test_conflicted_fact_is_never_confirmed() -> None:
    fact = _fact(fact_key="heated_area_m2", value="120", origin="INTERNAL_STATE", authority="INTERNAL_SOT", source_type="agent_write")
    claims = project_epistemic_claims(
        [fact],
        conflicting_facts=[{"fact_key": "heated_area_m2", "values": ["120", "160"]}],
    )
    claim = claims[0]
    assert claim.status == CONFLICTED
    assert claim.conflicted is True
    assert claim.decision_usable is False


def test_llm_conclusion_from_authoritative_rag_does_not_inherit_confirmed() -> None:
    # RAG fragment itself (authoritative document) -> CONFIRMED with retrieval ref.
    rag_fact = _fact(
        fact_key="device_model",
        value="WH-XYZ",
        source_type="rag_evidence",
        source_ref="chunk_9",
        origin="RAG",
        authority="AUTHORITATIVE_DOCUMENT",
    )
    # LLM conclusion derived from that retrieval -> INFERRED, not CONFIRMED.
    derived_fact = _fact(
        fact_key="customer_owns_device",
        value="WH-XYZ",
        source_type="inference",
        status="inferred",
        origin="DERIVED",
        authority="DERIVED_LLM_CLAIM",
        inference_basis=["document:chunk_9"],
    )
    claims = project_epistemic_claims([rag_fact, derived_fact])
    by_key = {c.proposition_key: c for c in claims}
    assert by_key["device_model"].status == CONFIRMED
    assert by_key["customer_owns_device"].status == INFERRED


def test_epistemic_status_is_independent_of_instruction_authority() -> None:
    claims = project_epistemic_claims([_fact(fact_key="error_code", value="H70")])
    claim = claims[0]
    assert claim.status == CONFIRMED
    assert claim.source_origin == "CUSTOMER_EMAIL"
    assert claim.evidence_authority == "CUSTOMER_STATEMENT"
    assert claim.instruction_authority == "NONE"


def test_unknown_fields_flow_into_draft_context_without_fake_value() -> None:
    claims = project_epistemic_claims([_fact(fact_key="error_code", value="H70")])
    ctx = build_draft_claim_context(claims, ["exact_symptoms", "problem_start_time"])
    assert isinstance(ctx, DraftClaimContext)
    unknown = {c.proposition_key for c in ctx.unknown_fields}
    assert {"exact_symptoms", "problem_start_time"} <= unknown
    for claim in ctx.unknown_fields:
        if claim.proposition_key in {"exact_symptoms", "problem_start_time"}:
            assert claim.status == UNKNOWN
            assert claim.value == ""


def test_contract_forbids_extra_fields() -> None:
    with pytest.raises(ValueError):
        EpistemicClaim(
            claim_id="c1",
            proposition_key="x",
            value="y",
            status=CONFIRMED,
            forged="field",  # type: ignore[call-arg]
        )
