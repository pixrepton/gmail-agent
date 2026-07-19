"""D3: duplicate CanonicalSignal append must not re-reconcile."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gmail_signal_adapter import run_gmail_signal_runtime
from mailbox_memory_store import InMemoryMailboxMemoryStore


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        mailbox_memory_blob_root=Path(tempfile.gettempdir()) / "cel-d3-idempotency",
        signal_journal_jsonl_mirror_enabled=False,
        signal_runtime_mode="active",
        groq_model="test-model",
    )


def test_duplicate_gmail_signal_skips_second_reconcile() -> None:
    settings = _settings()
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    run_state: dict = {"signal_store": store}
    snapshot = {
        "source_message": {"message_id": "msg-d3-dup", "thread_id": "t1", "subject": "Test"},
        "mailbox": "test@example.com",
    }
    fake_reconcile = SimpleNamespace(
        signal_id="sig-dup",
        source_kind="gmail",
        signal_kind="gmail_message_observed",
        processing_state="reconciled",
        mailbox_memory_result={"case_id": "case-1"},
    )
    reconcile_calls: list[int] = []

    def _reconcile(*_a, **_k):
        reconcile_calls.append(1)
        return fake_reconcile

    with patch("gmail_signal_adapter.reconcile_signal", side_effect=_reconcile):
        first = run_gmail_signal_runtime(
            settings=settings,
            run_state=run_state,
            snapshot=snapshot,
            intake_result_final={"decision": {"action": "review"}},
            preclassification_result={"lane": "intake_llm"},
            lane_stage_plan={"run_case_linking": True},
            context_bundle={"context_messages": []},
            model="m",
            verbose=False,
            dry_run=False,
        )
        second = run_gmail_signal_runtime(
            settings=settings,
            run_state=run_state,
            snapshot=snapshot,
            intake_result_final={"decision": {"action": "review"}},
            preclassification_result={"lane": "intake_llm"},
            lane_stage_plan={"run_case_linking": True},
            context_bundle={"context_messages": []},
            model="m",
            verbose=False,
            dry_run=False,
        )

    assert first.append_results[0].inserted is True
    assert second.append_results[0].inserted is False
    assert second.reconcile_result.processing_state == "skipped_duplicate"
    assert len(reconcile_calls) == 1


def test_projection_proof_forces_reconcile_on_journal_duplicate() -> None:
    settings = _settings()
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    run_state: dict = {
        "signal_store": store,
        "runtime_controls": {"projection_proof": True},
    }
    snapshot = {
        "source_message": {"message_id": "msg-d3-proof-force", "thread_id": "t1", "subject": "Test"},
        "mailbox": "test@example.com",
    }
    fake_reconcile = SimpleNamespace(
        signal_id="sig-proof",
        source_kind="gmail",
        signal_kind="gmail_message_observed",
        processing_state="reconciled",
        mailbox_memory_result={"case_id": "case-1"},
        stage_outputs={},
        v2_projection={"case_id": "case-1"},
        preview=None,
    )
    reconcile_calls: list[int] = []

    def _reconcile(*_a, **_k):
        reconcile_calls.append(1)
        return fake_reconcile

    with patch("gmail_signal_adapter.reconcile_signal", side_effect=_reconcile):
        first = run_gmail_signal_runtime(
            settings=settings,
            run_state=run_state,
            snapshot=snapshot,
            intake_result_final={"decision": {"action": "review"}},
            preclassification_result={"lane": "intake_llm"},
            lane_stage_plan={"run_case_linking": True},
            context_bundle={"context_messages": []},
            model="m",
            verbose=False,
            dry_run=False,
        )
        second = run_gmail_signal_runtime(
            settings=settings,
            run_state=run_state,
            snapshot=snapshot,
            intake_result_final={"decision": {"action": "review"}},
            preclassification_result={"lane": "intake_llm"},
            lane_stage_plan={"run_case_linking": True},
            context_bundle={"context_messages": []},
            model="m",
            verbose=False,
            dry_run=False,
        )

    assert first.append_results[0].inserted is True
    assert second.append_results[0].inserted is False
    assert second.reconcile_result.processing_state == "reconciled"
    assert len(reconcile_calls) == 2
    assert any("proof_force_v2_reprocess" in str(w) for w in run_state.get("warnings", []))
