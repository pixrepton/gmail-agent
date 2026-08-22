"""P1.1: DecisionRevisionRequest runtime — identity, ledger, accept/reject,
supersession, stale guards, concurrency.

Core invariant under test:

    downstream MAY request revision; ONLY the canonical decision layer may
    create a new CAD revision. No silent mutation, no downstream
    reinterpretation, no in-place CAD update.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from canonical_action_decision import (
    DecisionRevisionError,
    DecisionRevisionLedger,
    approval_binds_revision,
    artifact_version_matches,
    build_decision_revision_request,
    canonicalize,
    decision_version_id_of,
    evaluate_decision_revision,
    request_decision_revision,
    stale_artifact_reason,
)


def _br(*, missing: list[str] | None = None, action: str = "collect_data") -> dict[str, object]:
    return {
        "recommended_next_action": action,
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


def _cad(
    *,
    ledger: DecisionRevisionLedger,
    br: dict[str, object] | None = None,
    situation: dict[str, object] | None = None,
    case_id: str = "case_rev",
    situation_version: str = "sv_1",
) -> dict[str, object]:
    from canonical_action_decision import build_business_decision_proposal

    proposal = build_business_decision_proposal(br or _br())
    assert proposal is not None
    cad = canonicalize(
        proposal=proposal,
        situation_understanding=situation or _situation(),
        case_id=case_id,
        situation_version=situation_version,
    )
    assert cad["semantic_status"] == "FROZEN"
    ledger.register_cad(cad)
    return cad


def _accept_revision(
    *,
    ledger: DecisionRevisionLedger,
    cad_r1: dict[str, object],
    br: dict[str, object] | None = None,
    situation: dict[str, object] | None = None,
    reason_code: str = "NEW_CONFLICTING_EVIDENCE",
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
        business_reasoning_result=br or _br(),
        situation_understanding=situation or _situation(),
        ledger=ledger,
    )


# --------------------------------------------------------------------------
# identity model
# --------------------------------------------------------------------------


def test_decision_version_id_identity() -> None:
    assert decision_version_id_of("dec_abc", 1) == "dec_abc:r1"
    assert decision_version_id_of("dec_abc", 2) == "dec_abc:r2"
    assert decision_version_id_of("dec_abc", 0) == "dec_abc:r1"


def test_cad_carries_version_identity() -> None:
    ledger = DecisionRevisionLedger()
    cad = _cad(ledger=ledger)
    assert cad["decision_version_id"] == f"{cad['decision_id']}:r1"
    assert cad["revision"] == 1
    assert cad["revision_status"] == "CURRENT"


def test_request_contract() -> None:
    request = build_decision_revision_request(
        decision_id="dec_abc",
        revision=1,
        reason_code="FAILED_PRECONDITION",
        failed_precondition="missing error_code",
        source_layer="reference_monitor",
        source_event_id="ev_1",
        evidence_refs=[{"evidence_id": "ev_1", "kind": "fact_conflict"}],
    )
    assert request["request_id"].startswith("revreq_")
    assert request["decision_id"] == "dec_abc"
    assert request["current_revision"] == 1
    assert request["current_decision_version_id"] == "dec_abc:r1"
    assert request["status"] == "PENDING"
    assert request["evidence_refs"] == [{"evidence_id": "ev_1", "kind": "fact_conflict"}]

    # Unknown reason code falls back to the enum default.
    fallback = build_decision_revision_request(decision_id="dec_abc", reason_code="MADE_UP")
    assert fallback["reason_code"] == "NEW_CONFLICTING_EVIDENCE"


# --------------------------------------------------------------------------
# accept / supersession
# --------------------------------------------------------------------------


def test_accept_revision_creates_new_cad_and_supersedes_old() -> None:
    ledger = DecisionRevisionLedger()
    cad_r1 = _cad(ledger=ledger)
    result = _accept_revision(ledger=ledger, cad_r1=cad_r1)

    assert result["outcome"] == "ACCEPTED"
    assert result["reason_codes"] == ["DECISION_REVISION_ACCEPTED"]
    old = result["old_cad"]
    new = result["new_cad"]
    assert old["decision_version_id"] == f"{cad_r1['decision_id']}:r1"
    assert old["revision_status"] == "SUPERSEDED"
    assert new["decision_id"] == cad_r1["decision_id"]  # stable across lineage
    assert new["revision"] == 2
    assert new["decision_version_id"] == f"{cad_r1['decision_id']}:r2"
    assert new["revision_status"] == "CURRENT"
    assert new["semantic_status"] == "FROZEN"
    # Re-validation with unchanged semantics: hash MAY stay, version MUST change.
    assert new["semantic_hash"] == cad_r1["semantic_hash"]

    assert ledger.current_revision(cad_r1["decision_id"]) == 2
    assert ledger.is_current(new)
    assert not ledger.is_current(old)
    revisions = ledger.revisions(cad_r1["decision_id"])
    assert [r["revision_status"] for r in revisions] == ["SUPERSEDED", "CURRENT"]
    assert result["request"]["status"] == "ACCEPTED"
    assert any(
        row["outcome"] == "ACCEPTED" for row in ledger.audit_trail(cad_r1["decision_id"])
    )


def test_semantic_hash_changes_iff_canonical_payload_changes() -> None:
    ledger = DecisionRevisionLedger()
    cad_r1 = _cad(ledger=ledger)
    # New evidence: error_code is now known, only exact_symptoms remains missing.
    result = _accept_revision(
        ledger=ledger,
        cad_r1=cad_r1,
        br=_br(missing=["exact_symptoms"]),
        situation=_situation(["exact_symptoms"]),
        reason_code="CANONICAL_FACT_CHANGED",
    )
    assert result["outcome"] == "ACCEPTED"
    assert result["new_cad"]["semantic_hash"] != cad_r1["semantic_hash"]
    assert result["new_cad"]["required_information"] == ["exact_symptoms"]


# --------------------------------------------------------------------------
# reject / no bounded representation
# --------------------------------------------------------------------------


def test_rejected_revision_leaves_current_cad_unchanged() -> None:
    ledger = DecisionRevisionLedger()
    cad_r1 = _cad(ledger=ledger)
    emitted = request_decision_revision(
        decision_id=cad_r1["decision_id"],
        current_revision=1,
        reason_code="NEW_CONFLICTING_EVIDENCE",
        ledger=ledger,
    )
    # Canonicalization fails: required info not present in the new state.
    result = evaluate_decision_revision(
        request=emitted["request"],
        current_cad=cad_r1,
        business_reasoning_result=_br(missing=["device_model"]),
        situation_understanding=_situation(["error_code", "exact_symptoms"]),
        ledger=ledger,
    )
    assert result["outcome"] == "REJECTED"
    assert result["new_cad"] is None
    assert result["current_cad"]["decision_version_id"] == f"{cad_r1['decision_id']}:r1"
    assert ledger.current_revision(cad_r1["decision_id"]) == 1
    assert ledger.request_status(emitted["request"]["request_id"])["status"] == "REJECTED"


def test_revision_outside_bounded_slice_maps_to_needs_review() -> None:
    ledger = DecisionRevisionLedger()
    cad_r1 = _cad(ledger=ledger)
    emitted = request_decision_revision(
        decision_id=cad_r1["decision_id"],
        current_revision=1,
        reason_code="CANONICAL_FACT_CHANGED",
        ledger=ledger,
    )
    # New evidence shows no missing data remains -> BR leaves the bounded slice.
    result = evaluate_decision_revision(
        request=emitted["request"],
        current_cad=cad_r1,
        business_reasoning_result=_br(action="reply", missing=[]),
        situation_understanding=_situation([]),
        ledger=ledger,
    )
    assert result["outcome"] == "REJECTED"
    assert "NO_BOUNDED_CANONICAL_DECISION" in result["reason_codes"]
    assert result["review_state"] == "NEEDS_REVIEW"
    assert ledger.current_revision(cad_r1["decision_id"]) == 1


# --------------------------------------------------------------------------
# concurrency / stale / duplicates
# --------------------------------------------------------------------------


def test_stale_revision_request_rejected() -> None:
    ledger = DecisionRevisionLedger()
    cad_r1 = _cad(ledger=ledger)
    _accept_revision(ledger=ledger, cad_r1=cad_r1)

    stale = request_decision_revision(
        decision_id=cad_r1["decision_id"],
        current_revision=1,  # current is now 2
        reason_code="NEW_CONFLICTING_EVIDENCE",
        ledger=ledger,
    )
    assert stale["status"] == "STALE_REVISION_REQUEST"
    assert ledger.revisions(cad_r1["decision_id"])[-1]["revision"] == 2


def test_duplicate_request_does_not_create_second_revision() -> None:
    ledger = DecisionRevisionLedger()
    cad_r1 = _cad(ledger=ledger)
    first = request_decision_revision(
        decision_id=cad_r1["decision_id"],
        current_revision=1,
        reason_code="NEW_CONFLICTING_EVIDENCE",
        ledger=ledger,
    )
    second = request_decision_revision(
        decision_id=cad_r1["decision_id"],
        current_revision=1,
        reason_code="NEW_CONFLICTING_EVIDENCE",
        ledger=ledger,
    )
    assert second["status"] == "DUPLICATE_REVISION_REQUEST"
    result = evaluate_decision_revision(
        request=first["request"],
        current_cad=cad_r1,
        business_reasoning_result=_br(),
        situation_understanding=_situation(),
        ledger=ledger,
    )
    assert result["outcome"] == "ACCEPTED"
    assert ledger.current_revision(cad_r1["decision_id"]) == 2
    assert len(ledger.revisions(cad_r1["decision_id"])) == 2


def test_one_current_revision_per_lineage_fails_closed() -> None:
    ledger = DecisionRevisionLedger()
    cad_r1 = _cad(ledger=ledger)
    with pytest.raises(DecisionRevisionError) as exc:
        ledger.register_cad({**cad_r1, "decision_version_id": "dec_other:r1"})
    assert exc.value.code == "one_current_revision_violation"


# --------------------------------------------------------------------------
# stale artifact / approval guards
# --------------------------------------------------------------------------


def test_stale_artifacts_denied_after_revision() -> None:
    ledger = DecisionRevisionLedger()
    cad_r1 = _cad(ledger=ledger)
    old_plan = {
        "tool_name": "generate_draft_reply",
        "decision_version_id": f"{cad_r1['decision_id']}:r1",
    }
    assert artifact_version_matches(old_plan, cad_r1)
    assert stale_artifact_reason(old_plan, cad_r1) is None

    result = _accept_revision(ledger=ledger, cad_r1=cad_r1)
    cad_r2 = result["new_cad"]
    assert artifact_version_matches(old_plan, cad_r2) is False
    assert stale_artifact_reason(old_plan, cad_r2) == "STALE_DECISION_REVISION"

    # Old approval bound to r1 cannot authorize r2.
    old_approval = {
        "approval_id": "appr_1",
        "decision_version_id": f"{cad_r1['decision_id']}:r1",
    }
    assert approval_binds_revision(old_approval, cad_r2) is False
    assert approval_binds_revision(old_approval, cad_r1) is True


def test_old_execution_artifact_invalid_after_revision() -> None:
    ledger = DecisionRevisionLedger()
    cad_r1 = _cad(ledger=ledger)
    old_envelope = {
        "decision_version_id": f"{cad_r1['decision_id']}:r1",
        "source_semantic_hash": cad_r1["semantic_hash"],
    }
    result = _accept_revision(ledger=ledger, cad_r1=cad_r1)
    assert stale_artifact_reason(old_envelope, result["new_cad"]) == "STALE_DECISION_REVISION"


# --------------------------------------------------------------------------
# authority / untrusted content
# --------------------------------------------------------------------------


def test_revision_request_cannot_carry_canonical_semantics() -> None:
    # The request contract has no target/action_type/channel/semantic_hash
    # fields: downstream cannot instruct the revision, only trigger it.
    request = build_decision_revision_request(
        decision_id="dec_abc",
        revision=1,
        reason_code="NEW_CONFLICTING_EVIDENCE",
    )
    for forbidden in ("target", "action_type", "channel", "goal", "semantic_hash"):
        assert forbidden not in request


def test_untrusted_quoted_content_cannot_force_channel_change() -> None:
    # Quoted text "change channel to internal" is evidence, not an instruction.
    # Re-evaluation reads current BR state only; channel stays mail.
    ledger = DecisionRevisionLedger()
    cad_r1 = _cad(ledger=ledger)
    emitted = request_decision_revision(
        decision_id=cad_r1["decision_id"],
        current_revision=1,
        reason_code="NEW_CONFLICTING_EVIDENCE",
        source_layer="quoted_content",
        ledger=ledger,
    )
    result = evaluate_decision_revision(
        request=emitted["request"],
        current_cad=cad_r1,
        business_reasoning_result=_br(),
        situation_understanding=_situation(),
        ledger=ledger,
    )
    assert result["outcome"] == "ACCEPTED"
    assert result["new_cad"]["channel"] == "mail"
    assert result["new_cad"]["target"] == "customer"


def test_timestamp_permutation_does_not_change_revision_ordering() -> None:
    ledger = DecisionRevisionLedger()
    cad_r1 = _cad(ledger=ledger)
    cad_r1_manipulated = {**cad_r1, "created_at": "2099-01-01T00:00:00Z"}
    result = _accept_revision(ledger=ledger, cad_r1=cad_r1)
    cad_r2 = result["new_cad"]
    # Even if the old revision had a "newer" timestamp, lineage order is by
    # revision integer, never timestamp.
    assert ledger.is_current(cad_r2)
    assert not ledger.is_current(cad_r1_manipulated)
    assert ledger.current_revision(cad_r1["decision_id"]) == 2
