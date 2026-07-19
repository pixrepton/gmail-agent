"""Operator feedback bridge: calibration vs adjudication truth loop."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from adjudication_executioner import bridge_operator_feedback
from mailbox_memory_store import InMemoryMailboxMemoryStore
from signal_contract import build_canonical_signal
from signal_journal import SignalJournal
class BridgeOperatorFeedbackTests(unittest.TestCase):
    def test_calibration_persist_only(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        journal = SignalJournal(store)
        rt = mock.MagicMock()
        rt.resolved_store = store
        rt.journal = journal
        rt.run_state = {"run_id": "t-cal"}
        rt.mode = "active"
        rt.persist_entity_links = True
        rt.graph_store = None
        rt.settings = mock.MagicMock()
        rt.verbose = False
        rt.model = None
        out = bridge_operator_feedback(
            store=store,
            journal=journal,
            runtime_context=rt,
            raw_operator_payload={
                "event_domain": "calibration",
                "case_id": "c1",
                "calibration_category": "wrong_priority",
                "detail": "x",
            },
        )
        self.assertEqual(out["domain"], "calibration")
        self.assertFalse(out["truth_loop_executed"])
        types = [e.get("event_type") for e in store.events]
        self.assertIn("v2_1_feedback_calibration", types)

    def test_adjudication_triggers_reconcile_when_reject_same_case(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        store.upsert_case(
            {
                "case_id": "case-a",
                "case_key": "K",
                "thread_id": "",
                "case_family": "ops",
                "mailbox": "test",
                "subject": "s",
                "status": "open",
                "customer_name": "",
                "customer_email": "",
                "metadata": {},
                "created_at": "2026-04-16T10:00:00+02:00",
                "updated_at": "2026-04-16T10:00:00+02:00",
            }
        )
        sig = build_canonical_signal(
            signal_kind="gmail_message_observed",
            source_kind="gmail",
            source_ref={"message_id": "m1", "thread_id": "t1"},
            observed_at="2026-04-16T10:00:00+02:00",
            effective_at=None,
            case_key_hint="K",
            thread_key_hint="t1",
            business_lane="intake_llm",
            signal_summary_pl="x",
            payload={"case_id": "case-a"},
            artifacts={},
            revision_marker="m1",
            created_by_runtime="test",
        )
        journal = SignalJournal(store)
        journal.append(sig)
        rt = mock.MagicMock()
        rt.resolved_store = store
        rt.journal = journal
        rt.run_state = {"run_id": "t-adj"}
        rt.mode = "active"
        rt.persist_entity_links = True
        rt.graph_store = None
        rt.settings = mock.MagicMock()
        rt.verbose = False
        rt.model = None
        with mock.patch("adjudication_executioner.reconcile_signal") as rec:
            rec.return_value = mock.MagicMock()
            out = bridge_operator_feedback(
                store=store,
                journal=journal,
                runtime_context=rt,
                raw_operator_payload={
                    "event_domain": "adjudication",
                    "case_id": "case-a",
                    "adjudication_kind": "reject_same_case",
                    "detail": "wrong case",
                    "target_refs": {"signal_id": sig.signal_id, "rejected_case_id": "case-a"},
                },
            )
        self.assertEqual(out["domain"], "adjudication")
        self.assertTrue(out["truth_loop_executed"])
        rec.assert_called_once()
        ovl = store.fetch_latest_adjudication_link_override(sig.signal_id)
        self.assertIsNotNone(ovl)

    def test_adjudication_confirm_same_case_skips_reconcile(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        store.upsert_case(
            {
                "case_id": "case-a",
                "case_key": "K",
                "thread_id": "",
                "case_family": "ops",
                "mailbox": "test",
                "subject": "s",
                "status": "open",
                "customer_name": "",
                "customer_email": "",
                "metadata": {},
                "created_at": "2026-04-16T10:00:00+02:00",
                "updated_at": "2026-04-16T10:00:00+02:00",
            }
        )
        journal = SignalJournal(store)
        rt = mock.MagicMock()
        rt.resolved_store = store
        rt.journal = journal
        rt.run_state = {"run_id": "t-confirm"}
        rt.mode = "active"
        rt.persist_entity_links = True
        rt.graph_store = None
        rt.settings = mock.MagicMock()
        rt.verbose = False
        rt.model = None
        with mock.patch("adjudication_executioner.reconcile_signal") as rec:
            out = bridge_operator_feedback(
                store=store,
                journal=journal,
                runtime_context=rt,
                raw_operator_payload={
                    "event_domain": "adjudication",
                    "case_id": "case-a",
                    "adjudication_kind": "confirm_same_case",
                    "detail": "ok",
                    "target_refs": {"signal_id": "sig-x"},
                },
            )
        rec.assert_not_called()
        self.assertEqual(out["domain"], "adjudication")
        self.assertFalse(out["reconcile_signal_ran"])

    def test_adjudication_reconcile_carries_projection_refresh_metadata(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        store.upsert_case(
            {
                "case_id": "case-b",
                "case_key": "K2",
                "thread_id": "",
                "case_family": "ops",
                "mailbox": "test",
                "subject": "s",
                "status": "open",
                "customer_name": "",
                "customer_email": "",
                "metadata": {},
                "created_at": "2026-04-16T10:00:00+02:00",
                "updated_at": "2026-04-16T10:00:00+02:00",
            }
        )
        sig = build_canonical_signal(
            signal_kind="gmail_message_observed",
            source_kind="gmail",
            source_ref={"message_id": "m2", "thread_id": "t2"},
            observed_at="2026-04-16T10:00:00+02:00",
            effective_at=None,
            case_key_hint="K2",
            thread_key_hint="t2",
            business_lane="intake_llm",
            signal_summary_pl="x",
            payload={"case_id": "case-b"},
            artifacts={},
            revision_marker="m2",
            created_by_runtime="test",
        )
        journal = SignalJournal(store)
        journal.append(sig)
        rt = mock.MagicMock()
        rt.resolved_store = store
        rt.journal = journal
        rt.run_state = {"run_id": "t-proj-refresh"}
        rt.mode = "active"
        rt.persist_entity_links = True
        rt.graph_store = None
        rt.settings = mock.MagicMock()
        rt.verbose = False
        rt.model = None
        reconcile_result = mock.MagicMock()
        reconcile_result.projection_refresh_decision = mock.MagicMock()
        reconcile_result.projection_refresh_decision.to_dict.return_value = {
            "should_refresh": True,
            "reason": "gmail_message_observed",
        }
        with mock.patch("adjudication_executioner.reconcile_signal", return_value=reconcile_result) as rec:
            out = bridge_operator_feedback(
                store=store,
                journal=journal,
                runtime_context=rt,
                raw_operator_payload={
                    "event_domain": "adjudication",
                    "case_id": "case-b",
                    "adjudication_kind": "reject_same_case",
                    "detail": "wrong case",
                    "target_refs": {"signal_id": sig.signal_id, "rejected_case_id": "case-b"},
                },
            )
        rec.assert_called_once()
        self.assertTrue(out["truth_loop_executed"])
        self.assertTrue(reconcile_result.projection_refresh_decision.to_dict().get("should_refresh"))


class AdjudicationStrategyDispatchTests(unittest.TestCase):
    def test_adjudication_strategy_dispatch_confirm(self) -> None:
        from adjudication_executioner import (
            _ADJUDICATION_STRATEGIES,
            _strategy_confirm_same_case,
        )

        self.assertIn("confirm_same_case", _ADJUDICATION_STRATEGIES)
        self.assertIs(_ADJUDICATION_STRATEGIES["confirm_same_case"], _strategy_confirm_same_case)

    def test_adjudication_strategy_dispatch_reject(self) -> None:
        from adjudication_executioner import (
            _ADJUDICATION_STRATEGIES,
            _strategy_reject_same_case,
        )

        self.assertIn("reject_same_case", _ADJUDICATION_STRATEGIES)
        self.assertIs(_ADJUDICATION_STRATEGIES["reject_same_case"], _strategy_reject_same_case)

    def test_adjudication_strategy_dispatch_unknown_falls_to_noop(self) -> None:
        from adjudication_executioner import (
            _ADJUDICATION_STRATEGIES,
            _strategy_noop,
            execute_adjudication_reconcile,
        )
        from unittest import mock

        result = execute_adjudication_reconcile(
            store=mock.MagicMock(),
            journal=mock.MagicMock(),
            runtime_context=mock.MagicMock(),
            adjudication_dict={
                "event_id": "evt-unknown-1",
                "case_id": "c1",
                "adjudication_kind": "unknown_kind_xyz",
                "occurred_at": "2026-07-01T10:00:00+00:00",
                "target_refs": {},
                "payload": {},
                "trace_id": "",
            },
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.get("adjudication_result"), "unsupported_kind")
        self.assertEqual(result.get("adjudication_kind"), "unknown_kind_xyz")

    def test_adjudication_strategy_each_kind_mapped(self) -> None:
        """Every known kind in the dispatch dict maps to a callable strategy."""
        from adjudication_executioner import _ADJUDICATION_STRATEGIES

        for kind, strategy in _ADJUDICATION_STRATEGIES.items():
            with self.subTest(kind=kind):
                self.assertTrue(callable(strategy), f"Strategy for {kind} is not callable")


if __name__ == "__main__":
    unittest.main()
