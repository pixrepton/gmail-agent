from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.agent_reconcile import (
    agent_runtime_reconcile_active,
    legacy_downstream_reconcile_active,
    resolve_case_id_for_agent,
    run_agent_reconcile_staging,
)
from agent_runtime.jobs import InMemoryAgentJobStore
from agent_runtime.planner import MockSequencePlanner
from agent_runtime.run import AgentRunResult
from agent_runtime.store import InMemoryOperatorEngagementStore, build_initial_snapshot
from agent_runtime.graph import AgentGraphRunResult
from correlation_registry.store import InMemoryCorrelationRegistryStore
from correlation_registry.service import CorrelationRegistryService
from llm_contracts.engagement_snapshot_v2 import OperationalStatus
from mailbox_memory_store import InMemoryMailboxMemoryStore
from signal_contract import build_canonical_signal
from signal_journal import SignalJournal
from signal_contract import build_canonical_signal
from signal_reconciler import SignalRuntimeContext, _reconcile_drive_signal, _reconcile_gmail_signal


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


def _gmail_signal(*, case_id: str = "case_agent_d") -> object:
    return build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"mailbox": "ops@example.com", "message_id": "msg-agent-d", "thread_id": "thr-agent-d"},
        observed_at="2026-06-04T10:00:00+02:00",
        effective_at="2026-06-04T10:00:00+02:00",
        thread_key_hint="thr-agent-d",
        business_lane="operations",
        signal_summary_pl="Zapytanie 128 m2 Radlin",
        payload={
            "case_id": case_id,
            "snapshot": {
                "source_message": {
                    "message_id": "msg-agent-d",
                    "thread_id": "thr-agent-d",
                    "subject": "Pompa ciepła Radlin 128m2",
                    "from_email": "klient@example.com",
                }
            },
            "intake_result_final": {
                "decision": {"action": "review"},
                "message": {
                    "message_id": "msg-agent-d",
                    "subject": "Pompa ciepła Radlin 128m2",
                    "sender": "klient@example.com",
                },
            },
            "preclassification_result": {"lane": "intake_llm"},
            "lane_stage_plan": {"run_case_linking": True},
        },
        artifacts={"source": "test"},
        revision_marker="1",
        created_by_runtime="test",
    )


def _context() -> tuple[InMemoryMailboxMemoryStore, SignalRuntimeContext]:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    journal = SignalJournal(store=store)
    settings = type(
        "S",
        (),
        {
            "mailbox_memory_database_url": "",
            "groq_model": "test",
        },
    )()
    ctx = SignalRuntimeContext(
        settings=settings,
        journal=journal,
        store=store,
        run_state={"run_id": "test-agent-d"},
    )
    return store, ctx


def test_resolve_case_id_from_entity_link() -> None:
    signal = build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"message_id": "m"},
        observed_at="2026-06-04T10:00:00+02:00",
        effective_at="2026-06-04T10:00:00+02:00",
        thread_key_hint="t",
        business_lane="operations",
        signal_summary_pl="T",
        payload={"snapshot": {}},
        artifacts={},
        revision_marker="1",
        created_by_runtime="test",
    )
    assert resolve_case_id_for_agent(signal, {"case_id": "case_from_link"}) == "case_from_link"


def _drive_signal(*, case_id: str = "case_agent_drive", file_id: str = "drv_agent_d") -> object:
    return build_canonical_signal(
        signal_kind="drive_document_added",
        source_kind="drive",
        source_ref={
            "file_id": file_id,
            "change_id": "chg-agent-d",
            "revision_id": "rev-agent-d",
            "modified_time": "2026-06-04T10:00:00+02:00",
            "source_ref": f"https://drive.google.com/file/d/{file_id}",
        },
        observed_at="2026-06-04T10:01:00+02:00",
        effective_at="2026-06-04T10:00:00+02:00",
        case_key_hint="case-key-agent-d",
        thread_key_hint="case-key-agent-d",
        business_lane="finance",
        signal_summary_pl="Nowa faktura Drive",
        payload={
            "document_row": {
                "document_id": "doc-agent-d",
                "drive_item_id": file_id,
                "file_name": "invoice.pdf",
                "source_ref": f"https://drive.google.com/file/d/{file_id}",
                "lane": "finance",
                "document_kind": "invoice",
                "scope": "case_specific",
                "case_id": case_id,
                "probable_case_key": "case-key-agent-d",
                "extraction_confidence": 0.9,
                "link_confidence": 0.95,
                "metadata": {"link_reasons": ["invoice_number_match"]},
            },
            "case_seed_row": {
                "case_id": case_id,
                "case_key": "case-key-agent-d",
                "thread_id": "",
                "case_family": "finance",
                "mailbox": "drive",
                "subject": "invoice.pdf",
                "status": "open",
                "customer_name": "",
                "customer_email": "klient@example.com",
                "metadata": {"source": "drive"},
                "created_at": "2026-06-04T10:00:00+02:00",
                "updated_at": "2026-06-04T10:00:00+02:00",
            },
            "case_id": case_id,
            "case_key": "case-key-agent-d",
            "linkage_status": "deterministic",
            "link_reasons": ["invoice_number_match"],
        },
        artifacts={"source": "test", "raw_observation_id": "obs-drive-agent-d"},
        revision_marker="rev-agent-d",
        created_by_runtime="test",
    )


def test_reconcile_drive_signal_uses_agent_path() -> None:
    store, context = _context()
    signal = _drive_signal()
    registry = CorrelationRegistryService(InMemoryCorrelationRegistryStore())
    registry.bootstrap()
    registry.sync_mailbox_case(
        case_id="case_agent_drive",
        customer_email="klient@example.com",
    )
    initial = build_initial_snapshot(
        case_id="case_agent_drive",
        engagement_id="eng_drive_pr_d",
        trace_id=signal.signal_id,
    )
    initial.operational_status = OperationalStatus(code="enriching", steps_remaining=10)
    graph_result = AgentGraphRunResult(snapshot=initial, turns=[])

    with patch.dict(
        os.environ,
        {"AGENT_RUNTIME_ENABLED": "1", "AGENT_RUNTIME_MODE": "prep", "AGENT_OPENAI_API_KEY": "sk-test"},
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
            mock_run.return_value = AgentRunResult(
                snapshot=initial.model_copy(
                    update={
                        "version": 2,
                        "operational_status": OperationalStatus(
                            code="pending_operator",
                            steps_remaining=0,
                            blocking=True,
                        ),
                    }
                ),
                graph=graph_result,
                version=2,
            )
            result = _reconcile_drive_signal(
                signal,
                runtime_context=context,
                dry_run=False,
                entity_link_dict={"case_id": "case_agent_drive"},
            )

    mock_downstream.assert_not_called()
    mock_run.assert_called_once()
    passed_signal = mock_run.call_args.kwargs["signal"]
    assert passed_signal.get("drive_file_id") == "drv_agent_d"
    assert passed_signal.get("channel") == "drive"
    assert result.processing_state == "reconciled"
    assert result.case_id == "case_agent_drive"
    assert result.source_kind == "drive"
    assert result.stage_outputs.get("reconcile_path") == "agent_runtime"
    assert result.preview.get("reconcile_path") == "agent_runtime"
    assert any("agent_runtime" in str(w) for w in result.warnings)


def test_agent_mode_skips_shared_downstream() -> None:
    store, context = _context()
    signal = _gmail_signal()
    registry = CorrelationRegistryService(InMemoryCorrelationRegistryStore())
    registry.bootstrap()
    registry.sync_mailbox_case(
        case_id="case_agent_d",
        customer_email="klient@example.com",
        message_id="msg-agent-d",
    )
    initial = build_initial_snapshot(
        case_id="case_agent_d",
        engagement_id="eng_pr_d_test",
        trace_id=signal.signal_id,
    )
    initial.operational_status = OperationalStatus(code="enriching", steps_remaining=10)
    graph_result = AgentGraphRunResult(snapshot=initial, turns=[])

    with patch.dict(
        os.environ,
        {"AGENT_RUNTIME_ENABLED": "1", "AGENT_RUNTIME_MODE": "prep", "AGENT_OPENAI_API_KEY": "sk-test"},
        clear=False,
    ):
        assert agent_runtime_reconcile_active()
        assert not legacy_downstream_reconcile_active()
        with patch(
            "agent_runtime.agent_reconcile.build_registry_for_reconcile",
            return_value=registry,
        ), patch(
            "agent_runtime.agent_reconcile.execute_agent_run",
        ) as mock_run, patch(
            "intake_shared_downstream.run_shared_downstream_stages",
        ) as mock_downstream:
            mock_run.return_value = AgentRunResult(
                snapshot=initial.model_copy(
                    update={
                        "version": 2,
                        "operational_status": OperationalStatus(
                            code="pending_operator",
                            steps_remaining=0,
                            blocking=True,
                        ),
                    }
                ),
                graph=graph_result,
                version=2,
            )
            result = _reconcile_gmail_signal(
                signal,
                runtime_context=context,
                dry_run=False,
                entity_link_dict={"case_id": "case_agent_d"},
            )

    mock_downstream.assert_not_called()
    mock_run.assert_called_once()
    assert result.processing_state == "reconciled"
    assert result.case_id == "case_agent_d"
    assert result.v2_projection is not None
    assert result.v2_projection["signal_projection"]["signal_id"] == signal.signal_id
    assert any("agent_runtime" in str(w) for w in result.warnings)
    assert result.stage_outputs.get("agent_engagement_snapshot")
    assert result.stage_outputs.get("reconcile_path") == "agent_runtime"
    assert result.preview.get("reconcile_path") == "agent_runtime"


def test_agent_dry_run_skips_execute_but_returns_projection() -> None:
    store, context = _context()
    signal = _gmail_signal()
    registry = CorrelationRegistryService(InMemoryCorrelationRegistryStore())
    registry.bootstrap()
    registry.sync_mailbox_case(
        case_id="case_agent_d",
        customer_email="klient@example.com",
    )
    with patch.dict(
        os.environ,
        {"AGENT_RUNTIME_ENABLED": "1", "AGENT_RUNTIME_MODE": "prep", "AGENT_OPENAI_API_KEY": "sk-test"},
        clear=False,
    ):
        with patch("agent_runtime.agent_reconcile.build_registry_for_reconcile", return_value=registry), patch(
            "agent_runtime.agent_reconcile.execute_agent_run",
        ) as mock_run:
            result = _reconcile_gmail_signal(
                signal,
                runtime_context=context,
                dry_run=True,
                entity_link_dict={"case_id": "case_agent_d"},
            )
    mock_run.assert_not_called()
    assert result.processing_state == "shadowed"
    assert result.v2_projection is not None


def test_agent_run_failure_does_not_break_reconcile() -> None:
    store, context = _context()
    signal = _gmail_signal()
    registry = CorrelationRegistryService(InMemoryCorrelationRegistryStore())
    registry.bootstrap()
    registry.sync_mailbox_case(case_id="case_agent_d", customer_email="klient@example.com")
    with patch.dict(
        os.environ,
        {"AGENT_RUNTIME_ENABLED": "1", "AGENT_RUNTIME_MODE": "prep", "AGENT_OPENAI_API_KEY": "sk-test"},
        clear=False,
    ):
        with patch("agent_runtime.agent_reconcile.build_registry_for_reconcile", return_value=registry), patch(
            "agent_runtime.agent_reconcile.execute_agent_run",
            side_effect=RuntimeError("planner boom"),
        ):
            result = _reconcile_gmail_signal(
                signal,
                runtime_context=context,
                dry_run=False,
                entity_link_dict={"case_id": "case_agent_d"},
            )
    assert result.processing_state == "reconciled"
    assert any("agent_run_failed" in str(w) for w in result.warnings)


def test_run_agent_reconcile_staging_refreshes_existing_snapshot_trace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, context = _context()
    signal = _gmail_signal(case_id="")
    operator_store = InMemoryOperatorEngagementStore()
    existing = build_initial_snapshot(
        case_id="",
        engagement_id="stg_test_trace_refresh",
        signal_id="sig_old_staging",
        trace_id="sig_old_staging",
    )
    operator_store.insert_snapshot(existing)

    monkeypatch.setattr(
        "agent_runtime.agent_reconcile.build_operator_engagement_store",
        lambda settings, *, allow_in_memory=False: operator_store,
    )
    monkeypatch.setattr("agent_runtime.agent_reconcile.get_trace_id", lambda: "trace_runtime_other")
    monkeypatch.setattr(
        "agent_runtime.engagement_resolver.resolve_staging_engagement",
        lambda payload, signal_id="": type("Resolution", (), {"engagement_id": "stg_test_trace_refresh"})(),
    )

    def _fake_run(engagement_id, *, store, signal, settings, mailbox_store, require_enabled, operator_scope):
        snap = store.load_snapshot(engagement_id)
        assert snap is not None
        return AgentRunResult(
            snapshot=snap,
            graph=AgentGraphRunResult(snapshot=snap, turns=[]),
            version=snap.version,
        )

    with patch.dict(
        os.environ,
        {"AGENT_RUNTIME_ENABLED": "1", "AGENT_RUNTIME_MODE": "prep", "AGENT_OPENAI_API_KEY": "sk-test"},
        clear=False,
    ), patch(
        "agent_runtime.agent_reconcile.execute_agent_run",
        side_effect=_fake_run,
    ):
        snapshot, run_result, resolution, warnings = run_agent_reconcile_staging(
            signal,
            runtime_context=context,
            dry_run=False,
            intake_output=dict(signal.payload.get("intake_result_final") or {}),
        )

    refreshed = operator_store.load_snapshot("stg_test_trace_refresh")
    assert refreshed is not None
    assert refreshed.signal_id == signal.signal_id
    assert refreshed.trace_id == "trace_runtime_other"
    assert snapshot.signal_id == signal.signal_id
    assert snapshot.trace_id == "trace_runtime_other"
    assert run_result is not None
    assert resolution.engagement_id == "stg_test_trace_refresh"
    assert len(operator_store._rows) == 1


def test_run_agent_reconcile_staging_prevents_intake_signal_id_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, context = _context()
    signal = _gmail_signal(case_id="")
    operator_store = InMemoryOperatorEngagementStore()

    monkeypatch.setattr(
        "agent_runtime.agent_reconcile.build_operator_engagement_store",
        lambda settings, *, allow_in_memory=False: operator_store,
    )
    monkeypatch.setattr("agent_runtime.agent_reconcile.get_trace_id", lambda: "trace_runtime_override_guard")

    seen: dict[str, object] = {}

    def _resolve(payload, signal_id=""):  # type: ignore[no-untyped-def]
        seen["payload"] = dict(payload)
        seen["signal_id"] = signal_id
        return type("Resolution", (), {"engagement_id": "stg_test_override_guard"})()

    monkeypatch.setattr("agent_runtime.engagement_resolver.resolve_staging_engagement", _resolve)

    with patch.dict(
        os.environ,
        {"AGENT_RUNTIME_ENABLED": "1", "AGENT_RUNTIME_MODE": "prep", "AGENT_OPENAI_API_KEY": "sk-test"},
        clear=False,
    ):
        snapshot, run_result, resolution, warnings = run_agent_reconcile_staging(
            signal,
            runtime_context=context,
            dry_run=True,
            intake_output={"signal_id": "sig-from-intake", "message": {"message_id": "msg-agent-d"}},
        )

    assert seen["signal_id"] == signal.signal_id
    assert seen["payload"]["signal_id"] == signal.signal_id
    assert snapshot.signal_id == signal.signal_id
    assert snapshot.trace_id == "trace_runtime_override_guard"
    assert run_result is None
    assert resolution.engagement_id == "stg_test_override_guard"
    assert "agent_staging_dry_run_skipped_execute" in warnings


def test_unknown_agent_runtime_mode_raises_config_error() -> None:
    import pytest

    from agent_runtime.validate import AgentRuntimeConfigError

    with patch.dict(os.environ, {"AGENT_RUNTIME_MODE": "bogus"}, clear=False):
        with pytest.raises(AgentRuntimeConfigError):
            from agent_runtime.settings import load_agent_runtime_settings

            load_agent_runtime_settings()


def test_operator_store_load_snapshots_for_case_ids() -> None:
    op_store = InMemoryOperatorEngagementStore()
    op_store.init_snapshot_from_signal(
        signal={"signal_id": "s1"},
        case_id="case_a",
        engagement_id="eng_a",
    )
    op_store.init_snapshot_from_signal(
        signal={"signal_id": "s2"},
        case_id="case_b",
        engagement_id="eng_b",
    )
    loaded = op_store.load_snapshots_for_case_ids(["case_a", "case_missing", "case_b"])
    assert len(loaded) == 2
    assert {s.case_id for s in loaded} == {"case_a", "case_b"}


def test_job_store_records_completed_run() -> None:
    jobs = InMemoryAgentJobStore()
    row = jobs.record_completed(engagement_id="eng_j", signal_id="sig_j", case_id="case_j")
    assert row["status"] == "completed"


def test_legacy_mode_still_calls_shared_downstream() -> None:
    from agent_runtime.agent_reconcile import agent_runtime_reconcile_active

    _store, context = _context()
    signal = _gmail_signal(case_id="case_legacy_d")

    with patch.dict(
        os.environ,
        {"AGENT_RUNTIME_ENABLED": "0", "AGENT_RUNTIME_MODE": "legacy"},
        clear=False,
    ):
        assert agent_runtime_reconcile_active() is False
        with patch(
            "gmail_intake.hydrate_intelligence_seam_config",
            return_value=None,
        ), patch(
            "intake_shared_downstream.run_shared_downstream_stages",
        ) as mock_ds:
            mock_ds.return_value = type(
                "D",
                (),
                {
                    "case_link_result": {"selected_case_key": "k", "decision": "linked", "reasons": []},
                    "business_result": {},
                    "reply_result": {"draft_enabled": False, "drafts": []},
                    "action_plan_result": {"primary_action": "review"},
                    "case_intelligence_result": {},
                    "mailbox_memory_result": {"case_id": "case_legacy_d", "snapshot": {}, "policy_report": {"status": "OK"}},
                    "context_bundle": {},
                    "stage_config": {},
                    "warnings": [],
                    "hot_state_snapshot": {},
                },
            )()
            v2_inner = {
                "signal_projection": {"signal_id": signal.signal_id},
                "case_patch": {"command": "noop"},
                "desk_note_patch": {
                    "command": "noop",
                    "presence_mode": "silent",
                    "lifecycle": "active",
                    "source_signal_ids": [signal.signal_id],
                },
                "decision_trace": {
                    "trigger_signal_id": signal.signal_id,
                    "presence_mode": "silent",
                },
            }
            with patch("gmail_intake.build_projection_preview", return_value={}), patch(
                "projection_snapshot_transport.build_operator_projection_snapshot",
                return_value={"v2_projection": v2_inner, "projection_validation": {"ok": True}},
            ):
                _reconcile_gmail_signal(
                    signal,
                    runtime_context=context,
                    dry_run=True,
                    entity_link_dict={"case_id": "case_legacy_d"},
                )
            mock_ds.assert_called_once()
