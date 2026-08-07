"""AI-OS 4.4 — Install-prep projection from active Case facts (not a second SoT)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from install_prep_projection import project_install_prep  # noqa: E402


def test_incomplete_when_required_keys_missing() -> None:
    out = project_install_prep(
        case_id="case_ip_1",
        active_facts=[
            {"fact_key": "city", "normalized_value": "Radlin", "status": "active"},
        ],
    )
    assert out["ready"] is False
    assert out["status"] == "incomplete"
    assert "heated_area_m2" in out["missing_required_keys"]
    assert out["second_sot"] is False
    assert out["source"] == "case_active_facts"


def test_ready_when_required_keys_present() -> None:
    out = project_install_prep(
        case_id="case_ip_2",
        active_facts=[
            {"fact_key": "heated_area_m2", "normalized_value": "140", "status": "active"},
            {"fact_key": "city", "normalized_value": "Radlin", "status": "active"},
            {"fact_key": "building_type", "normalized_value": "dom", "status": "active"},
            {"fact_key": "ozc_kw", "normalized_value": "8", "status": "active"},
        ],
    )
    assert out["ready"] is True
    assert out["status"] == "ready"
    assert out["missing_required_keys"] == []
    assert out["values"]["city"] == "Radlin"
    assert "ozc_kw" in out["present_keys"]


def test_ignores_superseded_leak() -> None:
    out = project_install_prep(
        case_id="case_ip_3",
        active_facts=[
            {"fact_key": "heated_area_m2", "normalized_value": "999", "status": "superseded"},
            {"fact_key": "heated_area_m2", "normalized_value": "140", "status": "active"},
            {"fact_key": "city", "normalized_value": "Radlin", "status": "active"},
            {"fact_key": "building_type", "normalized_value": "dom", "status": "active"},
        ],
    )
    assert out["ready"] is True
    assert out["values"]["heated_area_m2"] == "140"
