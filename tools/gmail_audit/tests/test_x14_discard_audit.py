"""X14: audit trail for skip / reference_only discard decisions.

Two independent active discard gates exist in the active (SIGNAL_RUNTIME_MODE=active)
Gmail pipeline (see also test_intake_noise_01.py, which fixed a false-positive on one
of them):

  - preclassifier.preclassify_snapshot (lane=skip / reference_only), whose decision
    reaches the durable CanonicalSignal (business_lane, payload.preclassification_result,
    artifacts.triage_result) before reconcile ever runs.
  - agent_runtime.agent_reconcile._evaluate_cost_gate (ReconcileResult.warnings,
    e.g. "cost_gate_skip:spam_indicator:reklama"), invoked *inside* reconcile.

RED-1/RED-2 show that both gates' *reasons* are durably persisted only in coarse
form: you can tell a signal was discarded and roughly by which lane, but not which
specific keyword/pattern/check fired — the same class of ambiguity that let the
reklama/reklamacja collision (INTAKE-NOISE-01) go undetected. RED-1c shows the
cost gate's ReconcileResult.warnings (which already carries a precise reason) is
silently dropped by reconcile_signal's durable processing-attempt write.

RED-3 proves audit/signal identity is already anchored on the stable signal_id /
idempotency_key, not on an ephemeral trace_id — no fix required there.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.signal_registry import SIGNAL_HANDLERS
from gmail_signal_adapter import build_gmail_raw_observation, build_gmail_signals
from mailbox_memory_store import InMemoryMailboxMemoryStore
from observation_triage import triage_gmail_observation
from preclassifier import preclassify_snapshot
from signal_contract import build_canonical_signal
from signal_journal import SignalJournal
from signal_reconciler import ReconcileResult, SignalRuntimeContext, reconcile_signal


def _runtime_context(store: InMemoryMailboxMemoryStore) -> SignalRuntimeContext:
    settings = SimpleNamespace(
        mailbox_memory_blob_root=Path(tempfile.gettempdir()) / "x14-discard-audit-tests",
        signal_journal_jsonl_mirror_enabled=False,
        signal_runtime_mode="active",
        groq_model="test-model",
    )
    return SignalRuntimeContext(
        settings=settings,
        journal=SignalJournal(store),
        store=store,
        graph_store=None,
        run_state={},
        model="test-model",
        verbose=False,
        mode="active",
    )


def _noise_snapshot() -> dict:
    """Real marketing noise: standalone 'reklama' in subject (subject-keyword branch)."""
    return {
        "mailbox": "ops@example.com",
        "observed_at": "2026-07-10T09:00:00+02:00",
        "source_message": {
            "message_id": "msg-x14-noise-1",
            "thread_id": "thr-x14-noise-1",
            "history_id": "5001",
            "date": "2026-07-10T08:59:00+02:00",
            "subject": "Reklama nowej promocji sezonowej",
            "sender": "Dzial Marketingu",
            "sender_email": "marketing@example.com",
            "body": "Sprawdz nasza najnowsza reklame i skorzystaj z promocji.",
            "snippet": "Sprawdz nasza najnowsza reklame.",
        },
    }


def _reference_only_snapshot() -> dict:
    """Informational delivery confirmation, no question / no request language."""
    return {
        "mailbox": "ops@example.com",
        "observed_at": "2026-07-10T10:00:00+02:00",
        "source_message": {
            "message_id": "msg-x14-refonly-1",
            "thread_id": "thr-x14-refonly-1",
            "history_id": "5002",
            "date": "2026-07-10T09:59:00+02:00",
            "subject": "Potwierdzenie dostawy zamowienia 4471",
            "sender": "Kurier",
            "sender_email": "tracking@kurier.example.com",
            "body": "Potwierdzenie dostawy zamowienia 4471. Dostawa zrealizowana dzisiaj.",
            "snippet": "Dostawa zrealizowana.",
        },
    }


def _business_signal_snapshot() -> dict:
    """Genuine complaint containing 'reklamacja' — must not be discarded by either gate."""
    return {
        "mailbox": "ops@example.com",
        "observed_at": "2026-07-10T11:00:00+02:00",
        "source_message": {
            "message_id": "msg-x14-business-1",
            "thread_id": "thr-x14-business-1",
            "history_id": "5003",
            "date": "2026-07-10T10:59:00+02:00",
            "subject": "Reklamacja montazu klimatyzacji",
            "sender": "Jan Kowalski",
            "sender_email": "jan.kowalski@example.com",
            "body": "Prosze o kontakt w sprawie reklamacji montazu, instalacja nie dziala poprawnie.",
            "snippet": "Prosze o kontakt w sprawie reklamacji.",
        },
    }


def _durable_signal_for(snapshot: dict, store: InMemoryMailboxMemoryStore) -> tuple[dict, dict]:
    """Run the real active-path building blocks and return (fetched_signal_row, triage_result)."""
    raw_observation = build_gmail_raw_observation(snapshot=snapshot, created_by_runtime="test")
    triage_result = triage_gmail_observation(raw_observation)
    preclassification_result = triage_result["preclassification"]
    signals = build_gmail_signals(
        snapshot=snapshot,
        intake_result_final={"decision": {"action": "n/a"}},
        preclassification_result=preclassification_result,
        lane_stage_plan={"lane": preclassification_result["lane"]},
        context_bundle={},
        raw_observation=raw_observation,
        triage_result=triage_result,
        created_by_runtime="test",
    )
    journal = SignalJournal(store)
    result = journal.append(signals[0])
    fetched = store.fetch_signal(result.signal.signal_id)
    assert fetched is not None
    return fetched, triage_result


class SkipLaneReasonGranularityTests(unittest.TestCase):
    """RED-1: durable skip-lane audit must reveal which rule matched, not just a coarse code."""

    def test_preclassify_result_reasons_expose_matched_subject_keyword(self) -> None:
        result = preclassify_snapshot(_noise_snapshot())
        self.assertEqual(result["lane"], "skip")
        self.assertTrue(
            any("reklama" in reason for reason in result["reasons"]),
            f"reasons do not reveal which keyword matched: {result['reasons']}",
        )

    def test_durable_signal_payload_reveals_matched_subject_keyword(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        fetched, triage_result = _durable_signal_for(_noise_snapshot(), store)

        self.assertEqual(fetched["business_lane"], "skip")
        payload_reasons = fetched["payload_json"]["preclassification_result"]["reasons"]
        self.assertTrue(
            any("reklama" in reason for reason in payload_reasons),
            f"durable payload reasons do not reveal matched keyword: {payload_reasons}",
        )
        artifact_reason_codes = fetched["artifacts_json"]["triage_result"]["reason_codes"]
        self.assertTrue(
            any("reklama" in reason for reason in artifact_reason_codes),
            f"durable artifact reason_codes do not reveal matched keyword: {artifact_reason_codes}",
        )

    def test_preclassify_result_stamps_deciding_gate(self) -> None:
        result = preclassify_snapshot(_noise_snapshot())
        self.assertEqual(
            result.get("stage_name"), "preclassifier",
            f"result does not identify which gate decided: {result}",
        )


class ReferenceOnlyReasonGranularityTests(unittest.TestCase):
    """RED-2: reference_only lane has the same granularity gap as skip."""

    def test_preclassify_result_reasons_expose_matched_pattern(self) -> None:
        result = preclassify_snapshot(_reference_only_snapshot())
        self.assertEqual(result["lane"], "reference_only")
        self.assertTrue(
            any("potwierdzenie" in reason for reason in result["reasons"]),
            f"reasons do not reveal which reference pattern matched: {result['reasons']}",
        )

    def test_durable_signal_payload_reveals_matched_pattern(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        fetched, _triage_result = _durable_signal_for(_reference_only_snapshot(), store)

        self.assertEqual(fetched["business_lane"], "reference_only")
        payload_reasons = fetched["payload_json"]["preclassification_result"]["reasons"]
        self.assertTrue(
            any("potwierdzenie" in reason for reason in payload_reasons),
            f"durable payload reasons do not reveal matched pattern: {payload_reasons}",
        )


class CostGateWarningDroppedFromProcessingAttemptTests(unittest.TestCase):
    """RED-1c: reconcile_signal must not silently drop ReconcileResult.warnings.

    _evaluate_cost_gate (agent_runtime/agent_reconcile.py) already computes a precise
    machine-readable reason (e.g. "cost_gate_skip:spam_indicator:reklama") and stores it
    on ReconcileResult.warnings. reconcile_signal() calls
    journal.record_processing_attempt(..., details={"case_id":…, "projection_refresh":…})
    on every signal, for every source_kind — but does not include warnings, so the
    only durable per-signal processing history (mailbox_memory_signal_processing_attempts)
    never captures *why* a cost-gate (or any other handler-level) skip happened.
    """

    TEST_SOURCE_KIND = "x14_test_cost_gate_source"

    def _register_fake_handler(self, warnings: list[str]) -> None:
        def _fake_handler(signal, *, runtime_context, dry_run, entity_link_dict):
            return ReconcileResult(
                signal_id=signal.signal_id,
                source_kind=signal.source_kind,
                signal_kind=signal.signal_kind,
                processing_state="reconciled",
                warnings=list(warnings),
            )

        SIGNAL_HANDLERS[self.TEST_SOURCE_KIND] = _fake_handler

    def tearDown(self) -> None:
        SIGNAL_HANDLERS.pop(self.TEST_SOURCE_KIND, None)

    def test_dropped_cost_gate_reason_is_not_recoverable_from_processing_attempts(self) -> None:
        self._register_fake_handler(["cost_gate_skip:spam_indicator:reklama"])
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        context = _runtime_context(store)
        signal = build_canonical_signal(
            signal_kind="x14_test_signal_observed",
            source_kind=self.TEST_SOURCE_KIND,
            source_ref={"message_id": "msg-x14-costgate-1"},
            observed_at="2026-07-10T12:00:00+02:00",
            signal_summary_pl="Test signal for X14 cost-gate audit gap",
            payload={},
            created_by_runtime="test",
        )
        context.journal.append(signal)

        reconcile_signal(signal, runtime_context=context, dry_run=False)

        attempts = store.fetch_signal_processing_attempts(signal.signal_id)
        self.assertTrue(attempts, "expected at least one durable processing attempt")
        all_details = [dict(attempt.get("details_json") or attempt.get("details") or {}) for attempt in attempts]
        found = any(
            "cost_gate_skip:spam_indicator:reklama" in str(details)
            for details in all_details
        )
        self.assertTrue(
            found,
            "cost-gate warning must be recoverable from the durable processing-attempt "
            f"record, found only: {all_details}",
        )


class BusinessSignalIsNotFalselyDiscardedTests(unittest.TestCase):
    """Guardrail: a real complaint containing 'reklamacja' must not carry a discard audit."""

    def test_reklamacja_signal_is_not_skip_or_reference_only(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        fetched, _triage_result = _durable_signal_for(_business_signal_snapshot(), store)
        self.assertNotIn(fetched["business_lane"], {"skip", "reference_only"})


class SignalIdentityStabilityTests(unittest.TestCase):
    """RED-3 (proof, not a fix): dedupe is anchored on idempotency_key/signal_id, not trace_id."""

    def test_reprocessing_same_message_under_different_trace_id_dedupes_by_signal_identity(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        snapshot = _noise_snapshot()

        raw_observation = build_gmail_raw_observation(snapshot=snapshot, created_by_runtime="test")
        triage_result = triage_gmail_observation(raw_observation)
        preclassification_result = triage_result["preclassification"]
        signals_run1 = build_gmail_signals(
            snapshot=snapshot,
            intake_result_final={"decision": {"action": "n/a"}},
            preclassification_result=preclassification_result,
            lane_stage_plan={"lane": preclassification_result["lane"]},
            context_bundle={},
            raw_observation=raw_observation,
            triage_result=triage_result,
            created_by_runtime="run-1-trace-aaa",
        )
        signals_run2 = build_gmail_signals(
            snapshot=snapshot,
            intake_result_final={"decision": {"action": "n/a"}},
            preclassification_result=preclassification_result,
            lane_stage_plan={"lane": preclassification_result["lane"]},
            context_bundle={},
            raw_observation=raw_observation,
            triage_result=triage_result,
            created_by_runtime="run-2-trace-zzz",
        )

        journal = SignalJournal(store)
        first = journal.append(signals_run1[0])
        second = journal.append(signals_run2[0])

        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(second.duplicate_of_signal_id, first.signal.signal_id)
        self.assertEqual(signals_run1[0].idempotency_key, signals_run2[0].idempotency_key)


if __name__ == "__main__":
    unittest.main()
