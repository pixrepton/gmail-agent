from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution import load_constitution
from agent_runtime.graph import AgentGraphEngine
from agent_runtime.planner import MockSequencePlanner
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import InMemoryOperatorEngagementStore, build_initial_snapshot
from agent_runtime.tools_registry import MockToolRegistry


def test_second_reconcile_does_not_re_add_area_gap() -> None:
    store = InMemoryOperatorEngagementStore()
    snap = build_initial_snapshot(case_id="case_epi", engagement_id="eng_epi", trace_id="sig1")
    snap = apply_snapshot_delta(
        snap,
        {"hvac_profile": {"heated_area_m2": 128, "location": {"city": "Radlin"}}},
    )
    store.insert_snapshot(snap)
    loaded = store.load_snapshot("eng_epi")
    assert loaded is not None
    engine = AgentGraphEngine(
        planner=MockSequencePlanner(["extract_facts_from_text", "report_gaps_and_stop"]),
        constitution=load_constitution(),
        tool_registry=MockToolRegistry(),
    )
    result = engine.run(loaded)
    gap_fields = [g.field for g in result.snapshot.gaps]
    assert "heated_area_m2" not in gap_fields
    assert result.snapshot.hvac_profile.heated_area_m2 == 128
