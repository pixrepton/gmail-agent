"""Tests for operator-facing understanding quality and readiness facets projection."""

from __future__ import annotations

from operator_projection_quality import (
    build_readiness_facets_projection,
    build_understanding_quality_projection,
)


def test_understanding_quality_available_model_result() -> None:
    intel = {
        "execution_metadata": {
            "case_understanding_provenance": {
                "schema_version": "v1",
                "availability": "available",
                "source_mode": "model_result",
                "validation_state": "clean",
                "reason_codes": [],
                "observed_at": "2026-01-01T00:00:00Z",
            }
        }
    }
    out = build_understanding_quality_projection(intel)
    assert out is not None
    assert out["availability"] == "available"
    assert out["operator_label_pl"] == "Pełne rozumienie sprawy"


def test_understanding_quality_not_required_skipped_lane() -> None:
    intel = {
        "execution_metadata": {
            "case_understanding_provenance": {
                "availability": "not_required",
                "source_mode": "skipped_for_lane",
                "validation_state": "",
                "reason_codes": [],
                "observed_at": "",
            }
        }
    }
    out = build_understanding_quality_projection(intel)
    assert out is not None
    assert "niewymagane" in out["operator_label_pl"].lower()


def test_understanding_quality_missing_provenance_returns_none() -> None:
    assert build_understanding_quality_projection({}) is None
    assert build_understanding_quality_projection({"execution_metadata": {}}) is None


def test_readiness_facets_blocked_by_critical_missing() -> None:
    intel = {
        "case_guidance": {"business_readiness": "blocked"},
        "case_understanding": {"review_required": True},
        "missing_info": {"critical": ["address"], "important": []},
    }
    out = build_readiness_facets_projection(intel, projection_state={"conflicting_facts": [{"x": 1}]})
    assert out["context_readiness"] == "not_ready"
    assert out["blocked_by_data"] is True
    assert out["gap_count"] >= 1
    assert out["conflict_count"] == 1
