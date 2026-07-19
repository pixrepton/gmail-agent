"""V2.1 sprint: CaseSnapshotHotState + FeedbackEvent / AdjudicationEvent split."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from adjudication_executioner import (
    append_reject_same_case_override,
    execute_adjudication_reconcile,
    persist_and_execute_adjudication_truth_loop,
)
from case_snapshot_hot_state_contract import (
    CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION,
    validate_case_snapshot_hot_state,
)
from case_intelligence import apply_hot_state_to_case_intelligence
from case_snapshot_manager import CaseSnapshotManager
from dash_projection_v2 import build_v2_shadow_projection
from entity_linker import EntityLinker
from feedback_event_contract import (
    AdjudicationEvent,
    FeedbackEvent,
    validate_adjudication_event,
    validate_feedback_event,
)
from mailbox_memory_store import InMemoryMailboxMemoryStore
from operator_feedback_runtime import (
    calibration_cannot_mutate_truth,
    persist_routed_event,
    route_operator_payload,
)
from signal_contract import build_canonical_signal
from signal_journal import SignalJournal


class HotStateContractTests(unittest.TestCase):
    def test_hot_state_schema_validate_examples_file(self) -> None:
        path = TOOL_DIR / "examples" / "hot_state_feedback_examples.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        hs = data["case_snapshot_hot_state_example"]
        self.assertEqual(hs["schema_version"], CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION)
        errs = validate_case_snapshot_hot_state(hs)
        self.assertEqual(errs, [], errs)

    def test_append_only_versions_and_cold_pointers(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        store.upsert_case(
            {
                "case_id": "c1",
                "case_key": "k1",
                "thread_id": "",
                "case_family": "ops",
                "mailbox": "drive",
                "subject": "x",
                "status": "open",
                "customer_name": "A",
                "customer_email": "a@b.c",
                "metadata": {},
                "created_at": "2026-04-16T10:00:00+02:00",
                "updated_at": "2026-04-16T10:00:00+02:00",
            }
        )
        mgr = CaseSnapshotManager(store=store)
        sig = build_canonical_signal(
            signal_kind="drive_document_added",
            source_kind="drive",
            source_ref={"file_id": "f1", "change_id": "c1", "revision_id": "r1", "modified_time": "2026-04-16T10:01:00+02:00"},
            observed_at="2026-04-16T10:01:00+02:00",
            effective_at="2026-04-16T10:01:00+02:00",
            case_key_hint="k1",
            thread_key_hint="k1",
            business_lane="ops",
            signal_summary_pl="Doc",
            payload={"case_id": "c1", "case_key": "k1"},
            artifacts={"raw_observation_id": "ro1"},
            revision_marker="r1",
            created_by_runtime="test",
        )
        hot = mgr.apply_signal(sig, case_id_override="c1", trace_id="t1")
        self.assertEqual(hot["schema_version"], CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION)
        self.assertIn("raw_observation_ids", hot["cold_evidence_pointers"])
        self.assertTrue(hot["cold_evidence_pointers"]["raw_observation_ids"])
        vers = store.fetch_case_snapshot_versions("c1")
        self.assertEqual(len(vers), 1)
        self.assertEqual(vers[0]["version"], 1)

    def test_conflicts_not_flattened(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        store.upsert_case(
            {
                "case_id": "c2",
                "case_key": "k2",
                "thread_id": "",
                "case_family": "ops",
                "mailbox": "drive",
                "subject": "x",
                "status": "open",
                "customer_name": "",
                "customer_email": "",
                "metadata": {},
                "created_at": "2026-04-16T10:00:00+02:00",
                "updated_at": "2026-04-16T10:00:00+02:00",
            }
        )
        store.append_fact_rows(
            [
                {
                    "fact_id": "f1",
                    "case_id": "c2",
                    "message_id": "",
                    "document_id": "",
                    "entity_scope": "case",
                    "fact_key": "city",
                    "normalized_value": "A",
                    "raw_value": "A",
                    "confidence": 0.9,
                    "observed_at": "2026-04-16T10:00:00+02:00",
                    "source_type": "test",
                    "source_ref": "r1",
                    "status": "active",
                    "metadata": {},
                },
                {
                    "fact_id": "f2",
                    "case_id": "c2",
                    "message_id": "",
                    "document_id": "",
                    "entity_scope": "case",
                    "fact_key": "city",
                    "normalized_value": "B",
                    "raw_value": "B",
                    "confidence": 0.85,
                    "observed_at": "2026-04-16T10:00:01+02:00",
                    "source_type": "test",
                    "source_ref": "r2",
                    "status": "active",
                    "metadata": {},
                },
            ]
        )
        mgr = CaseSnapshotManager(store=store)
        sig = build_canonical_signal(
            signal_kind="drive_document_added",
            source_kind="drive",
            source_ref={"file_id": "f2", "change_id": "c2", "revision_id": "r2", "modified_time": "2026-04-16T10:02:00+02:00"},
            observed_at="2026-04-16T10:02:00+02:00",
            effective_at="2026-04-16T10:02:00+02:00",
            case_key_hint="k2",
            thread_key_hint="k2",
            business_lane="ops",
            signal_summary_pl="X",
            payload={"case_id": "c2"},
            artifacts={"raw_observation_id": "ro2"},
            revision_marker="r2",
            created_by_runtime="test",
        )
        hot = mgr.apply_signal(sig, case_id_override="c2")
        self.assertTrue(hot["active_conflicts"])
        self.assertTrue(any("city" in str(c.get("fact_key")) for c in hot["active_conflicts"]))


class IntelligenceHotStateOverlayTests(unittest.TestCase):
    def test_apply_hot_state_merges_primary_flags(self) -> None:
        intel = {"case_understanding": {"summary_short": "legacy"}, "execution_metadata": {}}
        hot = {
            "schema_version": CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION,
            "snapshot_id": "snap1",
            "case": {"case_id": "c1", "summary_text": "hot summary text", "operational_status": "active"},
            "active_conflicts": [],
            "key_facts": [],
            "open_loops": [],
            "recommended_next_step": "review",
            "cold_evidence_pointers": {},
        }
        out = apply_hot_state_to_case_intelligence(intel, hot)
        self.assertTrue(out["case_understanding"]["case_snapshot_hot_state_primary"])
        self.assertEqual(out["case_understanding"]["summary_short"], "hot summary text"[:200])
        self.assertEqual(out["execution_metadata"]["hot_state_snapshot_id"], "snap1")


class FeedbackSplitTests(unittest.TestCase):
    def test_feedback_and_adjudication_validation(self) -> None:
        fe = FeedbackEvent(
            event_id="fb1",
            occurred_at="2026-04-16T11:00:00+02:00",
            case_id="c1",
            calibration_category="accurate",
            detail="ok",
        )
        self.assertFalse(validate_feedback_event(fe.to_dict()))
        ae = AdjudicationEvent(
            event_id="ad1",
            occurred_at="2026-04-16T11:01:00+02:00",
            case_id="c1",
            adjudication_kind="resolve_conflict",
            detail="pick A",
        )
        self.assertFalse(validate_adjudication_event(ae.to_dict()))

    def test_route_and_persist_separate_streams(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        dom_c, cal = route_operator_payload(
            {
                "event_domain": "calibration",
                "case_id": "cx",
                "calibration_category": "wrong_priority",
                "detail": "too high",
            }
        )
        self.assertEqual(dom_c, "calibration")
        self.assertEqual(cal.get("event_class"), "FeedbackEvent")
        persist_routed_event(store, "calibration", cal)
        dom_a, adj = route_operator_payload(
            {
                "event_domain": "adjudication",
                "case_id": "cx",
                "adjudication_kind": "invalidate_fact",
                "detail": "drop city fact",
            }
        )
        self.assertEqual(dom_a, "adjudication")
        self.assertEqual(adj.get("event_class"), "AdjudicationEvent")
        persist_routed_event(store, "adjudication", adj)
        types = [e.get("event_type") for e in store.events]
        self.assertIn("v2_1_feedback_calibration", types)
        self.assertIn("v2_1_adjudication", types)
        self.assertTrue(calibration_cannot_mutate_truth(FeedbackEvent(event_id="x", occurred_at="2026-04-16T12:00:00+02:00", case_id="c")))


class TruthLoopAdjudicationTests(unittest.TestCase):
    def test_wrong_case_adjudication_yields_conflict_hot_state_and_pending_entity_link(self) -> None:
        nip = "5252440985"
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        store.upsert_case(
            {
                "case_id": "case-a",
                "case_key": "K-A",
                "thread_id": "",
                "case_family": "lead_opportunity",
                "mailbox": "test",
                "subject": "Test",
                "status": "open",
                "customer_name": "",
                "customer_email": "",
                "metadata": {"nip": nip},
                "created_at": "2026-04-16T10:00:00+02:00",
                "updated_at": "2026-04-16T10:00:00+02:00",
            }
        )
        signal = build_canonical_signal(
            signal_kind="gmail_message_observed",
            source_kind="gmail",
            source_ref={"message_id": "m-wrong-case", "thread_id": "t-wrong"},
            observed_at="2026-04-16T10:00:00+02:00",
            effective_at=None,
            case_key_hint="K-A",
            thread_key_hint="t-wrong",
            business_lane="intake_llm",
            signal_summary_pl="Lead mail",
            payload={
                "snapshot": {},
                "intake_result_final": {"extracted_data": {"references": {}}},
                "case_hints": {"nip": nip},
                "case_id": "case-a",
            },
            artifacts={},
            revision_marker="m-wrong-case",
            created_by_runtime="test",
        )
        journal = SignalJournal(store)
        journal.append(signal)

        mgr = CaseSnapshotManager(store=store)
        hot_before = mgr.apply_signal(signal, case_id_override="case-a", trace_id="t0")
        self.assertNotEqual(hot_before["case"]["operational_status"], "CONFLICT")

        append_reject_same_case_override(
            store,
            signal_id=signal.signal_id,
            rejected_case_id="case-a",
            adjudication_event_id="adj-wrong-case-1",
            trace_id="trace-proof",
        )

        link = EntityLinker(store).find_case(signal)
        self.assertEqual(link.link_status, "PENDING_ADJUDICATION")
        self.assertEqual(link.phase, "adjudication")

        hot_after = mgr.apply_signal(signal, case_id_override="case-a", trace_id="t1")
        self.assertEqual(hot_after["case"]["operational_status"], "CONFLICT")
        self.assertGreaterEqual(int(hot_after.get("version") or 0), 2)
        self.assertIn("Adjudication conflict", " ".join(hot_after.get("open_loops") or []))

    def test_execute_adjudication_reconcile_runs_signal_reconciler(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        store.upsert_case(
            {
                "case_id": "case-x",
                "case_key": "KX",
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
            signal_kind="drive_document_added",
            source_kind="drive",
            source_ref={"file_id": "fx", "change_id": "cx", "revision_id": "rx", "modified_time": "2026-04-16T10:00:00+02:00"},
            observed_at="2026-04-16T10:00:00+02:00",
            effective_at="2026-04-16T10:00:00+02:00",
            case_key_hint="KX",
            thread_key_hint="KX",
            business_lane="ops",
            signal_summary_pl="Doc",
            payload={"case_id": "case-x", "case_key": "KX"},
            artifacts={"raw_observation_id": "rox"},
            revision_marker="rx",
            created_by_runtime="test",
        )
        journal = SignalJournal(store)
        journal.append(sig)
        rt = mock.MagicMock()
        rt.resolved_store = store
        rt.journal = journal
        rt.run_state = {"run_id": "run-exec-test"}
        rt.mode = "active"
        rt.persist_entity_links = True
        rt.graph_store = None
        rt.settings = mock.MagicMock()
        rt.verbose = False
        rt.model = None
        with mock.patch("adjudication_executioner.reconcile_signal") as rec:
            rec.return_value = mock.MagicMock(case_id="case-x")
            out = execute_adjudication_reconcile(
                store=store,
                journal=journal,
                runtime_context=rt,
                adjudication_dict={
                    "event_id": "adj-exec-1",
                    "occurred_at": "2026-04-16T12:00:00+02:00",
                    "case_id": "case-x",
                    "adjudication_kind": "reject_same_case",
                    "detail": "wrong case",
                    "target_refs": {"signal_id": sig.signal_id, "rejected_case_id": "case-x"},
                    "payload": {},
                },
            )
        self.assertIsNotNone(out)
        rec.assert_called_once()
        ov = store.fetch_latest_adjudication_link_override(sig.signal_id)
        self.assertIsNotNone(ov)
        self.assertEqual(ov.get("override_kind"), "reject_same_case")

    def test_execute_adjudication_reconcile_merges_case_id_for_auxiliary_gmail_signal(self) -> None:
        """Desk bridge carries case_id; thread/attachment signals often omit it — needed for projection refresh."""
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        store.upsert_case(
            {
                "case_id": "case-bridge",
                "case_key": "KBR",
                "thread_id": "th1",
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
            signal_kind="gmail_thread_update_observed",
            source_kind="gmail",
            source_ref={"mailbox": "test", "thread_id": "th1", "message_id": "mid1", "history_id": "h1"},
            observed_at="2026-04-16T10:00:00+02:00",
            effective_at="2026-04-16T10:00:00+02:00",
            case_key_hint="KBR",
            thread_key_hint="th1",
            business_lane="ops",
            signal_summary_pl="thread",
            payload={"thread_id": "th1", "message_id": "mid1"},
            artifacts={},
            revision_marker="h1:th1",
            created_by_runtime="test",
        )
        journal = SignalJournal(store)
        journal.append(sig)
        rt = mock.MagicMock()
        rt.resolved_store = store
        rt.journal = journal
        rt.run_state = {"run_id": "run-bridge-hint"}
        rt.mode = "active"
        rt.persist_entity_links = True
        rt.graph_store = None
        rt.settings = mock.MagicMock()
        rt.verbose = False
        rt.model = None
        with mock.patch("adjudication_executioner.reconcile_signal") as rec:
            rec.return_value = mock.MagicMock(processing_state="reconciled", case_id="case-bridge")
            execute_adjudication_reconcile(
                store=store,
                journal=journal,
                runtime_context=rt,
                adjudication_dict={
                    "event_id": "adj-bridge-hint",
                    "occurred_at": "2026-04-16T12:00:00+02:00",
                    "case_id": "case-bridge",
                    "adjudication_kind": "reject_same_case",
                    "detail": "wrong case",
                    "target_refs": {"signal_id": sig.signal_id, "rejected_case_id": "case-bridge"},
                    "payload": {},
                },
            )
        rec.assert_called_once()
        passed_signal = rec.call_args[0][0]
        self.assertEqual(str(passed_signal.payload.get("case_id")), "case-bridge")

    def test_persist_and_execute_truth_loop_persists_v2_1_and_runs_reconcile(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        store.upsert_case(
            {
                "case_id": "case-pe",
                "case_key": "KPE",
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
            signal_kind="drive_document_added",
            source_kind="drive",
            source_ref={"file_id": "fpe", "change_id": "cpe", "revision_id": "rpe", "modified_time": "2026-04-16T10:00:00+02:00"},
            observed_at="2026-04-16T10:00:00+02:00",
            effective_at="2026-04-16T10:00:00+02:00",
            case_key_hint="KPE",
            thread_key_hint="KPE",
            business_lane="ops",
            signal_summary_pl="Doc",
            payload={"case_id": "case-pe", "case_key": "KPE"},
            artifacts={"raw_observation_id": "rope"},
            revision_marker="rpe",
            created_by_runtime="test",
        )
        journal = SignalJournal(store)
        journal.append(sig)
        rt = mock.MagicMock()
        rt.resolved_store = store
        rt.journal = journal
        rt.run_state = {"run_id": "run-pe"}
        rt.mode = "active"
        rt.persist_entity_links = True
        rt.graph_store = None
        rt.settings = mock.MagicMock()
        rt.verbose = False
        rt.model = None
        raw = {
            "event_domain": "adjudication",
            "event_id": "adj-pe-1",
            "occurred_at": "2026-04-16T12:00:00+02:00",
            "case_id": "case-pe",
            "adjudication_kind": "reject_same_case",
            "detail": "wrong case",
            "target_refs": {"signal_id": sig.signal_id, "rejected_case_id": "case-pe"},
        }
        with mock.patch("adjudication_executioner.reconcile_signal") as rec:
            rec.return_value = mock.MagicMock(case_id="case-pe")
            eid, out = persist_and_execute_adjudication_truth_loop(
                store=store,
                journal=journal,
                runtime_context=rt,
                raw_operator_payload=raw,
            )
        self.assertEqual(eid, "adj-pe-1")
        self.assertIsNotNone(out)
        rec.assert_called_once()
        types = [e.get("event_type") for e in store.events]
        self.assertIn("v2_1_adjudication", types)


class V2ProjectionHotStatePrimaryTests(unittest.TestCase):
    def test_v2_case_and_desk_summary_pl_prefers_hot_state_text(self) -> None:
        intake = {
            "schema_version": "1.0",
            "source": {"channel": "gmail", "mailbox": "m", "observed_at": "2026-01-01T00:00:00"},
            "message": {
                "message_id": "mid-hot",
                "date": "2026-01-01",
                "sender": "a@b.c",
                "subject": "Sub",
                "snippet": "sn",
                "has_attachments": False,
            },
            "thread": {"thread_id": "tid", "thread_position": "latest", "is_reply_or_forward": False},
            "business_area": "sales",
            "primary_signal": {"code": "x", "name": "X", "description": "d", "business_significance": "b"},
            "case_assessment": {
                "case_family": "lead_opportunity",
                "is_new_case": True,
                "state_detected": "new",
                "state_change": {"detected": False},
            },
            "decision": {"action": "create_case", "action_rationale": "r"},
            "priority": "medium",
            "confidence": {
                "signal_confidence": 0.8,
                "case_link_confidence": 0.7,
                "decision_confidence": 0.7,
                "extraction_confidence": 0.7,
            },
            "review": {"required": False, "flags": []},
            "reason": "reason",
            "extracted_data": {"entities": {}, "dates": [], "amounts": [], "references": {}, "deadlines": []},
        }
        intel = {
            "case_understanding": {
                "case_id": "case_x",
                "business_priority": "medium",
                "attention_reason": "a",
                "blockers": [],
                "review_required": False,
                "review_flags": [],
            },
            "operator_brief": {"brief_pl": "brief"},
            "next_best_action": {
                "primary_next_action": {
                    "action_type": "prepare_offer",
                    "title_pl": "Przygotuj ofertę",
                    "reason_pl": "offer reason",
                    "urgency_level": "normal",
                    "confidence": 0.6,
                    "whether_human_review_required": False,
                    "suggested_channel": "internal",
                    "optional_draft_pointer": "",
                },
                "secondary_actions": [],
            },
            "missing_info": {"summary_pl": "m", "critical": [], "important": [], "helpful": []},
            "risk_assessment": {"summary_pl": "r", "risks": []},
            "merge_split_suggestions": {"summary_pl": "", "merge_candidates": [], "split_suspicions": []},
            "desk_composition": {
                "should_surface": True,
                "presence_mode": "advisory",
                "surface_zone": "desk",
                "day_bucket": "dzisiaj",
                "title_pl": "t",
                "body_short_pl": "legacy desk body should not win when hot state exists",
                "body_reason_pl": "br",
                "assistant_suggestion_pl": "as",
                "visibility_score": 0.6,
                "lifecycle_intent": "create",
                "review_required": False,
                "trace_summary": "",
            },
            "lifecycle_revision": {
                "lifecycle_intent": "create",
                "target_presence_mode": "advisory",
                "target_surface_zone": "desk",
                "reason_pl": "lr",
                "should_create": True,
                "should_update": False,
            },
            "feedback_learning_memory": {
                "explicit_signals": [],
                "implicit_signals": [],
                "preference_biases": [],
                "suppression_hints": [],
                "tone_hint_pl": "",
                "emphasis_hint_pl": "",
            },
            "case_guidance": {},
            "attachment_intelligence": {},
            "thread_memory": {},
            "review_routing": {},
            "automation_policy": {},
        }
        proj = build_v2_shadow_projection(
            intake,
            run_id="hot-primary-test",
            stage_outputs={
                "preclassification_result": {"lane": "intake_llm"},
                "case_link_result": {"decision": "linked", "selected_case_key": "k", "reasons": []},
                "action_plan_result": {"primary_action": "prepare_reply", "safe_for_live_push": False},
                "business_reasoning_result": {},
                "case_intelligence_result": intel,
                "mailbox_memory_result": {
                    "context_pack": {"snapshot": {}},
                    "case_snapshot_hot_state": {
                        "schema_version": CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION,
                        "case": {
                            "case_id": "case_x",
                            "summary_text": "HOT_STATE_PRIMARY_SUMMARY_FOR_UI",
                            "operational_status": "active",
                        },
                    },
                },
            },
        )
        self.assertEqual(proj["case_patch"]["summary_pl"], "HOT_STATE_PRIMARY_SUMMARY_FOR_UI")
        self.assertEqual(proj["desk_note_patch"]["summary_pl"], "HOT_STATE_PRIMARY_SUMMARY_FOR_UI")

    def test_v2_projection_prefers_hot_state_arrays_over_compat_snapshot(self) -> None:
        intake = {
            "schema_version": "1.0",
            "source": {"channel": "gmail", "mailbox": "m", "observed_at": "2026-01-01T00:00:00"},
            "message": {
                "message_id": "mid-hot-arrays",
                "date": "2026-01-01",
                "sender": "a@b.c",
                "subject": "Sub",
                "snippet": "sn",
                "has_attachments": False,
            },
            "thread": {"thread_id": "tid", "thread_position": "latest", "is_reply_or_forward": False},
            "business_area": "sales",
            "primary_signal": {"code": "x", "name": "X", "description": "d", "business_significance": "b"},
            "case_assessment": {
                "case_family": "lead_opportunity",
                "is_new_case": True,
                "state_detected": "new",
                "state_change": {"detected": False},
            },
            "decision": {"action": "create_case", "action_rationale": "r"},
            "priority": "medium",
            "confidence": {
                "signal_confidence": 0.8,
                "case_link_confidence": 0.7,
                "decision_confidence": 0.7,
                "extraction_confidence": 0.7,
            },
            "review": {"required": False, "flags": []},
            "reason": "reason",
            "extracted_data": {"entities": {}, "dates": [], "amounts": [], "references": {}, "deadlines": []},
        }
        intel = {
            "case_understanding": {
                "case_id": "case_x",
                "business_priority": "medium",
                "attention_reason": "a",
                "blockers": [],
                "review_required": False,
                "review_flags": [],
            },
            "operator_brief": {"brief_pl": "brief"},
            "next_best_action": {"primary_next_action": {}, "secondary_actions": []},
            "missing_info": {"summary_pl": "", "critical": [], "important": [], "helpful": []},
            "risk_assessment": {"summary_pl": "", "risks": []},
            "merge_split_suggestions": {"summary_pl": "", "merge_candidates": [], "split_suspicions": []},
            "desk_composition": {
                "should_surface": True,
                "presence_mode": "advisory",
                "surface_zone": "desk",
                "day_bucket": "dzisiaj",
                "title_pl": "t",
                "body_short_pl": "legacy body",
                "body_reason_pl": "br",
                "assistant_suggestion_pl": "as",
                "visibility_score": 0.6,
                "lifecycle_intent": "create",
                "review_required": False,
                "trace_summary": "",
            },
            "lifecycle_revision": {
                "lifecycle_intent": "create",
                "target_presence_mode": "advisory",
                "target_surface_zone": "desk",
                "reason_pl": "lr",
                "should_create": True,
                "should_update": False,
            },
            "feedback_learning_memory": {
                "explicit_signals": [],
                "implicit_signals": [],
                "preference_biases": [],
                "suppression_hints": [],
                "tone_hint_pl": "",
                "emphasis_hint_pl": "",
            },
            "case_guidance": {},
            "attachment_intelligence": {},
            "thread_memory": {},
            "review_routing": {},
            "automation_policy": {},
        }
        proj = build_v2_shadow_projection(
            intake,
            run_id="hot-primary-arrays-test",
            stage_outputs={
                "preclassification_result": {"lane": "intake_llm"},
                "case_link_result": {"decision": "linked", "selected_case_key": "k", "reasons": []},
                "action_plan_result": {"primary_action": "prepare_reply", "safe_for_live_push": False},
                "business_reasoning_result": {},
                "case_intelligence_result": intel,
                "mailbox_memory_result": {
                    "context_pack": {
                        "snapshot": {
                            "key_facts": [{"fact_key": "legacy_fact", "value": "legacy"}],
                            "conflicting_facts": [{"fact_key": "legacy_conflict", "values": ["legacy"]}],
                            "latest_documents": [{"document_id": "legacy_doc"}],
                        }
                    },
                    "case_snapshot_hot_state": {
                        "schema_version": CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION,
                        "case": {
                            "case_id": "case_x",
                            "summary_text": "HOT_STATE_PRIMARY_SUMMARY_FOR_UI",
                            "operational_status": "active",
                        },
                        "key_facts": [{"fact_key": "hot_fact", "value": "hot"}],
                        "active_conflicts": [{"fact_key": "hot_conflict", "values": ["hot-a", "hot-b"]}],
                        "documents_summary": [{"document_id": "hot_doc"}],
                    },
                },
            },
        )
        self.assertEqual(proj["case_patch"]["key_facts"], [{"fact_key": "hot_fact", "value": "hot"}])
        self.assertEqual(
            proj["case_patch"]["conflicting_facts"],
            [{"fact_key": "hot_conflict", "values": ["hot-a", "hot-b"]}],
        )
        self.assertEqual(proj["case_patch"]["latest_documents"], [{"document_id": "hot_doc"}])
        self.assertEqual(proj["desk_note_patch"]["key_facts"], [{"fact_key": "hot_fact", "value": "hot"}])
        self.assertEqual(
            proj["desk_note_patch"]["conflicting_facts"],
            [{"fact_key": "hot_conflict", "values": ["hot-a", "hot-b"]}],
        )
        self.assertEqual(proj["desk_note_patch"]["latest_documents"], [{"document_id": "hot_doc"}])


if __name__ == "__main__":
    unittest.main()
