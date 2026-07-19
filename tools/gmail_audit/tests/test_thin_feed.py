from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.store import build_initial_snapshot
from daszek_engagement_feed import snapshot_to_feed_case


def test_operator_essence_pl_empty_without_trace() -> None:
    snap = build_initial_snapshot(case_id="c1", engagement_id="e1", trace_id="t1")
    row = snapshot_to_feed_case(snap)
    assert row["operator_essence_pl"] == ""
    assert row["summary_pl"] == ""
    assert row["hitl_pending"] is False


def test_hitl_pending_from_gate() -> None:
    from agent_runtime.snapshot_delta import apply_snapshot_delta

    snap = build_initial_snapshot(case_id="c2", engagement_id="e2", trace_id="t2")
    patched = apply_snapshot_delta(
        snap,
        {"hitl_gate": {"required": True, "reason": "draft_ready_for_approval"}},
    )
    row = snapshot_to_feed_case(patched)
    assert row["hitl_pending"] is True
