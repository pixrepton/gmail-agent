"""DQ-18: Case Intelligence degradation is a separate state dimension from core
reconciliation, made durable, operator-visible, and retryable without repeating
the whole signal or any effect.

Covers:
- unit correctness of `case_intelligence_degradation.py` (the durable state module)
- the live `signal_reconciler.reconcile_signal` path recording degradation while
  keeping `processing_state == "reconciled"` (DQ-18's explicit, decided answer —
  core reconciliation and Case Intelligence are separate dimensions)
- retry via `signal_reconciler.retry_degraded_case_intelligence`: success clears
  degradation, repeated failure counts attempts and terminalizes at the max, a
  terminally degraded case refuses further retries, and a staleness guard refuses
  to retry a signal a newer signal has already superseded
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_intelligence_degradation import (
    DEGRADATION_MAX_ATTEMPTS,
    case_intelligence_result_is_fallback,
    clear_case_intelligence_degradation,
    is_case_intelligence_terminally_degraded,
    latest_signal_id_for_case,
    maybe_record_case_intelligence_degradation,
    read_case_intelligence_degradation,
    record_case_intelligence_degradation,
)
from mailbox_memory_store import InMemoryMailboxMemoryStore
from signal_contract import build_canonical_signal
from signal_journal import SignalJournal
from signal_reconciler import SignalRuntimeContext, reconcile_signal, retry_degraded_case_intelligence


# --- module-level unit tests -------------------------------------------------


def _store() -> InMemoryMailboxMemoryStore:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    return store


def test_case_intelligence_result_is_fallback_detects_the_marker() -> None:
    assert case_intelligence_result_is_fallback(
        {"execution_metadata": {"source_mode": "fallback"}}
    )
    assert not case_intelligence_result_is_fallback({"execution_metadata": {"source_mode": "model_result"}})
    assert not case_intelligence_result_is_fallback({})
    assert not case_intelligence_result_is_fallback(None)


def test_record_creates_and_increments_durable_state() -> None:
    store = _store()
    store.upsert_case({"case_id": "case-a", "case_key": "k", "metadata": {}})

    first = record_case_intelligence_degradation(
        store, "case-a", signal_id="sig-1", failure_reason="RuntimeError"
    )
    assert first["degraded"] is True
    assert first["attempts"] == 1
    assert first["terminally_degraded"] is False

    second = record_case_intelligence_degradation(
        store, "case-a", signal_id="sig-2", failure_reason="RuntimeError"
    )
    assert second["attempts"] == 2

    read_back = read_case_intelligence_degradation(store, "case-a")
    assert read_back == second


def test_terminally_degraded_after_max_attempts() -> None:
    store = _store()
    store.upsert_case({"case_id": "case-b", "case_key": "k", "metadata": {}})
    state = {}
    for _ in range(DEGRADATION_MAX_ATTEMPTS):
        state = record_case_intelligence_degradation(
            store, "case-b", signal_id="sig-x", failure_reason="x", max_attempts=DEGRADATION_MAX_ATTEMPTS
        )
    assert state["attempts"] == DEGRADATION_MAX_ATTEMPTS
    assert state["terminally_degraded"] is True
    assert is_case_intelligence_terminally_degraded(store, "case-b")


def test_clear_removes_state_and_is_safe_on_missing_case() -> None:
    store = _store()
    store.upsert_case({"case_id": "case-c", "case_key": "k", "metadata": {}})
    record_case_intelligence_degradation(store, "case-c", signal_id="sig-1", failure_reason="x")
    assert read_case_intelligence_degradation(store, "case-c")["degraded"] is True

    clear_case_intelligence_degradation(store, "case-c")
    assert read_case_intelligence_degradation(store, "case-c") == read_case_intelligence_degradation(store, "unknown-case")

    # Missing case entirely: must not raise.
    clear_case_intelligence_degradation(store, "does-not-exist")


def test_maybe_record_is_a_noop_when_not_fallback() -> None:
    store = _store()
    store.upsert_case({"case_id": "case-d", "case_key": "k", "metadata": {}})
    warnings = maybe_record_case_intelligence_degradation(
        store, "case-d", {"execution_metadata": {"source_mode": "model_result"}}, signal_id="sig-1"
    )
    assert warnings == []
    assert read_case_intelligence_degradation(store, "case-d")["degraded"] is False


def test_maybe_record_returns_a_warning_string_on_fallback() -> None:
    store = _store()
    store.upsert_case({"case_id": "case-e", "case_key": "k", "metadata": {}})
    warnings = maybe_record_case_intelligence_degradation(
        store, "case-e", {"execution_metadata": {"source_mode": "fallback"}}, signal_id="sig-1"
    )
    assert warnings == ["case_intelligence_degraded:attempts=1"]


# --- live-path integration: signal_reconciler.reconcile_signal --------------


def _runtime_context() -> tuple[InMemoryMailboxMemoryStore, SignalRuntimeContext]:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    settings = SimpleNamespace(
        mailbox_memory_blob_root=Path(tempfile.gettempdir()) / "dq18-tests",
        signal_journal_jsonl_mirror_enabled=False,
        signal_runtime_mode="active",
        groq_model="test-model",
    )
    context = SignalRuntimeContext(
        settings=settings,
        journal=SignalJournal(store),
        store=store,
        graph_store=None,
        run_state={},
        model="test-model",
        verbose=False,
        mode="active",
    )
    return store, context


def _degrading_signal(signal_id: str) -> object:
    return build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"mailbox": "ops@example.com", "message_id": signal_id, "thread_id": f"thr-{signal_id}", "history_id": "1"},
        observed_at="2026-04-13T09:05:00+02:00",
        effective_at="2026-04-13T09:04:00+02:00",
        thread_key_hint=f"thr-{signal_id}",
        business_lane="service",
        signal_summary_pl="DQ-18 test signal",
        payload={
            "snapshot": {"source_message": {"message_id": signal_id, "thread_id": f"thr-{signal_id}", "subject": "Pilny serwis"}},
            "intake_result_final": {"decision": {"action": "review"}, "review_required": True},
            "preclassification_result": {"lane": "review_direct"},
            "lane_stage_plan": {"run_case_linking": True},
            "context_bundle": {},
        },
        artifacts={"source": "test", "raw_observation_id": f"obs-{signal_id}"},
        revision_marker="1",
        created_by_runtime="test",
    )


def _patched_reconcile(*, case_intelligence_fails: bool, case_id: str = "case-dq18"):
    """Context manager mirroring test_signal_reconciler_runtime's degrade mock, so
    the same signal can be reconciled first as a failure and then as a success
    (retry) with a consistent case_id across calls.
    """
    fake_v2 = {
        "signal_projection": {"message_key": "msg"},
        "case_patch": {"case_id": case_id},
        "desk_note_patch": {"note_id": "note"},
        "decision_trace": {"trace_id": "trace"},
    }
    ci_kwargs = (
        {"side_effect": RuntimeError("case intelligence provider timeout")}
        if case_intelligence_fails
        else {"return_value": {"execution_metadata": {"source_mode": "model_result"}}}
    )
    return (
        # Deterministic legacy-path routing for these tests, regardless of the
        # ambient AGENT_RUNTIME_MODE the process happened to load from .env — the
        # agent_runtime.agent_reconcile.run_agent_reconcile path is exercised
        # separately in
        # tests/test_signal_reconciler_agent_pr_d.py::test_agent_path_records_case_intelligence_degradation.
        patch("agent_runtime.agent_reconcile.agent_runtime_reconcile_active", return_value=False),
        patch("gmail_intake.hydrate_intelligence_seam_config", return_value=None),
        patch(
            "gmail_intake.link_case_context",
            return_value={"selected_case_key": "case-key-dq18", "decision": "linked", "reasons": ["subject_match"]},
        ),
        patch(
            "gmail_intake.ingest_mailbox_memory",
            return_value={"case_id": case_id, "snapshot": {}, "context_pack": {}, "facts": [], "documents": [], "events": []},
        ),
        patch("gmail_intake.run_business_reasoning", return_value={"business_summary_short": "serwis"}),
        patch("gmail_intake.draft_reply", return_value={"draft_enabled": False, "drafts": []}),
        patch(
            "gmail_intake.plan_actions",
            return_value={"primary_action": "review", "safe_for_operator_projection": True, "safe_for_live_push": False},
        ),
        patch("gmail_intake.build_case_intelligence_layer", **ci_kwargs),
        patch(
            "gmail_intake.finalize_mailbox_memory",
            return_value={"case_id": case_id, "snapshot": {"status": "open"}, "context_pack": {"source_refs": []}, "next_action": {}, "facts": [], "documents": [], "events": []},
        ),
        patch(
            "policy_action_proposal.attach_policy_and_proposals",
            side_effect=lambda **kw: (None, {"proposal_id": "prop"}),
        ),
        patch("gmail_intake.build_projection_preview", return_value={"message_id": "msg", "decision_action": "review"}),
        patch(
            "projection_snapshot_transport.build_operator_projection_snapshot",
            side_effect=lambda *_a, **_k: {"v2_projection": fake_v2, "decision_view": {}},
        ),
    )


def test_live_path_records_degradation_but_processing_state_stays_reconciled() -> None:
    """DQ-18's decided answer, re-proven: core reconciliation completing and Case
    Intelligence failing are independent outcomes. `reconciled` is correct here —
    the degradation is what must now ALSO be true, not a replacement for it.
    """
    store, context = _runtime_context()
    signal = _degrading_signal("sig-dq18-a")
    context.journal.append(signal)

    patches = _patched_reconcile(case_intelligence_fails=True)
    with contextlib.ExitStack() as _stack:
        for _p in patches:
            _stack.enter_context(_p)
        result = reconcile_signal(signal, runtime_context=context, dry_run=False)

    assert result.processing_state == "reconciled"
    assert result.case_id == "case-dq18"
    assert any(w.startswith("case_intelligence_degraded:attempts=1") for w in result.warnings)

    degradation = read_case_intelligence_degradation(store, "case-dq18")
    assert degradation["degraded"] is True
    assert degradation["attempts"] == 1
    assert degradation["terminally_degraded"] is False
    assert degradation["last_degraded_signal_id"] == signal.signal_id


def test_dry_run_never_persists_degradation() -> None:
    store, context = _runtime_context()
    signal = _degrading_signal("sig-dq18-dry")
    context.journal.append(signal)

    patches = _patched_reconcile(case_intelligence_fails=True)
    with contextlib.ExitStack() as _stack:
        for _p in patches:
            _stack.enter_context(_p)
        result = reconcile_signal(signal, runtime_context=context, dry_run=True)

    assert result.processing_state == "shadowed"
    assert read_case_intelligence_degradation(store, "case-dq18")["degraded"] is False


def test_retry_clears_degradation_on_success() -> None:
    store, context = _runtime_context()
    signal = _degrading_signal("sig-dq18-b")
    context.journal.append(signal)

    fail_patches = _patched_reconcile(case_intelligence_fails=True)
    with contextlib.ExitStack() as _stack:
        for _p in fail_patches:
            _stack.enter_context(_p)
        reconcile_signal(signal, runtime_context=context, dry_run=False)

    assert read_case_intelligence_degradation(store, "case-dq18")["degraded"] is True

    ok_patches = _patched_reconcile(case_intelligence_fails=False)
    with contextlib.ExitStack() as _stack:
        for _p in ok_patches:
            _stack.enter_context(_p)
        result = retry_degraded_case_intelligence("case-dq18", runtime_context=context)

    assert result is not None
    assert result.processing_state == "reconciled"
    assert read_case_intelligence_degradation(store, "case-dq18")["degraded"] is False


def test_repeated_retry_failure_terminalizes_and_then_refuses() -> None:
    store, context = _runtime_context()
    signal = _degrading_signal("sig-dq18-c")
    context.journal.append(signal)

    fail_patches = _patched_reconcile(case_intelligence_fails=True)

    def _run_once():
        with contextlib.ExitStack() as _stack:
            for _p in fail_patches:
                _stack.enter_context(_p)
            return reconcile_signal(signal, runtime_context=context, dry_run=False)

    _run_once()  # attempt 1 (initial reconcile)
    for _ in range(DEGRADATION_MAX_ATTEMPTS - 1):
        with contextlib.ExitStack() as _stack:
            for _p in fail_patches:
                _stack.enter_context(_p)
            retried = retry_degraded_case_intelligence("case-dq18", runtime_context=context)
            assert retried is not None

    state = read_case_intelligence_degradation(store, "case-dq18")
    assert state["attempts"] == DEGRADATION_MAX_ATTEMPTS
    assert state["terminally_degraded"] is True

    # One more retry must be a documented no-op: it must not call reconcile again.
    with patch("signal_reconciler.reconcile_signal") as mock_reconcile:
        refused = retry_degraded_case_intelligence("case-dq18", runtime_context=context)
    assert refused is None
    mock_reconcile.assert_not_called()
    assert read_case_intelligence_degradation(store, "case-dq18")["attempts"] == DEGRADATION_MAX_ATTEMPTS


def test_retry_with_nothing_degraded_is_a_noop() -> None:
    store, context = _runtime_context()
    store.upsert_case({"case_id": "case-fresh", "case_key": "k", "metadata": {}})
    with patch("signal_reconciler.reconcile_signal") as mock_reconcile:
        result = retry_degraded_case_intelligence("case-fresh", runtime_context=context)
    assert result is None
    mock_reconcile.assert_not_called()


def test_staleness_guard_refuses_to_retry_a_superseded_signal() -> None:
    """A newer signal already reconciled for this case must never be overwritten
    by retrying an older, degraded one.
    """
    store, context = _runtime_context()
    signal = _degrading_signal("sig-dq18-d")
    context.journal.append(signal)

    fail_patches = _patched_reconcile(case_intelligence_fails=True)
    with contextlib.ExitStack() as _stack:
        for _p in fail_patches:
            _stack.enter_context(_p)
        reconcile_signal(signal, runtime_context=context, dry_run=False)

    assert latest_signal_id_for_case(store, "case-dq18") == signal.signal_id

    # A newer signal supersedes it.
    store.mutate_case("case-dq18", lambda row: {**row, "latest_signal_id": "sig-dq18-newer"}, create_if_missing=True)

    with patch("signal_reconciler.reconcile_signal") as mock_reconcile:
        refused = retry_degraded_case_intelligence("case-dq18", runtime_context=context)
    assert refused is None
    mock_reconcile.assert_not_called()
    # Degradation record is untouched, not silently cleared or corrupted.
    assert read_case_intelligence_degradation(store, "case-dq18")["attempts"] == 1
