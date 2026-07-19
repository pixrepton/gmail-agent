from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution import load_constitution, load_live
from agent_runtime.engagement_resolver import (
    EngagementResolution,
    extract_case_id_from_signal,
    resolve_engagement_for_case,
)
from agent_runtime.graph import AgentGraphEngine
from agent_runtime.planner import MockSequencePlanner
from agent_runtime.semantic_memory import fetch_constitution_rag_chunks
from agent_runtime.signal_engagement import patch_signal_engagement
from agent_runtime.store import InMemoryOperatorEngagementStore, build_initial_snapshot
from agent_runtime.tools_registry import MockToolRegistry
from agent_runtime.turn_journal import InMemoryAgentTurnJournal
from correlation_registry.service import CorrelationRegistryService
from correlation_registry.store import InMemoryCorrelationRegistryStore
from llm_contracts.engagement_snapshot_v2 import (
    EngagementSnapshotV2,
    engagement_snapshot_v2_json_schema,
)
from mailbox_memory_store import InMemoryMailboxMemoryStore

SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "contracts" / "engagement_snapshot_v2.schema.json"
)


def _registry() -> CorrelationRegistryService:
    store = InMemoryCorrelationRegistryStore()
    store.bootstrap()
    return CorrelationRegistryService(store)


def test_schema_json_file_matches_pydantic() -> None:
    assert SCHEMA_PATH.is_file()
    on_disk = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    live = engagement_snapshot_v2_json_schema()
    assert on_disk["title"] == live["title"]
    assert on_disk["$defs"].keys() == live["$defs"].keys()


def test_operational_status_blocking_field() -> None:
    snap = build_initial_snapshot(case_id="c", engagement_id="e", trace_id="t")
    assert snap.operational_status.blocking is False
    updated = EngagementSnapshotV2.model_validate(
        {
            **snap.model_dump(),
            "operational_status": {
                "code": "pending_operator",
                "steps_remaining": 0,
                "blocking": True,
            },
        }
    )
    assert updated.operational_status.blocking is True


def test_load_snapshot_by_case_id() -> None:
    store = InMemoryOperatorEngagementStore()
    store.init_snapshot_from_signal(
        signal={"signal_id": "sig_case_lookup"},
        case_id="case_lookup",
        engagement_id="eng_lookup",
    )
    loaded = store.load_snapshot_by_case_id("case_lookup")
    assert loaded is not None
    assert loaded.engagement_id == "eng_lookup"


def test_resolve_engagement_creates_via_registry() -> None:
    registry = _registry()
    resolution = resolve_engagement_for_case(
        "case_resolver_01",
        registry=registry,
        customer_email="klient@example.com",
        message_id="msg_01",
    )
    assert isinstance(resolution, EngagementResolution)
    assert resolution.created is True
    assert resolution.engagement_id
    again = resolve_engagement_for_case("case_resolver_01", registry=registry)
    assert again.created is False
    assert again.engagement_id == resolution.engagement_id


def test_extract_case_id_from_signal() -> None:
    assert extract_case_id_from_signal({"case_id": "case_x"}) == "case_x"
    assert extract_case_id_from_signal(
        {"payload_json": {"mailbox_case_id": "case_y"}}
    ) == "case_y"


def test_turn_journal_append_and_list() -> None:
    journal = InMemoryAgentTurnJournal()
    from agent_runtime.tool_result import ToolCallPlan, ToolResult

    journal.append_turn(
        engagement_id="eng_j",
        snapshot_version=2,
        trace_id="sig_j",
        plan=ToolCallPlan(tool_name="extract_facts_from_text", arguments={"text": "128 m2 Radlin"}),
        result=ToolResult(status="ok", turn_summary_pl="Wyciągnięto fakty", tokens_used=42),
    )
    rows = journal.list_turns("eng_j")
    assert len(rows) == 1
    assert rows[0]["tokens_used"] == 42
    assert "Radlin" in rows[0]["tool_args_redacted"]["text"]


def test_graph_persists_turns_to_journal() -> None:
    store = InMemoryOperatorEngagementStore()
    journal = InMemoryAgentTurnJournal()
    snapshot = store.init_snapshot_from_signal(
        signal={"signal_id": "sig_journal"},
        case_id="case_journal",
        engagement_id="eng_journal",
    )
    engine = AgentGraphEngine(
        planner=MockSequencePlanner(["extract_facts_from_text", "report_gaps_and_stop"]),
        constitution=load_constitution(),
        tool_registry=MockToolRegistry(),
        turn_journal=journal,
    )
    engine.run(snapshot)
    assert len(journal.list_turns("eng_journal")) == 2


def test_load_live_constitution_without_rag() -> None:
    base = load_constitution()
    live = load_live(rag_enabled=False)
    assert live.tool_allowlist == base.tool_allowlist


def test_fetch_constitution_rag_degrades_empty() -> None:
    assert fetch_constitution_rag_chunks("CP2025") == []


def test_patch_signal_engagement_in_memory() -> None:
    mem = InMemoryMailboxMemoryStore()
    mem.bootstrap()
    mem.append_signal(
        {
            "signal_id": "sig_patch",
            "idempotency_key": "idem_patch",
            "engagement_id": "",
        }
    )
    assert patch_signal_engagement(mem, signal_id="sig_patch", engagement_id="eng_patch")
    row = mem.fetch_signal("sig_patch")
    assert row is not None
    assert row["engagement_id"] == "eng_patch"


def test_illegal_blocking_type_raises() -> None:
    snap = build_initial_snapshot(case_id="c", engagement_id="e", trace_id="t")
    payload = snap.model_dump()
    payload["operational_status"]["blocking"] = {"invalid": True}
    with pytest.raises(ValidationError):
        EngagementSnapshotV2.model_validate(payload)
