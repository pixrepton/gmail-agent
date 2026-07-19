"""PR-F: CEL Radlin Digital Twin DoD — end-to-end agent reconcile without shared downstream."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.agent_reconcile import agent_runtime_reconcile_active
from agent_runtime.constitution import load_constitution
from agent_runtime.digital_twin_dod import assert_digital_twin_dod, evaluate_digital_twin_dod
from agent_runtime.graph import AgentGraphEngine
from agent_runtime.planner import MockSequencePlanner
from agent_runtime.run import AgentRunResult
from agent_runtime.store import InMemoryOperatorEngagementStore, build_initial_snapshot
from agent_runtime.jobs import InMemoryAgentJobStore
from agent_runtime.tools_registry import MockToolRegistry
from agent_runtime.turn_journal import InMemoryAgentTurnJournal
from correlation_registry.service import CorrelationRegistryService
from correlation_registry.store import InMemoryCorrelationRegistryStore
from daszek_engagement_feed import build_operational_feed_from_engagement_store
from llm_contracts.engagement_snapshot_v2 import OperationalStatus
from mailbox_memory_store import InMemoryMailboxMemoryStore
from signal_contract import build_canonical_signal
from signal_journal import SignalJournal
from signal_reconciler import SignalRuntimeContext, _reconcile_gmail_signal


@pytest.fixture(autouse=True)
def _in_memory_operator_engagement_store(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryOperatorEngagementStore()
    monkeypatch.setattr(
        "agent_runtime.agent_reconcile.build_operator_engagement_store",
        lambda settings, *, allow_in_memory=False: store,
    )


@pytest.fixture(autouse=True)
def _in_memory_agent_job_store(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryAgentJobStore()
    monkeypatch.setattr(
        "agent_runtime.agent_reconcile.build_agent_job_store",
        lambda settings, *, allow_in_memory=False: store,
    )


def _context() -> tuple[InMemoryMailboxMemoryStore, SignalRuntimeContext]:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    journal = SignalJournal(store=store)
    settings = type("S", (), {"mailbox_memory_database_url": "", "groq_model": "test"})()
    ctx = SignalRuntimeContext(
        settings=settings,
        journal=journal,
        store=store,
        run_state={"run_id": "test-radlin-dod"},
    )
    return store, ctx


def _radlin_signal() -> object:
    return build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"mailbox": "ops@example.com", "message_id": "msg-radlin-cel", "thread_id": "thr-radlin"},
        observed_at="2026-06-01T09:22:27+02:00",
        effective_at="2026-06-01T09:22:27+02:00",
        thread_key_hint="thr-radlin",
        business_lane="operations",
        signal_summary_pl="Zapytanie 128 m2 Radlin 44-310",
        payload={
            "case_id": "case_radlin_cel",
            "snapshot": {
                "source_message": {
                    "message_id": "msg-radlin-cel",
                    "thread_id": "thr-radlin",
                    "subject": "Dom 128 mkw Radlin (44-310)",
                    "from_email": "dozorca@cieplo.app",
                    "body_text": "Lokalizacja: Radlin 44-310. Powierzchnia 128 m2.",
                }
            },
            "intake_result_final": {
                "decision": {"action": "review"},
                "message": {
                    "message_id": "msg-radlin-cel",
                    "subject": "Dom 128 mkw Radlin (44-310)",
                },
            },
            "preclassification_result": {"lane": "intake_llm"},
            "lane_stage_plan": {"run_case_linking": True},
        },
        artifacts={"source": "test"},
        revision_marker="1",
        created_by_runtime="test",
    )


@pytest.mark.parametrize("mode", ["prep"])
def test_cel_radlin_dod_after_agent_reconcile(mode: str) -> None:
    store, context = _context()
    signal = _radlin_signal()
    registry = CorrelationRegistryService(InMemoryCorrelationRegistryStore())
    registry.bootstrap()
    registry.sync_mailbox_case(
        case_id="case_radlin_cel",
        customer_email="dozorca@cieplo.app",
        message_id="msg-radlin-cel",
    )
    constitution = load_constitution()
    engine = AgentGraphEngine(
        planner=MockSequencePlanner(["extract_facts_from_text", "report_gaps_and_stop"]),
        constitution=constitution,
        tool_registry=MockToolRegistry(),
        turn_journal=InMemoryAgentTurnJournal(),
    )
    initial = build_initial_snapshot(
        case_id="case_radlin_cel",
        engagement_id="eng_radlin_cel",
        trace_id=signal.signal_id,
    )

    with patch.dict(
        os.environ,
        {
            "AGENT_RUNTIME_ENABLED": "1",
            "AGENT_RUNTIME_MODE": mode,
            "AGENT_OPENAI_API_KEY": "sk-test",
            "DASZEK_FEED_SOURCE": "",
        },
        clear=False,
    ):
        assert agent_runtime_reconcile_active()
        with patch(
            "agent_runtime.agent_reconcile.build_registry_for_reconcile",
            return_value=registry,
        ), patch(
            "agent_runtime.agent_reconcile.execute_agent_run",
        ) as mock_run, patch(
            "intake_shared_downstream.run_shared_downstream_stages",
        ) as mock_downstream:
            graph_result = engine.run(initial)
            mock_run.return_value = AgentRunResult(
                snapshot=graph_result.snapshot,
                graph=graph_result,
                version=2,
            )
            result = _reconcile_gmail_signal(
                signal,
                runtime_context=context,
                dry_run=False,
                entity_link_dict={"case_id": "case_radlin_cel"},
            )

    mock_downstream.assert_not_called()
    snap_data = result.stage_outputs.get("agent_engagement_snapshot") or {}
    from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2

    snapshot = EngagementSnapshotV2.model_validate(snap_data)
    op_store = InMemoryOperatorEngagementStore()
    op_store.init_snapshot_from_signal(
        signal={"signal_id": signal.signal_id},
        case_id="case_radlin_cel",
        engagement_id="eng_radlin_cel",
    )
    op_store._rows["eng_radlin_cel"]["snapshot_data"] = snapshot.model_dump(mode="python")
    journal = InMemoryAgentTurnJournal()
    for turn in mock_run.return_value.graph.turns:
        from agent_runtime.tool_result import ToolCallPlan, ToolResult

        journal.append_turn(
            engagement_id="eng_radlin_cel",
            snapshot_version=snapshot.version,
            trace_id=signal.signal_id,
            plan=ToolCallPlan(tool_name=turn.tool_name),
            result=ToolResult(status=turn.tool_status, turn_summary_pl=turn.turn_summary_pl),
        )
    feed = build_operational_feed_from_engagement_store(
        op_store,
        case_ids=["case_radlin_cel"],
        journal=journal,
    )

    report = assert_digital_twin_dod(
        snapshot,
        reconcile_result=result,
        feed_envelope=feed,
        require_feed=True,
    )
    assert report.ok
    assert report.checks["heated_area_128_m2"]
    assert report.checks["city_radlin"]
    assert report.checks["feed_engagement_snapshot_v2"]
    assert report.checks["v2_projection_agent_runtime"]
    assert report.checks["feed_case_visible"]
    assert report.checks["feed_agent_turns_present"]


def test_evaluate_dod_fails_without_radlin_facts() -> None:
    snap = build_initial_snapshot(case_id="c1", engagement_id="e1", trace_id="s1")
    report = evaluate_digital_twin_dod(snap)
    assert not report.ok
    assert "heated_area_128_m2" in report.checks
    assert not report.checks["heated_area_128_m2"]
