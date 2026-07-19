"""Engagement feed operator labels."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.store import build_initial_snapshot
from daszek_engagement_feed.case import snapshot_to_feed_case
from daszek_engagement_feed.labels import primary_next_action_pl


def test_feed_case_maps_case_kind_to_family_label() -> None:
    snap = build_initial_snapshot(case_id="case_x", engagement_id="eng_x", trace_id="sig_x")
    snap = snap.model_copy(update={"case_kind": "wycena_oferta"})
    row = snapshot_to_feed_case(snap, subject="Wycena klimatyzacji")
    assert row["title"] == "Wycena klimatyzacji"
    assert row["family_label"] == "Wycena / oferta"
    assert row["family"] == "lead_opportunity"
    assert row["status_label"] == "Nowe zapytanie"


def test_primary_next_action_for_hitl_draft() -> None:
    snap = build_initial_snapshot(case_id="case_x", engagement_id="eng_x", trace_id="sig_x")
    from llm_contracts.engagement_snapshot_v2 import ActionItem, HitlGate

    snap = snap.model_copy(
        update={
            "hitl_gate": HitlGate(required=True, reason="draft_ready"),
            "actions": [
                ActionItem(id="draft_reply", enabled=True, payload_pl="Draft", disabled_reason_pl=None),
            ],
        }
    )
    assert "draft" in primary_next_action_pl(snap).lower()
