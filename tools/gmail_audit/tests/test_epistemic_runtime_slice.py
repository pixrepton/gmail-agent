"""P1.3: bounded production-faithful epistemic runtime slice.

Real seams used:
  - generate_draft_reply handler (deterministic composer) with a durable
    MailboxMemoryStore and Understanding missing fields;
  - epistemic projection + draft claim context + draft sanity gate;
  - P1.1 DecisionRevisionLedger for the legal CAD revision path when new
    CONFIRMED evidence changes required_information.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.draft_sanity import evaluate_draft_sanity
from agent_runtime.epistemic_projection import (
    build_draft_claim_context_from_store,
    evaluate_draft_epistemic_sanity,
)
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan
from agent_runtime.tools.handlers import generate_draft_reply
from canonical_action_decision import (
    DecisionRevisionLedger,
    build_business_decision_proposal,
    canonicalize,
    evaluate_decision_revision,
    request_decision_revision,
)
from llm_contracts.engagement_snapshot_v2 import CaseUnderstandingProjection
from llm_contracts.epistemic_claims import CONFIRMED, UNKNOWN
from mailbox_memory import InMemoryMailboxMemoryStore


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=2,
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="",
        kalk_top_agent_key="",
        kalk_top_timeout_sec=1,
        kalk_top_max_retries=1,
    )


def _br(*, missing: list[str]) -> dict:
    return {
        "recommended_next_action": "collect_data",
        "missing_information": missing,
        "recommended_action_reason": "Brak danych diagnostycznych.",
        "urgency": "normal",
        "confidence": {"action_confidence": 0.8, "business_confidence": 0.7},
    }


def _situation(*, missing: list[str]) -> dict:
    return {
        "missing_information": missing,
        "missing_critical_fields": missing,
    }


def _fact_row(
    *,
    fact_key: str,
    value: str,
    source_type: str,
    source_ref: str,
    origin: str,
    authority: str,
    confidence: float,
) -> dict:
    return {
        "fact_id": f"fact_{fact_key}_{source_ref}",
        "case_id": "case_p1_3",
        "message_id": source_ref,
        "document_id": "",
        "entity_scope": "case",
        "fact_key": fact_key,
        "normalized_value": value,
        "raw_value": value,
        "confidence": confidence,
        "observed_at": "2026-08-22T10:00:00Z",
        "source_type": source_type,
        "source_ref": source_ref,
        "status": "active",
        "metadata": {
            "source_origin": origin,
            "evidence_authority": authority,
            "instruction_authority": "NONE",
        },
    }


def _store_with_case() -> InMemoryMailboxMemoryStore:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case(
        {
            "case_id": "case_p1_3",
            "case_family": "mail_case",
            "status": "open",
            "customer_email": "customer@example.com",
            "metadata": {},
        }
    )
    return store


def _snapshot(*, missing: list[str]):
    from agent_runtime.store import build_initial_snapshot

    snap = build_initial_snapshot(
        case_id="case_p1_3",
        engagement_id="eng_p1_3",
        signal_id="sig_p1_3",
        trace_id="t_p1_3",
    )
    return snap.model_copy(
        update={
            "case_kind": "awaria_naprawa",
            "case_understanding": CaseUnderstandingProjection(
                source_signal_id="sig_p1_3",
                missing_critical_fields=missing,
            ),
        }
    )


def _ctx(snapshot, store) -> ToolExecutionContext:
    from agent_runtime.constitution import load_constitution

    return ToolExecutionContext.from_snapshot(
        snapshot,
        settings=_settings(),
        mailbox_store=store,
        signal_payload={"harness_mode": True},
        constitution=load_constitution(),
    )


def test_positive_epistemic_trajectory_produces_hitl_ready_draft() -> None:
    store = _store_with_case()
    store.append_fact_rows(
        [
            _fact_row(
                fact_key="error_code",
                value="H70",
                source_type="gmail_message",
                source_ref="msg_1",
                origin="CUSTOMER_EMAIL",
                authority="CUSTOMER_STATEMENT",
                confidence=0.9,
            ),
            _fact_row(
                fact_key="device_fault_cause",
                value="pompa obiegowa",
                source_type="inference",
                source_ref="msg_1",
                origin="DERIVED",
                authority="DERIVED_LLM_CLAIM",
                confidence=0.45,
            ),
        ]
    )
    snapshot = _snapshot(missing=["exact_symptoms", "problem_start_time"])
    ctx = _ctx(snapshot, store)
    context = build_draft_claim_context_from_store(
        store,
        "case_p1_3",
        ["exact_symptoms", "problem_start_time"],
    )
    assert context is not None
    assert {c.proposition_key for c in context.confirmed_claims} == {"error_code"}
    assert {c.proposition_key for c in context.inferred_claims} == {"device_fault_cause"}
    assert {"exact_symptoms", "problem_start_time"} <= {
        c.proposition_key for c in context.unknown_fields
    }

    result = generate_draft_reply(
        ToolCallPlan(tool_name="generate_draft_reply", arguments={"intent": "missing_info"}),
        ctx,
    )
    assert result.status == "ok"
    actions = list(result.snapshot_delta.get("actions") or [])
    body = str(actions[0].get("payload_pl") or "")
    assert "Dziękujemy za informację o kodzie błędu H70." in body
    assert "Prosimy o dokładny opis objawów." in body
    assert "Prosimy o informację, kiedy problem się zaczął." in body
    assert "pompa obiegowa" not in body  # INFERRED is never asserted
    assert result.snapshot_delta["hitl_gate"]["required"] is True
    assert result.snapshot_delta["hitl_gate"]["reason"] == "draft_ready_for_approval"


def test_positive_trajectory_when_error_code_unknown_asks_for_code() -> None:
    store = _store_with_case()
    snapshot = _snapshot(missing=["error_code", "exact_symptoms"])
    ctx = _ctx(snapshot, store)
    result = generate_draft_reply(
        ToolCallPlan(tool_name="generate_draft_reply", arguments={"intent": "missing_info"}),
        ctx,
    )
    assert result.status == "ok"
    body = str(result.snapshot_delta["actions"][0].get("payload_pl") or "")
    assert "Prosimy o podanie kodu błędu, jeśli jest dostępny." in body
    assert "Dziękujemy za informację o kodzie" not in body


def test_bad_draft_is_denied_by_epistemic_guard() -> None:
    store = _store_with_case()
    store.append_fact_rows(
        [
            _fact_row(
                fact_key="error_code",
                value="H70",
                source_type="gmail_message",
                source_ref="msg_1",
                origin="CUSTOMER_EMAIL",
                authority="CUSTOMER_STATEMENT",
                confidence=0.9,
            ),
            _fact_row(
                fact_key="device_fault_cause",
                value="pompa obiegowa",
                source_type="inference",
                source_ref="msg_1",
                origin="DERIVED",
                authority="DERIVED_LLM_CLAIM",
                confidence=0.45,
            ),
        ]
    )
    snapshot = _snapshot(missing=["exact_symptoms"])
    context = build_draft_claim_context_from_store(store, "case_p1_3", ["exact_symptoms"])
    bad_body = "Na pewno uszkodzona jest pompa obiegowa."
    sanity = evaluate_draft_sanity(
        body=bad_body,
        case_kind="awaria_naprawa",
        intent="missing_info",
        snapshot=snapshot,
        epistemic_context=context,
    )
    assert sanity["ok"] is False
    assert "INFERRED_AS_CONFIRMED" in sanity["reason_codes"]
    # No HITL-ready artifact for an epistemically unsupported draft.
    assert evaluate_draft_epistemic_sanity(
        body=bad_body,
        claim_context=context,
    )["ok"] is False


def test_new_confirmed_evidence_triggers_legal_cad_revision() -> None:
    store = _store_with_case()
    ledger = DecisionRevisionLedger(store=store)
    proposal = build_business_decision_proposal(_br(missing=["error_code", "exact_symptoms"]))
    assert proposal is not None
    cad_r1 = canonicalize(
        proposal=proposal,
        situation_understanding=_situation(missing=["error_code", "exact_symptoms"]),
        case_id="case_p1_3",
        situation_version="sv_1",
    )
    ledger.register_cad(cad_r1)
    assert ledger.current_cad(cad_r1["decision_id"])["decision_version_id"] == cad_r1["decision_version_id"]

    # New CONFIRMED evidence: customer reported error code H70.
    store.append_fact_rows(
        [
            _fact_row(
                fact_key="error_code",
                value="H70",
                source_type="gmail_message",
                source_ref="msg_2",
                origin="CUSTOMER_EMAIL",
                authority="CUSTOMER_STATEMENT",
                confidence=0.95,
            )
        ]
    )
    claims = __import__("agent_runtime.epistemic_projection", fromlist=["project_epistemic_claims"]).project_epistemic_claims(
        store.fetch_active_facts_for_case("case_p1_3") or []
    )
    error_claim = next(c for c in claims if c.proposition_key == "error_code")
    assert error_claim.status == CONFIRMED

    emitted = request_decision_revision(
        decision_id=cad_r1["decision_id"],
        current_revision=1,
        reason_code="NEW_CONFLICTING_EVIDENCE",
        source_layer="epistemic_projection",
        ledger=ledger,
    )
    result = evaluate_decision_revision(
        request=emitted["request"],
        current_cad=cad_r1,
        business_reasoning_result=_br(missing=["exact_symptoms"]),
        situation_understanding=_situation(missing=["exact_symptoms"]),
        ledger=ledger,
    )
    assert result["outcome"] == "ACCEPTED"
    r2 = result["new_cad"]
    assert r2["revision"] == 2
    assert r2["required_information"] == ["exact_symptoms"]
    assert ledger.current_cad(cad_r1["decision_id"])["decision_version_id"] == r2["decision_version_id"]
    # P1.1: semantic_hash is recomputed from canonical payload, never hand-edited.
    expected_r2 = canonicalize(
        proposal=build_business_decision_proposal(_br(missing=["exact_symptoms"])),
        situation_understanding=_situation(missing=["exact_symptoms"]),
        case_id="case_p1_3",
        situation_version="sv_1",
    )
    assert r2["semantic_hash"] == expected_r2["semantic_hash"]
