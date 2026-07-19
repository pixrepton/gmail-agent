from __future__ import annotations

import json
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from decision_candidate import (  # noqa: E402
    DECISION_CANDIDATE_SCHEMA_VERSION,
    build_decision_candidate,
    sanitize_decision_candidate_for_projection,
    validate_decision_candidate,
)


def _pack(*, ready: bool = False, action_readiness: str = "review_only") -> dict:
    return {
        "context_quality": {
            "ready_for_decision": ready,
            "operator_review_possible": True,
            "action_readiness": action_readiness,
            "not_ready_reasons": [] if ready else ["weak_or_missing_evidence"],
            "weak_evidence_count": 1 if not ready else 0,
            "evidence_warning_count": 1 if not ready else 0,
            "has_blocking_conflicts": False,
            "has_blocking_gaps": False,
        },
        "conflicting_facts": [
            {
                "conflict_id": "conf_weak",
                "fact_key": "customer_email",
                "summary": "client@example.invalid vs other@example.invalid",
                "projection_summary": "Sprzeczne dane kontaktowe - wymaga weryfikacji operatora.",
                "decision_usable": False,
                "evidence_status": "missing",
                "evidence_refs": [],
                "values": ["client@example.invalid", "other@example.invalid"],
            }
        ],
        "completeness_gaps": [],
        "evidence_cards": [],
    }


def test_weak_conflict_is_warning_not_decision_basis() -> None:
    cand = build_decision_candidate(
        case_id="case_1",
        source_signal_id="sig_1",
        topic="service",
        case_type="service_request",
        priority="high",
        sla_risk="medium",
        owner_hint="operator",
        next_best_action={"action_type": "review_required", "title_pl": "Sprawdz konflikt"},
        decision_basis=[
            {
                "basis_id": "weak_basis",
                "summary": "Atrakcyjny, ale slaby konflikt",
                "decision_usable": False,
                "evidence_status": "missing",
                "evidence_refs": [],
            }
        ],
        case_context_pack=_pack(),
    )

    valid, errors = validate_decision_candidate(cand)

    assert errors == []
    assert valid["schema_version"] == DECISION_CANDIDATE_SCHEMA_VERSION
    assert valid["decision_basis"] == []
    assert valid["review_only_warnings"]
    assert valid["requires_operator_review"] is True
    assert valid["automation_eligibility"] == "not_eligible"
    assert valid["recommended_mode"] == "operator_review_only"


def test_supported_conflict_is_not_automatic_allow_reason() -> None:
    pack = _pack(ready=True, action_readiness="decision_ready")
    pack["conflicting_facts"] = [
        {
            "conflict_id": "conf_supported",
            "fact_key": "device_power",
            "summary": "Power mismatch",
            "projection_summary": "Power mismatch",
            "severity": "warning",
            "decision_usable": True,
            "evidence_status": "supported",
            "evidence_refs": [
                {"source_type": "gmail_message", "source_id": "m1"},
                {"source_type": "drive_document", "source_id": "d1"},
            ],
        }
    ]

    cand = build_decision_candidate(
        case_id="case_2",
        source_signal_id="sig_2",
        topic="service",
        case_type="service_request",
        next_best_action="operator_review",
        case_context_pack=pack,
    )

    assert cand["decision_basis"] == []
    assert cand["automation_eligibility"] == "not_eligible"
    assert cand["requires_policy"] is True
    assert any("Power mismatch" in str(item) for item in cand["review_only_warnings"])


def test_review_only_readiness_blocks_decision_ready_status() -> None:
    cand = build_decision_candidate(
        case_id="case_3",
        source_signal_id="sig_3",
        topic="service",
        case_type="service_request",
        next_best_action="prepare_reply",
        case_context_pack=_pack(ready=False, action_readiness="review_only"),
    )

    assert cand["automation_eligibility"] == "not_eligible"
    assert cand["recommended_mode"] == "operator_review_only"
    assert "weak_or_missing_evidence" in cand["not_ready_reasons"]


def test_not_ready_context_uses_not_ready_mode() -> None:
    pack = _pack(ready=False, action_readiness="not_ready")
    pack["context_quality"]["has_blocking_gaps"] = True
    pack["context_quality"]["not_ready_reasons"] = ["blocking_gaps"]
    pack["completeness_gaps"] = [
        {
            "gap_id": "gap_1",
            "summary": "Brak terminu serwisu.",
            "severity": "blocking",
            "decision_usable": False,
            "evidence_status": "missing",
            "evidence_refs": [],
        }
    ]

    cand = build_decision_candidate(
        case_id="case_4",
        source_signal_id="sig_4",
        topic="service",
        case_type="service_request",
        next_best_action="schedule_service",
        case_context_pack=pack,
    )

    assert cand["recommended_mode"] == "not_ready"
    assert cand["blocking_gaps"]
    assert cand["automation_eligibility"] == "not_eligible"


def test_projection_sanitizer_removes_pii_and_forbidden_keys() -> None:
    cand = build_decision_candidate(
        case_id="case_5",
        source_signal_id="sig_5",
        topic="service",
        case_type="service_request",
        next_best_action={"title": "Call +48 600 700 800", "body": "private"},
        decision_basis=[
            {
                "basis_id": "basis_ok",
                "summary": "client@example.invalid should not leak",
                "decision_usable": True,
                "evidence_status": "supported",
                "evidence_refs": [{"source_type": "gmail_message", "source_id": "m5"}],
                "values": ["client@example.invalid"],
            }
        ],
        case_context_pack=_pack(ready=True, action_readiness="decision_ready"),
    )

    projection = sanitize_decision_candidate_for_projection(cand)
    blob = json.dumps(projection, ensure_ascii=False)

    for bad in ("body", "snippet", "prompt", "raw_llm", "raw_response", "values", "facts_in_conflict"):
        assert f'"{bad}"' not in blob
    assert "example.invalid" not in blob
    assert "600 700 800" not in blob


def test_validate_flags_malformed_candidate() -> None:
    valid, errors = validate_decision_candidate(
        {
            "schema_version": "wrong",
            "recommended_mode": "bad_mode",
            "decision_basis": [
                {
                    "summary": "Looks useful",
                    "decision_usable": False,
                    "evidence_status": "weak",
                    "evidence_refs": [],
                }
            ],
        }
    )

    assert "invalid_schema_version" in errors
    assert "missing_case_id" in errors
    assert "missing_source_signal_id" in errors
    assert valid["schema_version"] == DECISION_CANDIDATE_SCHEMA_VERSION
    assert valid["recommended_mode"] == "operator_review_only"
    assert valid["decision_basis"] == []
    assert valid["review_only_warnings"]


def test_decision_candidate_id_is_stable_for_canonical_input() -> None:
    first = build_decision_candidate(
        case_id="case_stable",
        source_signal_id="sig_stable",
        topic="service",
        case_type="service_request",
        next_best_action={"b": 2, "a": 1},
        case_context_pack=_pack(ready=True, action_readiness="decision_ready"),
    )
    second = build_decision_candidate(
        source_signal_id="sig_stable",
        case_id="case_stable",
        case_type="service_request",
        topic="service",
        next_best_action={"a": 1, "b": 2},
        case_context_pack=_pack(ready=True, action_readiness="decision_ready"),
    )

    assert first["decision_candidate_id"] == second["decision_candidate_id"]
    assert first["decision_candidate_id"].startswith("dc_")
