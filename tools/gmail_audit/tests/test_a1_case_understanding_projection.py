"""A1: wire existing case_intelligence_result/understanding_output into the
ACTIVE operator feed path (daszek_engagement_feed, EngagementSnapshotV2),
which today builds every card's essence/next-step purely from the agent's own
tool-call trace and never references case_intelligence_result/understanding_output
at all (confirmed by as-built discovery — the two are structurally unconnected).

Invariant this closes (operator-approved scope, 2026-07-16):
For every operator-facing case view there exists exactly one current source of
business meaning: the freshest, correctly-correlated Understanding. The active
projection may transport/format it but must never present stale or
uncorrelated Understanding as current, and must never let an agent tool-trace
stub masquerade as real Understanding. Absent fresh correlated Understanding,
the system falls back honestly (existing agent-trace essence), never silently.
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.agent_reconcile import build_case_understanding_projection
from agent_runtime.graph import _ground_current_signal
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import build_initial_snapshot
from daszek_engagement_feed.case import (
    operator_essence_pl_from_snapshot,
    recommended_next_step_pl_from_snapshot,
    why_on_desk_pl_from_snapshot,
)
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2, ReasoningTraceItem


def _understanding_output(*, source_signal_id: str, essence: str = "Klient potwierdzil wizje lokalna na wtorek.") -> dict:
    return {
        "source_signal_id": source_signal_id,
        "created_at": "2026-07-16T10:00:00Z",
        "operator_explanation": {
            "essence_pl": essence,
            "why_pl": "Klient odpowiedzial na oferte i podal termin.",
        },
        "situation_summary_pl": essence,
        "thread_delta": {"operator_visible_delta_summary": "Klient po raz pierwszy podal konkretny termin."},
        "missing_critical_fields": ["numer telefonu"],
        "risks": [{"risk_type": "operational_risk", "severity": "medium", "summary_pl": "Termin kolokuje sie z innym zleceniem."}],
        "next_best_action_recommendation": {
            "title_pl": "Potwierdz wizje lokalna na wtorek 10:00.",
            "kind": "recommendation",
            "note": "Recommendation only; not approved until PolicyDecision and operator approval.",
        },
    }


# --- 1. schema: EngagementSnapshotV2 carries an optional, backward-compatible
#    case_understanding projection field --------------------------------------


def test_snapshot_schema_defaults_case_understanding_to_none() -> None:
    """Old persisted snapshots (missing the new key entirely) must still validate."""
    snap = build_initial_snapshot(case_id="case_1", engagement_id="eng_1", trace_id="sig_1")
    assert snap.case_understanding is None
    # round-trip through model_dump/model_validate exactly like the Postgres store does
    reloaded = EngagementSnapshotV2.model_validate(snap.model_dump(mode="python"))
    assert reloaded.case_understanding is None


def test_snapshot_schema_accepts_case_understanding_projection() -> None:
    snap = build_initial_snapshot(case_id="case_1", engagement_id="eng_1", trace_id="sig_1")
    updated = apply_snapshot_delta(
        snap,
        {
            "case_understanding": {
                "source_signal_id": "msg_1",
                "generated_at": "2026-07-16T10:00:00Z",
                "essence_pl": "Essence",
                "what_changed_pl": "",
                "why_pl": "",
                "missing_critical_fields": [],
                "risks": [],
                "recommended_next_step_pl": "",
            }
        },
    )
    assert updated.case_understanding is not None
    assert updated.case_understanding.essence_pl == "Essence"


# --- 2. build_case_understanding_projection: correlation + honest-empty rules ---


def test_projection_builder_returns_none_when_message_id_mismatched() -> None:
    """Understanding computed for a DIFFERENT message than the one being processed
    this turn must never be projected as if it were current (stale/mismatched)."""
    intel = {"understanding_output": _understanding_output(source_signal_id="msg_OLD")}
    out = build_case_understanding_projection(intel, message_id="msg_CURRENT")
    assert out is None


def test_projection_builder_returns_none_when_no_understanding_output() -> None:
    out = build_case_understanding_projection({}, message_id="msg_1")
    assert out is None


def test_projection_builder_returns_none_when_essence_empty() -> None:
    intel = {"understanding_output": _understanding_output(source_signal_id="msg_1", essence="")}
    out = build_case_understanding_projection(intel, message_id="msg_1")
    assert out is None


def test_projection_builder_extracts_compact_fields_when_correlated() -> None:
    intel = {"understanding_output": _understanding_output(source_signal_id="msg_1")}
    out = build_case_understanding_projection(intel, message_id="msg_1")
    assert out is not None
    assert out["source_signal_id"] == "msg_1"
    assert "wizje lokalna" in out["essence_pl"]
    assert out["why_pl"].startswith("Klient odpowiedzial")
    assert out["what_changed_pl"] == "Klient po raz pierwszy podal konkretny termin."
    assert out["missing_critical_fields"] == ["numer telefonu"]
    assert out["risks"][0]["summary_pl"].startswith("Termin kolokuje")
    assert "Potwierdz wizje lokalna" in out["recommended_next_step_pl"]


# --- 3. _ground_current_signal: sets fresh projection, clears stale one --------


def test_ground_current_signal_sets_case_understanding_when_projection_present() -> None:
    snapshot = build_initial_snapshot(case_id="case_g", engagement_id="eng_g", trace_id="sig_g")
    projection = build_case_understanding_projection(
        {"understanding_output": _understanding_output(source_signal_id="msg_1")},
        message_id="msg_1",
    )
    grounded = _ground_current_signal(
        snapshot,
        {"subject": "Re: Oferta", "snippet": "...", "case_understanding_projection": projection},
    )
    assert grounded.case_understanding is not None
    assert grounded.case_understanding.essence_pl == projection["essence_pl"]


def test_ground_current_signal_clears_stale_case_understanding_when_absent_this_turn() -> None:
    """A prior turn's Understanding must not survive as if current once a new
    turn runs without a fresh, correlated projection for it."""
    snapshot = build_initial_snapshot(case_id="case_g2", engagement_id="eng_g2", trace_id="sig_g2")
    snapshot = apply_snapshot_delta(
        snapshot,
        {
            "case_understanding": {
                "source_signal_id": "msg_OLD",
                "generated_at": "2026-07-15T10:00:00Z",
                "essence_pl": "Old essence from a previous turn",
                "what_changed_pl": "",
                "why_pl": "",
                "missing_critical_fields": [],
                "risks": [],
                "recommended_next_step_pl": "",
            }
        },
    )
    grounded = _ground_current_signal(
        snapshot,
        {"subject": "Re: Oferta", "snippet": "kolejna wiadomosc"},
    )
    assert grounded.case_understanding is None


# --- 4. daszek_engagement_feed.case: active feed prefers Understanding,
#    falls back honestly when it is absent -------------------------------------


def _snapshot_with_case_understanding(essence: str, next_step: str = "", why: str = "") -> EngagementSnapshotV2:
    snap = build_initial_snapshot(case_id="case_f", engagement_id="eng_f", trace_id="sig_f")
    snap = apply_snapshot_delta(
        snap,
        {
            "agent_memory": {
                "reasoning_trace": [{"turn": 0, "summary_pl": "Agent tool-trace note (not business understanding)"}]
            },
            "case_understanding": {
                "source_signal_id": "msg_1",
                "generated_at": "2026-07-16T10:00:00Z",
                "essence_pl": essence,
                "what_changed_pl": "",
                "why_pl": why,
                "missing_critical_fields": [],
                "risks": [],
                "recommended_next_step_pl": next_step,
            },
        },
    )
    return snap


def test_active_feed_essence_prefers_fresh_understanding_over_tool_trace() -> None:
    snap = _snapshot_with_case_understanding("Klient potwierdzil termin wizji lokalnej.")
    essence = operator_essence_pl_from_snapshot(snap)
    assert essence == "Klient potwierdzil termin wizji lokalnej."
    assert "tool-trace" not in essence


def test_active_feed_essence_falls_back_to_tool_trace_when_no_understanding() -> None:
    snap = build_initial_snapshot(case_id="case_f2", engagement_id="eng_f2", trace_id="sig_f2")
    snap = apply_snapshot_delta(
        snap,
        {"agent_memory": {"reasoning_trace": [{"turn": 0, "summary_pl": "Fallback tool-trace note"}]}},
    )
    assert snap.case_understanding is None
    essence = operator_essence_pl_from_snapshot(snap)
    assert essence == "Fallback tool-trace note"


def test_active_feed_next_step_and_why_prefer_understanding_when_present() -> None:
    snap = _snapshot_with_case_understanding(
        "Essence",
        next_step="Potwierdz wizje lokalna na wtorek 10:00.",
        why="Klient po raz pierwszy podal konkretny termin.",
    )
    assert recommended_next_step_pl_from_snapshot(snap) == "Potwierdz wizje lokalna na wtorek 10:00."
    assert why_on_desk_pl_from_snapshot(snap) == "Klient po raz pierwszy podal konkretny termin."


def test_active_feed_why_on_desk_honest_empty_when_no_understanding() -> None:
    snap = build_initial_snapshot(case_id="case_f3", engagement_id="eng_f3", trace_id="sig_f3")
    assert why_on_desk_pl_from_snapshot(snap) == ""


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
