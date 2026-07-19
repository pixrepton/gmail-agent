from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = TOOL_DIR.parents[2]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from build_s2_identity_proof import (  # noqa: E402
    ProofRun,
    assert_cards_do_not_embed_trace,
    build_identity_record,
    matched_cards_for_signal,
    select_agent_run_trace_id,
)


def _load_browser_module():
    path = ROOT_DIR / "scripts" / "row4a_browser_proof.py"
    spec = importlib.util.spec_from_file_location("row4a_browser_proof", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _proof_run() -> ProofRun:
    now = datetime(2026, 7, 12, 18, 55, tzinfo=timezone.utc)
    return ProofRun(
        proof_dir=Path(tempfile.mkdtemp()),
        message_id="msg-1",
        signal_id="sig-1",
        engagement_id="eng-1",
        case_id="",
        snapshot_id="snap-1",
        title="Title",
        run_id="run-1",
        started_at=now,
        finished_at=now,
        live_request_url="http://127.0.0.1/live",
        live_request_status=200,
        latest_snapshot_matches_handoff=True,
        latest_source_run_id="run-1",
        matched_card_ids=["desk-eng-1"],
        matched_card_count=1,
        latest_source_signal_ids=["sig-1"],
        latest_source_message_id="msg-1",
    )


def test_browser_expected_identity_keeps_missing_trace_null() -> None:
    module = _load_browser_module()
    expected = module.expected_identity_from_handoff_item(
        {
            "message_id": "msg-1",
            "signal_id": "sig-1",
            "engagement_id": "eng-1",
            "snapshot_id": "snap-1",
            "title": "Title",
        }
    )
    assert expected["signal_id"] == "sig-1"
    assert expected["trace_id"] is None
    assert expected["trace_id_source"] == "missing"


def test_generator_source_has_no_old_hardcoded_trace_literals_or_timestamps() -> None:
    source = (TOOL_DIR / "build_s2_identity_proof.py").read_text(encoding="utf-8")
    assert "sig_173e3cbbaf284ac6" not in source
    assert "sig_d46cf114ad284ade" not in source
    assert "2026-07-12T18:52:38" not in source
    assert "2026-07-12T18:55:53" not in source


def test_select_agent_run_trace_id_picks_single_event_from_proof_window() -> None:
    proof = _proof_run()
    items = [
        {
            "event_type": "agent.run.started",
            "engagement_id": "eng-1",
            "trace_id": "agent_a",
            "occurred_at": "2026-07-12T18:55:00+00:00",
        },
        {
            "event_type": "agent.run.started",
            "engagement_id": "eng-1",
            "trace_id": "agent_b",
            "occurred_at": "2026-07-12T18:40:00+00:00",
        },
    ]
    trace_id, source = select_agent_run_trace_id(items, proof)
    assert trace_id == "agent_a"
    assert "proof window" in source


def test_select_agent_run_trace_id_fails_when_ambiguous() -> None:
    proof = _proof_run()
    items = [
        {
            "event_type": "agent.run.started",
            "engagement_id": "eng-1",
            "trace_id": "agent_a",
            "occurred_at": "2026-07-12T18:55:00+00:00",
        },
        {
            "event_type": "agent.run.started",
            "engagement_id": "eng-1",
            "trace_id": "agent_b",
            "occurred_at": "2026-07-12T18:55:30+00:00",
        },
    ]
    with pytest.raises(RuntimeError, match="expected exactly one"):
        select_agent_run_trace_id(items, proof)


def test_build_identity_record_preserves_distinct_trace_fields() -> None:
    record = build_identity_record(
        label="A",
        proof_run=_proof_run(),
        signal_runtime_trace_id="sig-runtime-a",
        signal_runtime_trace_source="db",
        agent_run_trace_id="agent-a",
        agent_run_trace_source="os_events",
    )
    assert record["signal_runtime_trace_id"] == "sig-runtime-a"
    assert record["agent_run_trace_id"] == "agent-a"
    assert record["signal_runtime_trace_id"] != record["agent_run_trace_id"]


def test_matched_cards_for_signal_returns_single_card_and_excludes_trace_ids() -> None:
    cards = [
        {
            "note_id": "desk-eng-1",
            "engagement_id": "eng-1",
            "source_signal_ids": ["sig-1"],
            "source_message_id": "msg-1",
        },
        {
            "note_id": "desk-eng-2",
            "engagement_id": "eng-2",
            "source_signal_ids": ["sig-2"],
            "source_message_id": "msg-2",
        },
    ]
    matched = matched_cards_for_signal(cards, "sig-1")
    assert [row["engagement_id"] for row in matched] == ["eng-1"]
    assert_cards_do_not_embed_trace(matched, ["trace-1", "agent-1"])


def test_assert_cards_do_not_embed_trace_fails_when_trace_leaks_into_source_signal_ids() -> None:
    cards = [{"source_signal_ids": ["sig-1", "trace-1"]}]
    with pytest.raises(RuntimeError, match="technical trace_id"):
        assert_cards_do_not_embed_trace(cards, ["trace-1"])
