"""Adaptive projection composer policy and compose integration."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import Settings
from llm_contracts.projection_compose import ProjectionComposeResult
from llm_projection_composer import apply_llm_enrichment_to_envelope, compose_projection_from_trays
from projection_composer_policy import resolve_projection_composer_mode
from projection_envelope import build_projection_envelope


def _trays(*, essence: str = "Sprawa o montaż pompy", gaps: int = 0, conflicts: int = 0) -> dict:
    gaps_tray = [{"gap_key": f"g{i}", "content_pl": "Brak danych"} for i in range(gaps)]
    conflicts_tray = [{"conflict_key": f"c{i}", "summary": "Konflikt"} for i in range(conflicts)]
    return {
        "schema_version": "context_tray_set.v1",
        "case_id": "case-policy-1",
        "essence_tray": [{"summary": essence}],
        "gaps_tray": gaps_tray,
        "conflicts_tray": conflicts_tray,
        "facts_tray": [],
        "evidence_tray": [],
    }


def _settings_with_llm() -> Settings:
    from tests.test_central_llm_stage import _minimal_settings

    return _minimal_settings(groq_api_key="gsk_test_projection")


def test_adaptive_skip_lane_is_deterministic() -> None:
    decision = resolve_projection_composer_mode(
        _trays(),
        stage_outputs={"preclassification_result": {"lane": "skip"}},
        settings=_settings_with_llm(),
    )
    assert decision["mode"] == "deterministic"
    assert decision["decision_reason"] == "lane_skip"


def test_adaptive_sufficient_trays_is_deterministic() -> None:
    decision = resolve_projection_composer_mode(
        _trays(essence="Klient prosi o wycenę pompy ciepła"),
        stage_outputs={"preclassification_result": {"lane": "intake_llm"}},
        settings=_settings_with_llm(),
    )
    assert decision["mode"] == "deterministic"
    assert decision["decision_reason"] == "trays_sufficient"


def test_adaptive_gaps_request_llm_when_available() -> None:
    decision = resolve_projection_composer_mode(
        _trays(gaps=1),
        stage_outputs={"preclassification_result": {"lane": "intake_llm"}},
        settings=_settings_with_llm(),
    )
    assert decision["mode"] == "llm"
    assert decision["decision_reason"] == "gaps_or_conflicts_present"


def test_adaptive_gaps_fall_back_without_api_key() -> None:
    decision = resolve_projection_composer_mode(
        _trays(gaps=1),
        stage_outputs={"preclassification_result": {"lane": "intake_llm"}},
        settings=None,
    )
    assert decision["mode"] == "deterministic"
    assert decision["decision_reason"] == "gaps_or_conflicts_present_llm_unavailable"


def test_compose_deterministic_sets_composer_metadata() -> None:
    envelope, decision = compose_projection_from_trays(
        _trays(),
        stage_outputs={"preclassification_result": {"lane": "intake_llm"}},
        settings=_settings_with_llm(),
    )
    assert decision["mode"] == "deterministic"
    assert envelope["composer"]["provider"] == "deterministic"
    assert envelope["composer"]["live_llm"] is False
    assert envelope["projection_validation"]["ok"] is True


@patch("llm_projection_composer.run_projection_llm_compose")
def test_compose_llm_path_enriches_envelope(mock_llm: MagicMock) -> None:
    mock_llm.return_value = (
        ProjectionComposeResult(
            essence_summary_pl="Esencja z LLM",
            operator_visibility_note_pl="Uwaga na luki",
        ),
        {"parse_status": "pydantic_validated"},
    )
    trays = _trays(gaps=1)
    envelope, decision = compose_projection_from_trays(
        trays,
        stage_outputs={"preclassification_result": {"lane": "intake_llm"}},
        settings=_settings_with_llm(),
    )
    assert decision["mode"] == "llm"
    assert envelope["composer"]["provider"] == "llm"
    assert envelope["composer"]["live_llm"] is True
    assert envelope["desk_cards"][0]["summary"] == "Esencja z LLM"
    mock_llm.assert_called_once()


def test_apply_llm_enrichment_preserves_validation_shape() -> None:
    base = build_projection_envelope(_trays())
    enriched = apply_llm_enrichment_to_envelope(
        base,
        ProjectionComposeResult(essence_summary_pl="Nowa esencja"),
        composer_meta={"provider": "llm", "decision_reason": "test"},
    )
    assert enriched["desk_cards"][0]["summary"] == "Nowa esencja"
