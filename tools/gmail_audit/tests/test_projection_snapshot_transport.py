"""PR-J: operator projection snapshot backward compatibility and envelope fields."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from projection_snapshot_transport import (
    OPERATOR_PROJECTION_SNAPSHOT_SCHEMA_VERSION,
    build_operator_projection_snapshot,
    v2_projection_from_snapshot,
)


def _minimal_stage_outputs() -> dict:
    ci = {
        "understanding_output": {"operator_explanation": {"essence_pl": "Sedno testowe"}},
        "decision_pipeline": {"outputs": {}},
        "policy_decision": {},
        "action_proposals_v2": [],
    }
    return {
        "preclassification_result": {"lane": "intake_llm"},
        "case_link_result": {"selected_case_key": "case-transport-1"},
        "business_reasoning_result": {},
        "reply_draft_result": {},
        "action_plan_result": {"primary_action": "review"},
        "case_intelligence_result": ci,
        "mailbox_memory_result": {
            "context_pack": {
                "vnext": {
                    "case_id": "case-transport-1",
                    "case_summary": {"summary_text": "Sprawa testowa"},
                    "completeness_gaps": [{"gap_key": "site_visit", "content_pl": "Brak wizji"}],
                }
            }
        },
        "generated_at": "2026-05-19T18:00:00Z",
    }


def test_v2_projection_from_snapshot_ignores_new_fields() -> None:
    snap = build_operator_projection_snapshot(
        {"decision": {"action": "review"}, "review": {"flags": []}, "source": {}, "message": {}, "thread": {}},
        stage_outputs=_minimal_stage_outputs(),
        run_id="run-transport-1",
    )
    legacy_consumer = v2_projection_from_snapshot(snap)
    assert isinstance(legacy_consumer, dict)
    assert "signal_projection" in legacy_consumer
    assert "decision_view" in legacy_consumer

    assert snap["schema_version"] == OPERATOR_PROJECTION_SNAPSHOT_SCHEMA_VERSION
    assert snap["context_tray_set"]["schema_version"] == "context_tray_set.v1"
    assert snap["projection_envelope"]["schema_version"] == "projection_envelope.v1"
    assert snap["projection_validation"]["ok"] is True
    assert isinstance(snap.get("projection_composer_decision"), dict)
    assert snap["projection_envelope"]["composer"]["provider"] == "deterministic"
    assert snap["daszek_routes"]["schema_version"] == "daszek_projection_router.v1"
    assert snap["projection_quality_metrics"]["schema_version"] == "projection_quality_metrics.v1"


def test_snapshot_preserves_top_level_decision_view_for_legacy_readers() -> None:
    snap = build_operator_projection_snapshot(
        {"decision": {"action": "review"}, "review": {"flags": []}, "source": {}, "message": {}, "thread": {}},
        stage_outputs=_minimal_stage_outputs(),
        run_id="run-transport-2",
    )
    assert isinstance(snap.get("decision_view"), dict)
    v2 = snap.get("v2_projection") if isinstance(snap.get("v2_projection"), dict) else {}
    assert v2.get("decision_view") == snap.get("decision_view")


def test_snapshot_suppresses_desk_and_tasks_for_non_business_noise() -> None:
    stage_outputs = _minimal_stage_outputs()
    stage_outputs["business_reasoning_result"] = {
        "recommended_next_action": "wait",
        "business_area": "spam",
    }
    snap = build_operator_projection_snapshot(
        {"decision": {"action": "wait"}, "review": {"flags": []}, "source": {}, "message": {}, "thread": {}},
        stage_outputs=stage_outputs,
        run_id="run-transport-spam",
    )
    envelope = snap["projection_envelope"]
    routes = snap["daszek_routes"]
    assert envelope.get("desk_tasks_suppressed") is True
    assert envelope.get("desk_cards") == []
    assert envelope.get("task_candidates") == []
    assert routes["desk_surface_policy"]["suppressed"] is True
    assert routes["surfaces"]["desk"] == []
    assert routes["surfaces"]["tasks"] == []
