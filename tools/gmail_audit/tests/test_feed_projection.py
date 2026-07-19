from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.feed_projection import build_v2_projection_from_engagement
from agent_runtime.store import build_initial_snapshot
from signal_contract import build_canonical_signal


def test_feed_projection_validates_v2_contract() -> None:
    engagement = build_initial_snapshot(
        case_id="case_fp",
        engagement_id="eng_fp",
        trace_id="sig_fp",
    )
    signal = build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"message_id": "m1", "thread_id": "t1"},
        observed_at="2026-06-04T12:00:00+02:00",
        effective_at="2026-06-04T12:00:00+02:00",
        thread_key_hint="t1",
        business_lane="operations",
        signal_summary_pl="Test",
        payload={"case_id": "case_fp"},
        artifacts={},
        revision_marker="1",
        created_by_runtime="test",
    )
    v2 = build_v2_projection_from_engagement(
        engagement,
        signal=signal,
        intake_output={"decision": {"action": "review"}, "message": {"message_id": "m1"}},
        case_key="t1",
    )
    assert v2["signal_projection"]["signal_id"] == signal.signal_id
    assert v2["case_patch"]["case_id"] == "case_fp"
    assert "agent_runtime" in v2["signal_projection"]
