"""Agent fact persistence uses full mailbox_memory_facts rows."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import signal_extractor
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tools.handlers import extract_facts_from_text


class _FakeStore:
    def __init__(self) -> None:
        self.appended: list[dict] = []

    def append_fact_rows(self, rows: list[dict]) -> None:
        self.appended.extend(rows)


def _ctx(**signal_payload: object) -> ToolExecutionContext:
    store = _FakeStore()
    snap = build_initial_snapshot(case_id="", engagement_id="eng_pf", trace_id="sig_pf")
    return ToolExecutionContext(
        snapshot=snap,
        settings=object(),
        mailbox_store=store,
        signal_payload={
            "subject": "Wycena 120 m2 Radlin",
            "body_text": "proszę o ofertę pompy",
            "message_id": "msg_pf",
            **signal_payload,
        },
    )


def test_persist_facts_includes_required_columns(monkeypatch) -> None:
    monkeypatch.setattr(
        signal_extractor,
        "run_signal_extraction",
        lambda **_: {"hvac_intent": "quote", "heated_area_m2": 120, "raw_geographic_signal": "Radlin"},
    )
    ctx = _ctx()
    extract_facts_from_text(None, ctx)
    assert len(ctx.mailbox_store.appended) == 2  # type: ignore[union-attr]
    row = ctx.mailbox_store.appended[0]  # type: ignore[union-attr]
    for key in (
        "fact_id",
        "confidence",
        "source_type",
        "source_ref",
        "status",
        "raw_value",
        "document_id",
    ):
        assert key in row
    assert row["source_type"] == "agent_extraction"
    assert row["fact_key"] == "heated_area_m2"


def test_llm_exhaustion_returns_blocking_gap(monkeypatch) -> None:
    monkeypatch.setattr(
        signal_extractor,
        "run_signal_extraction",
        lambda **_: {"parse_status": "extraction_failed", "error_reason": "rate_limit"},
    )
    ctx = _ctx(business_area="operations")
    result = extract_facts_from_text(None, ctx)
    assert result.status == "error"
    gaps = result.snapshot_delta.get("gaps", [])
    assert any(g.get("field") == "llm_extraction" and g.get("severity") == "blocking" for g in gaps)
