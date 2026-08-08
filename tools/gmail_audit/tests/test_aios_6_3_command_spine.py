"""AI-OS 6.3 - OperatorCommand spine uses canonical reconcile."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.operator_command_spine import run_operator_command_spine
from agent_runtime.store import build_staging_snapshot
from llm_contracts.engagement_snapshot_v2 import AgentMemory, HitlGate, MaterializeProposalItem
from mailbox_memory_store import InMemoryMailboxMemoryStore
from signal_reconciler import ReconcileResult


class _Runtime:
    def __init__(self, store: InMemoryMailboxMemoryStore) -> None:
        self.store = store
        self.graph_store = None

    def bootstrap(self) -> None:
        self.store.bootstrap()


class _FakeConn:
    def close(self) -> None:
        return None


class _EntityLink:
    def __init__(self, *, case_id: str = "", case_key: str = "") -> None:
        self.case_id = case_id
        self.case_key = case_key
        self.link_status = "VERIFIED" if case_id else "NO_MATCH"

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "case_key": self.case_key,
            "link_status": self.link_status,
            "link_confidence": 1.0 if self.case_id else 0.0,
        }


def _settings(*, db_url: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        mailbox_memory_blob_root=Path(tempfile.gettempdir()) / "operator-command-spine-tests",
        mailbox_memory_database_url=db_url,
        mailbox_memory_stage_mode="shadow",
        google_drive_graph_enabled=False,
        signal_journal_jsonl_mirror_enabled=False,
        signal_runtime_mode="active",
        groq_model="test-model",
    )


def _snapshot(
    *,
    engagement_id: str,
    signal_id: str,
    user_instruction: str = "",
    hitl_required: bool = False,
    with_proposal: bool = False,
) -> Any:
    materialize_proposals = []
    if with_proposal:
        materialize_proposals.append(
            MaterializeProposalItem(
                proposal_id="prop_1",
                proposal_type="composite_plan",
                status="pending",
                payload_json={"kind": "test_materialize"},
            )
        )
    snapshot = build_staging_snapshot(
        engagement_id=engagement_id,
        signal_id=signal_id,
        trace_id="trace_operator_command_test",
    )
    return snapshot.model_copy(
        update={
            "agent_memory": AgentMemory(materialize_proposals=materialize_proposals),
            "hitl_gate": HitlGate(required=hitl_required, reason="operator approval" if hitl_required else ""),
            "user_instruction": user_instruction,
        }
    )


def _reconcile_result(signal: Any, snapshot: Any, *, warnings: list[str] | None = None) -> ReconcileResult:
    return ReconcileResult(
        signal_id=signal.signal_id,
        source_kind=signal.source_kind,
        signal_kind=signal.signal_kind,
        processing_state="reconciled",
        case_id=str(getattr(snapshot, "case_id", "") or ""),
        mailbox_memory_result={"engagement_id": snapshot.engagement_id},
        rebuild_result={"engagement_id": snapshot.engagement_id},
        stage_outputs={"agent_engagement_snapshot": snapshot.model_dump(mode="python")},
        warnings=list(warnings or []),
    )


def _patch_runtime(monkeypatch, store: InMemoryMailboxMemoryStore) -> None:
    runtime = _Runtime(store)
    monkeypatch.setattr("mailbox_memory_runtime.build_mailbox_memory_runtime", lambda _settings: runtime)


def _patch_real_reconcile_dependencies(
    monkeypatch,
    *,
    store: InMemoryMailboxMemoryStore,
    staging,
    link_case_id: str = "",
) -> list[str]:
    import signal_reconciler
    from agent_runtime.signal_registry import SIGNAL_HANDLERS

    _patch_runtime(monkeypatch, store)
    monkeypatch.setattr(signal_reconciler, "_maybe_cleanup_staging_engagements", lambda _ctx: None)
    monkeypatch.setattr("agent_runtime.agent_reconcile.agent_runtime_reconcile_active", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "agent_runtime.agent_reconcile.load_agent_runtime_settings",
        lambda: SimpleNamespace(mode="prep", enabled=True),
    )
    monkeypatch.setattr("agent_runtime.agent_reconcile.projection_canonical_enabled", lambda: False)
    monkeypatch.setattr(
        "agent_runtime.agent_reconcile.build_v2_projection_from_engagement",
        lambda *_a, **_k: {"projection": "thin"},
    )
    monkeypatch.setattr(
        "agent_runtime.agent_reconcile.build_operator_snapshot_from_engagement",
        lambda *_a, **_k: {"projection": "operator"},
    )
    monkeypatch.setattr("agent_runtime.agent_reconcile.run_agent_reconcile_staging", staging)

    calls: list[str] = []

    class _SpyEntityLinker:
        def __init__(self, _store: Any) -> None:
            self.store = _store

        def find_case(self, signal: Any) -> _EntityLink:
            calls.append(signal.signal_id)
            case_id = link_case_id or str((signal.payload or {}).get("case_id") or "")
            return _EntityLink(case_id=case_id, case_key=f"key_{case_id}" if case_id else "")

    monkeypatch.setattr(signal_reconciler, "EntityLinker", _SpyEntityLinker)

    original_handler = SIGNAL_HANDLERS["operator_command"]

    def _spy_handler(signal: Any, *, runtime_context: Any, dry_run: bool, entity_link_dict: dict[str, Any]):
        calls.append(signal.source_kind)
        return original_handler(
            signal,
            runtime_context=runtime_context,
            dry_run=dry_run,
            entity_link_dict=entity_link_dict,
        )

    monkeypatch.setitem(SIGNAL_HANDLERS, "operator_command", _spy_handler)
    return calls


def test_sync_agent_chat_route_passes_through_reconcile_signal(monkeypatch) -> None:
    store = InMemoryMailboxMemoryStore()
    _patch_runtime(monkeypatch, store)
    reconcile_calls: list[str] = []

    def _fake_reconcile(signal: Any, *, runtime_context: Any, dry_run: bool) -> ReconcileResult:
        reconcile_calls.append(signal.signal_id)
        assert runtime_context.journal is not None
        assert signal.source_kind == "operator_command"
        return _reconcile_result(
            signal,
            _snapshot(engagement_id="eng_sync", signal_id=signal.signal_id),
            warnings=["sync_reconcile_path"],
        )

    monkeypatch.setattr("signal_reconciler.reconcile_signal", _fake_reconcile)
    monkeypatch.setattr(
        "agent_runtime.agent_reconcile.run_agent_reconcile_staging",
        MagicMock(side_effect=AssertionError("bypass must not call staging directly")),
    )

    import api_app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api_app, "load_settings", lambda *_a, **_k: _settings())
    monkeypatch.setattr(
        api_app,
        "_require_mutation_principal",
        lambda: SimpleNamespace(scope=SimpleNamespace(operator_id="op_sync"), operator_id="op_sync"),
    )
    app = api_app.create_app(
        runtime_provider=lambda: None,
        cohort_reader=lambda _run_id: None,
        registry_provider=lambda: None,
    )
    response = TestClient(app).post(
        "/agent-chat",
        json={"user_input": "sprawdz pipeline", "session_id": "sess_sync"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["receipt"]["status"] == "completed"
    assert reconcile_calls == [payload["signal_id"]]
    assert len(store.signals or {}) == 1


def test_async_agent_chat_worker_passes_through_same_reconcile_signal(monkeypatch) -> None:
    store = InMemoryMailboxMemoryStore()
    _patch_runtime(monkeypatch, store)
    reconcile_calls: list[str] = []

    def _fake_reconcile(signal: Any, *, runtime_context: Any, dry_run: bool) -> ReconcileResult:
        reconcile_calls.append(signal.signal_id)
        assert signal.payload["command_id"] == "cmd_async"
        return _reconcile_result(signal, _snapshot(engagement_id="eng_async", signal_id=signal.signal_id))

    monkeypatch.setattr("signal_reconciler.reconcile_signal", _fake_reconcile)
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda _db_url: _FakeConn()))
    monkeypatch.setattr("agent_runtime.agent_chat_jobs.ensure_agent_chat_jobs_table", lambda _conn: None)
    jobs = [
        {
            "job_id": "job_async",
            "command_id": "cmd_async",
            "session_id": "sess_async",
            "case_id": "case_async",
            "request_json": {
                "user_input": "asynchroniczne polecenie",
                "session_id": "sess_async",
                "case_id": "case_async",
            },
        }
    ]
    monkeypatch.setattr("agent_runtime.agent_chat_jobs.claim_next_agent_chat_job", lambda _conn: jobs.pop(0) if jobs else None)
    completed: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "agent_runtime.agent_chat_jobs.complete_agent_chat_job",
        lambda _conn, **kwargs: completed.append(kwargs),
    )

    from agent_runtime.agent_chat_worker import process_agent_chat_jobs_tick

    result = process_agent_chat_jobs_tick(_settings(db_url="postgresql://test"), max_jobs=1)

    assert result["ok"] is True
    assert result["processed"] == 1
    assert len(reconcile_calls) == 1
    assert completed[0]["status"] == "completed"
    assert completed[0]["receipt"]["signal_id"] == reconcile_calls[0]


def test_registered_operator_command_handler_entity_linker_and_processing_attempts(monkeypatch) -> None:
    store = InMemoryMailboxMemoryStore()
    seen: dict[str, Any] = {}

    def _fake_staging(signal: Any, *, runtime_context: Any, dry_run: bool, intake_output: dict[str, Any]):
        seen["payload"] = dict(signal.payload or {})
        seen["source_ref"] = dict(signal.source_ref or {})
        seen["intake"] = dict(intake_output or {})
        assert seen["payload"]["case_id"] == "case_42"
        assert seen["payload"]["user_instruction"] == "zrob follow-up"
        assert seen["payload"]["correlation_id"]
        return (
            _snapshot(engagement_id="eng_case", signal_id=signal.signal_id),
            None,
            SimpleNamespace(engagement_id="eng_case"),
            [],
        )

    calls = _patch_real_reconcile_dependencies(
        monkeypatch,
        store=store,
        staging=_fake_staging,
        link_case_id="case_42",
    )

    result = run_operator_command_spine(
        user_input="zrob follow-up",
        session_id="sess_case",
        case_id="case_42",
        opmem_context={},
        settings=_settings(),
        command_id="cmd_case",
    )

    attempts = store.fetch_signal_processing_attempts(result["signal_id"])
    statuses = {item["status"] for item in attempts}
    assert result["receipt"]["status"] == "completed"
    assert seen["source_ref"]["case_id"] == "case_42"
    assert seen["intake"]["is_general_chat"] is False
    assert "operator_command" in calls
    assert result["signal_id"] in calls
    assert {"started", "reconciled"}.issubset(statuses)


def test_general_chat_stays_case_free_and_hitl_receipt_is_preserved(monkeypatch) -> None:
    store = InMemoryMailboxMemoryStore()
    seen: dict[str, Any] = {}

    def _fake_staging(signal: Any, *, runtime_context: Any, dry_run: bool, intake_output: dict[str, Any]):
        seen["payload"] = dict(signal.payload or {})
        seen["intake"] = dict(intake_output or {})
        assert seen["payload"]["case_id"] == ""
        assert seen["payload"]["user_instruction"] == "przygotuj draft do zatwierdzenia"
        return (
            _snapshot(
                engagement_id="eng_general",
                signal_id=signal.signal_id,
                user_instruction="przygotuj draft do zatwierdzenia",
                hitl_required=True,
                with_proposal=True,
            ),
            None,
            SimpleNamespace(engagement_id="eng_general"),
            ["planner_hitl"],
        )

    _patch_real_reconcile_dependencies(monkeypatch, store=store, staging=_fake_staging)

    result = run_operator_command_spine(
        user_input="przygotuj draft do zatwierdzenia",
        session_id="sess_general",
        case_id="",
        opmem_context={},
        settings=_settings(),
        command_id="cmd_general",
    )

    assert seen["intake"]["is_general_chat"] is True
    assert seen["intake"]["case_id"] == ""
    assert result["receipt"]["status"] == "hitl_required"
    assert result["receipt"]["hitl_required"] is True
    assert result["proposals"] == [
        {
            "proposal_id": "prop_1",
            "proposal_type": "composite_plan",
            "status": "pending",
            "payload": {"kind": "test_materialize"},
        }
    ]
    assert result["snapshot_eng"].user_instruction == "przygotuj draft do zatwierdzenia"


def test_reconcile_failure_returns_failed_receipt(monkeypatch) -> None:
    store = InMemoryMailboxMemoryStore()
    _patch_runtime(monkeypatch, store)

    def _boom(*_args: Any, **_kwargs: Any) -> ReconcileResult:
        raise RuntimeError("planner boom")

    monkeypatch.setattr("signal_reconciler.reconcile_signal", _boom)

    result = run_operator_command_spine(
        user_input="wykonaj niemozliwe",
        session_id="sess_fail",
        case_id="case_fail",
        opmem_context={},
        settings=_settings(),
        command_id="cmd_fail",
    )

    assert result["receipt"]["status"] == "failed"
    assert "planner boom" in result["receipt"]["error"]
    assert result["journal_inserted"] is True
    assert len(store.signals or {}) == 1


def test_idempotent_replay_skips_duplicate_signal_and_second_agent_run(monkeypatch) -> None:
    store = InMemoryMailboxMemoryStore()
    staging_calls: list[str] = []

    def _fake_staging(signal: Any, *, runtime_context: Any, dry_run: bool, intake_output: dict[str, Any]):
        staging_calls.append(signal.signal_id)
        return (
            _snapshot(engagement_id="eng_replay", signal_id=signal.signal_id),
            None,
            SimpleNamespace(engagement_id="eng_replay"),
            [],
        )

    calls = _patch_real_reconcile_dependencies(monkeypatch, store=store, staging=_fake_staging)

    first = run_operator_command_spine(
        user_input="ten sam tekst",
        session_id="sess_replay",
        case_id="case_replay",
        opmem_context={},
        settings=_settings(),
        command_id="cmd_replay",
    )
    second = run_operator_command_spine(
        user_input="ten sam tekst",
        session_id="sess_replay",
        case_id="case_replay",
        opmem_context={},
        settings=_settings(),
        command_id="cmd_replay",
    )

    attempts = store.fetch_signal_processing_attempts(first["signal_id"])
    statuses = [item["status"] for item in attempts]
    assert first["journal_inserted"] is True
    assert second["journal_inserted"] is False
    assert second["journal_duplicate"] is True
    assert first["signal_id"] == second["signal_id"]
    assert len(store.signals or {}) == 1
    assert staging_calls == [first["signal_id"]]
    assert calls.count("operator_command") == 1
    assert statuses.count("started") == 1
    assert statuses.count("reconciled") == 1
    assert second["receipt"]["status"] == "completed"
    assert "signal_journal_duplicate_skipped_reconcile" in second["receipt"]["warnings"]
