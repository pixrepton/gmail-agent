"""P1.3: draft epistemic guard + deterministic composer realization."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.epistemic_projection import (
    build_draft_claim_context,
    evaluate_draft_epistemic_sanity,
    project_epistemic_claims,
)
from agent_runtime.tools.handlers import _compose_service_missing_info_body
from llm_contracts.epistemic_claims import (
    CONFIRMED,
    CONFLICTED,
    INFERRED,
    UNKNOWN,
    DraftClaimContext,
    EpistemicClaim,
)


def _claim(
    *,
    key: str,
    value: str = "",
    status: str = CONFIRMED,
    evidence: bool = True,
    basis: list[str] | None = None,
) -> EpistemicClaim:
    return EpistemicClaim(
        claim_id=f"epi_{key}",
        proposition_key=key,
        value=value,
        status=status,
        evidence_refs=[{"source_type": "gmail_message", "source_id": "msg_1"}] if evidence else [],
        inference_basis=basis or [],
        conflicted=status == CONFLICTED,
        decision_usable=status != CONFLICTED,
    )


def _ctx(**claims) -> DraftClaimContext:
    confirmed = [c for c in claims.get("confirmed", [])]
    inferred = [c for c in claims.get("inferred", [])]
    unknown = [c for c in claims.get("unknown", [])]
    conflicted = [c for c in claims.get("conflicted", [])]
    return DraftClaimContext(
        confirmed_claims=confirmed,
        inferred_claims=inferred,
        unknown_fields=unknown,
        conflicted_fields=conflicted,
    )


def test_composer_acknowledges_confirmed_and_asks_only_unknowns() -> None:
    ctx = _ctx(
        confirmed=[_claim(key="error_code", value="H70")],
        unknown=[
            _claim(key="exact_symptoms", status=UNKNOWN),
            _claim(key="problem_start_time", status=UNKNOWN),
        ],
    )
    body = _compose_service_missing_info_body(epistemic_context=ctx, legacy_body="LEGACY")
    assert "Dziękujemy za informację o kodzie błędu H70." in body
    assert "Prosimy o dokładny opis objawów." in body
    assert "Prosimy o informację, kiedy problem się zaczął." in body
    assert "LEGACY" not in body


def test_composer_preserves_legacy_template_without_context() -> None:
    body = _compose_service_missing_info_body(epistemic_context=None, legacy_body="LEGACY")
    assert body == "LEGACY"
    body2 = _compose_service_missing_info_body(
        epistemic_context=_ctx(),
        legacy_body="LEGACY",
    )
    assert body2 == "LEGACY"


def test_guard_positive_composed_body_passes() -> None:
    ctx = _ctx(
        confirmed=[_claim(key="error_code", value="H70")],
        unknown=[_claim(key="exact_symptoms", status=UNKNOWN)],
    )
    body = _compose_service_missing_info_body(epistemic_context=ctx, legacy_body="LEGACY")
    result = evaluate_draft_epistemic_sanity(body=body, claim_context=ctx)
    assert result["ok"] is True


def test_guard_denies_inferred_as_certainty() -> None:
    ctx = _ctx(inferred=[_claim(key="device_fault_cause", value="pompa obiegowa", status=INFERRED, basis=["error_code:H70"])])
    result = evaluate_draft_epistemic_sanity(
        body="Na pewno uszkodzona jest pompa obiegowa.",
        claim_context=ctx,
    )
    assert result["ok"] is False
    assert "INFERRED_AS_CONFIRMED" in result["reason_codes"]


def test_guard_denies_unknown_asserted_as_fact() -> None:
    ctx = _ctx(unknown=[_claim(key="error_code", status=UNKNOWN)])
    result = evaluate_draft_epistemic_sanity(
        body="Widzę kod H70 na sterowniku.",
        claim_context=ctx,
    )
    assert result["ok"] is False
    assert "UNKNOWN_AS_CONFIRMED" in result["reason_codes"]


def test_guard_denies_conflicted_fact_asserted() -> None:
    ctx = _ctx(conflicted=[_claim(key="heated_area_m2", value="120", status=CONFLICTED)])
    result = evaluate_draft_epistemic_sanity(
        body="Metraż budynku to 120 m2.",
        claim_context=ctx,
    )
    assert result["ok"] is False
    assert "CONFLICTED_FACT_ASSERTED" in result["reason_codes"]


def test_guard_denies_confirmed_without_evidence() -> None:
    ctx = _ctx(confirmed=[_claim(key="error_code", value="H70", evidence=False)])
    result = evaluate_draft_epistemic_sanity(
        body="Dziękujemy za informację o kodzie błędu H70.",
        claim_context=ctx,
    )
    assert result["ok"] is False
    assert "CONFIRMED_WITHOUT_EVIDENCE" in result["reason_codes"]


def test_guard_denies_unsupported_customer_fact() -> None:
    ctx = _ctx(confirmed=[_claim(key="error_code", value="H70")])
    result = evaluate_draft_epistemic_sanity(
        body="Proszę podać kod H99.",
        claim_context=ctx,
    )
    assert result["ok"] is False
    assert "UNSUPPORTED_CUSTOMER_FACT" in result["reason_codes"]


def test_guard_confirmed_value_may_be_asserted() -> None:
    ctx = _ctx(confirmed=[_claim(key="error_code", value="H70")])
    result = evaluate_draft_epistemic_sanity(
        body="Dziękujemy za informację o kodzie błędu H70.",
        claim_context=ctx,
    )
    assert result["ok"] is True


def test_guard_is_noop_without_context() -> None:
    result = evaluate_draft_epistemic_sanity(body="Cokolwiek.", claim_context=None)
    assert result["ok"] is True
