from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from daszek_projection_router import route_projection_envelope
from daszek_v3_feed_runtime import (
    accumulate_projection_route_overlay,
    case_id_and_routes_from_reconcile,
    projection_routes_from_snapshot,
)
from daszek_v3_operational_feed import apply_projection_route_overlays, build_operational_feed_snapshot
from operator_visibility_policy import apply_desk_composition_visibility, surface_zone_from_desk_composition


def test_projection_routes_from_operator_snapshot() -> None:
    envelope = {
        "schema_version": "projection_envelope.v1",
        "case_id": "case-abc",
        "desk_cards": [{"card_type": "case_essence", "title": "Router desk", "summary": "Essence"}],
        "task_candidates": [{"task_id": "t-router", "title": "Task", "read_only": True}],
        "case_detail_blocks": [{"block_type": "essence", "content": "Detail"}],
        "gap_blocks": [],
        "conflict_blocks": [],
    }
    routes = route_projection_envelope(envelope)
    snap = {"projection_envelope": envelope, "daszek_routes": routes}
    assert projection_routes_from_snapshot(snap)["schema_version"] == "daszek_projection_router.v1"
    assert len(projection_routes_from_snapshot(snap)["surfaces"]["desk"]) == 1


def test_apply_projection_route_overlays_backfills_case_operator_essence_pl() -> None:
    base = build_operational_feed_snapshot(
        cockpit={"desk": {"items": []}, "cases": {"items": []}},
        day=None,
        tasks=None,
        snapshot_id="base-feed-essence",
    )
    base["feed"]["cases"] = [
        {
            "case_id": "case-xyz",
            "title": "Sprawa XYZ",
            "status": "open",
            "summary": "",
            "operator_brief_pl": "",
            "operator_essence_pl": "",
        }
    ]
    routes = route_projection_envelope(
        {
            "schema_version": "projection_envelope.v1",
            "case_id": "case-xyz",
            "desk_cards": [
                {
                    "card_type": "case_essence",
                    "title": "Pytanie o wycenę",
                    "summary": "Klient z Krakowa pyta o wycenę instalacji pompy ciepła.",
                }
            ],
            "task_candidates": [],
            "case_detail_blocks": [],
            "gap_blocks": [],
            "conflict_blocks": [],
        }
    )
    merged = apply_projection_route_overlays(base, {"case-xyz": routes})
    case = next(c for c in merged["feed"]["cases"] if c["case_id"] == "case-xyz")
    assert case.get("operator_essence_pl") == "Klient z Krakowa pyta o wycenę instalacji pompy ciepła."
    assert case.get("operator_brief_pl") == case["operator_essence_pl"]
    assert case.get("summary") == case["operator_essence_pl"]


def test_apply_projection_route_overlays_merges_desk_and_tasks() -> None:
    base = build_operational_feed_snapshot(
        cockpit={"desk": {"items": []}, "cases": {"items": []}},
        day=None,
        tasks=None,
        snapshot_id="base-feed",
    )
    base["feed"]["cases"] = [
        {
            "case_id": "case-xyz",
            "title": "Sprawa XYZ",
            "status": "open",
            "badges": {"blocking_conflict": True},
            "conflicting_facts": [{"summary": "konflikt"}],
        }
    ]
    routes = route_projection_envelope(
        {
            "schema_version": "projection_envelope.v1",
            "case_id": "case-xyz",
            "desk_cards": [{"card_type": "router_card", "title": "Z routera", "summary": "Router summary"}],
            "task_candidates": [{"task_id": "router-task-1", "title": "Router task", "read_only": True}],
            "case_detail_blocks": [{"block_type": "essence", "content": "from router"}],
            "gap_blocks": [],
            "conflict_blocks": [],
        }
    )
    merged = apply_projection_route_overlays(base, {"case-xyz": routes})
    desk = merged["feed"]["desk"]
    assert any(str(d.get("title") or "").startswith("Z routera") for d in desk)
    task_ids = {str(t.get("task_id") or "") for t in merged["feed"].get("action_items") or merged["feed"].get("tasks") or [] if str(t.get("task_id") or "").strip()}
    assert "router-task-1" in task_ids
    detail = merged["feed"]["case_details"]["case-xyz"]
    assert detail.get("projection_router")
    assert detail.get("projection_blocks")


def test_apply_projection_route_overlays_skips_desk_tasks_when_suppressed() -> None:
    base = build_operational_feed_snapshot(
        cockpit={"desk": {"items": []}, "cases": {"items": []}},
        day=None,
        tasks=None,
        snapshot_id="base-feed-suppressed",
    )
    base["feed"]["cases"] = [
        {
            "case_id": "case-spam",
            "title": "Business Manager partner request",
            "status": "open",
            "badges": {"needs_operator_review": True},
        }
    ]
    routes = route_projection_envelope(
        {
            "schema_version": "projection_envelope.v1",
            "case_id": "case-spam",
            "desk_cards": [{"card_type": "router_card", "title": "Spam desk", "summary": "Should not show"}],
            "task_candidates": [{"task_id": "spam-task", "title": "Spam task", "read_only": True}],
            "case_detail_blocks": [{"block_type": "essence", "content": "detail ok"}],
            "gap_blocks": [],
            "conflict_blocks": [],
            "desk_tasks_suppressed": True,
            "desk_suppression_reason": "non_business_noise",
        }
    )
    merged = apply_projection_route_overlays(base, {"case-spam": routes})
    assert len(merged["feed"]["cases"]) == 1
    assert merged["feed"]["cases"][0]["case_id"] == "case-spam"
    assert merged["feed"]["desk"] == []
    assert (merged["feed"].get("action_items") or merged["feed"].get("tasks") or []) == []
    assert merged["feed"]["cases"][0].get("desk_tasks_suppressed") is True


def test_accumulate_overlay_from_reconcile_stage_outputs() -> None:
    envelope = {
        "schema_version": "projection_envelope.v1",
        "case_id": "case-bridge",
        "desk_cards": [],
        "task_candidates": [],
        "case_detail_blocks": [],
        "gap_blocks": [],
        "conflict_blocks": [],
    }
    routes = route_projection_envelope(envelope)
    reconcile = SimpleNamespace(
        case_id="case-bridge",
        processing_state="reconciled",
        projection_refresh_decision=SimpleNamespace(should_refresh=True),
        stage_outputs={"operator_projection_snapshot": {"daszek_routes": routes, "projection_envelope": envelope}},
    )
    run_state: dict = {"projection_route_overlays": {}}
    accumulate_projection_route_overlay(run_state, reconcile)
    cid, extracted = case_id_and_routes_from_reconcile(reconcile)
    assert cid == "case-bridge"
    assert extracted is not None
    assert "case-bridge" in run_state["projection_route_overlays"]


def test_apply_desk_composition_visibility_desk_zone() -> None:
    feed_case = {"case_id": "c1", "badges": {}}
    apply_desk_composition_visibility(
        feed_case,
        {"should_surface": True, "surface_zone": "desk"},
    )
    assert surface_zone_from_desk_composition({"surface_zone": "desk"}) == "desk"
    assert feed_case["badges"]["needs_operator_review"] is True
