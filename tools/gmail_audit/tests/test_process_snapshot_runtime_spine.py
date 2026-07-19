"""
Protective tests: signal-active orchestration in process_snapshot (only supported spine).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from gmail_intake import init_run_state, process_snapshot
from intake_shared_downstream import run_shared_downstream_stages as _run_shared_downstream_real
from gmail_signal_adapter import GmailSignalRuntimeResult
from signal_contract import build_canonical_signal
from signal_reconciler import ReconcileResult, SignalRuntimeContext, _reconcile_gmail_signal
from signal_journal import SignalJournal
from mailbox_memory_store import InMemoryMailboxMemoryStore


# Legacy tail stages that must run when process_snapshot does not early-return (PR-2/PR-3 contract).
LEGACY_TAIL_MARKERS = (
    "run_shared_downstream_stages",
    "link_case_context",
    "ingest_mailbox_memory",
    "run_business_reasoning",
    "build_case_intelligence_layer",
    "attach_policy_and_proposals",
)


@dataclass
class _CallLog:
    names: list[str] = field(default_factory=list)

    def record(self, name: str) -> None:
        self.names.append(name)


def _minimal_snapshot() -> dict[str, Any]:
    return {
        "mailbox": "ops@example.com",
        "observed_at": "2026-04-13T09:00:00+02:00",
        "source_message": {
            "message_id": "msg-spine-1",
            "thread_id": "thr-1",
            "history_id": "42",
            "date": "2026-04-13T08:59:00+02:00",
            "subject": "Spine test",
            "attachment_parts": [],
        },
        "context_messages": [],
    }


def _valid_validation_bundle() -> dict[str, Any]:
    intake_output = {"decision": {"action": "review"}, "review": {"flags": []}}
    intake_result_final = {
        "decision": {"action": "review"},
        "review_required": False,
        "review_reasons": [],
    }
    validation = SimpleNamespace(parse_ok=True, schema_ok=True, semantic_ok=True, errors=[])
    validation_trace = SimpleNamespace(
        normalized_candidate=None,
        repair_applied=False,
        normalization_applied=False,
        normalization_notes=[],
        repair_notes=[],
        final_output_origin="raw_valid",
    )
    return {
        "is_valid": True,
        "intake_output": intake_output,
        "intake_result_final": intake_result_final,
        "guardrail_flags": [],
        "final_output_origin": "raw_valid",
        "validation": validation,
        "validation_trace": validation_trace,
        "original_action": "review",
        "raw_valid": True,
        "normalized_valid": False,
        "repaired_valid": False,
    }


def _settings(signal_runtime_mode: str) -> SimpleNamespace:
    return SimpleNamespace(
        signal_runtime_mode=signal_runtime_mode,
        intake_llm_before_signal=True,
        daszek_v2_push_enabled=False,
        groq_model="test-model",
        mailbox_memory_stage_mode="shadow",
        mailbox_memory_database_url="postgresql://local/test",
        mailbox_memory_blob_root=Path(tempfile.gettempdir()) / "spine-tests",
        understanding_output_enabled=False,
        decision_pipeline_enabled=False,
        action_proposal_v2_enabled=False,
    )


def _run_state(tmp: Path) -> dict[str, Any]:
    return init_run_state(
        run_id="spine-test-run",
        run_dir=tmp / "run",
        command="message",
        selector={"type": "message"},
        mailbox="ops@example.com",
        model="test-model",
        schema_path=None,
        source_run=None,
        push_daszek=False,
        runtime_controls={"projection_proof": False},
    )


def _active_reconcile_result() -> ReconcileResult:
    return ReconcileResult(
        signal_id="sig-spine-active",
        source_kind="gmail",
        signal_kind="gmail_message_observed",
        processing_state="reconciled",
        preview={"operator_preview": True},
        v2_projection={"schema": "v2"},
        stage_outputs={
            "case_link_result": {"selected_case_key": "case-1"},
            "business_reasoning_result": {},
            "reply_draft_result": {},
            "action_plan_result": {},
            "case_intelligence_result": {},
            "mailbox_memory_result": {},
        },
    )


def _process_snapshot_patches(log: _CallLog):
    """Patches heavy IO/LLM; records spine function names when invoked."""

    intake_reasoning = {
        "raw_output_text": "{}",
        "response_json": {},
        "request_meta": {"final_inference_mode": "llm"},
        "second_pass_applied": False,
    }

    return (
        patch("gmail_intake.append_jsonl"),
        patch(
            "gmail_intake._record_gmail_raw_observation",
            return_value={"observation": SimpleNamespace(observation_id="obs-1", source_kind="gmail")},
        ),
        patch(
            "gmail_intake._build_gmail_triage_result",
            return_value={
                "preclassification": {"lane": "intake_llm"},
                "routing_decision": "process",
                "triage_class": "primary",
                "reasoning_budget": {},
                "batching": {},
            },
        ),
        patch("gmail_intake.hydrate_intelligence_seam_config"),
        patch("gmail_intake.enrich_snapshot_for_inference"),
        patch("gmail_intake.run_intake_reasoning", side_effect=lambda *a, **k: (log.record("run_intake_reasoning"), intake_reasoning)[1]),
        patch("gmail_intake.validate_intake_output", return_value=_valid_validation_bundle()),
        patch(
            "intake_shared_downstream.run_shared_downstream_stages",
            side_effect=lambda *a, **kw: (log.record("run_shared_downstream_stages"), _run_shared_downstream_real(*a, **kw))[1],
        ),
        patch("gmail_intake.link_case_context", side_effect=lambda *a, **k: (log.record("link_case_context"), {"selected_case_key": "case-1"})[1]),
        patch("gmail_intake.ingest_mailbox_memory", side_effect=lambda *a, **k: (log.record("ingest_mailbox_memory"), {"context_pack": {}})[1]),
        patch("gmail_intake.load_hot_state_preflight_for_stage_config"),
        patch("gmail_intake._resolve_effective_context_bundle", side_effect=lambda _s, bundle, _c: bundle),
        patch("gmail_intake.run_business_reasoning", side_effect=lambda *a, **k: (log.record("run_business_reasoning"), {})[1]),
        patch("gmail_intake.draft_reply", return_value={}),
        patch("gmail_intake.plan_actions", return_value={}),
        patch("gmail_intake.build_case_intelligence_layer", side_effect=lambda *a, **k: (log.record("build_case_intelligence_layer"), {})[1]),
        patch("gmail_intake.finalize_mailbox_memory", side_effect=lambda **k: k.get("mailbox_memory_result") or {}),
        patch("gmail_intake.inject_latest_hot_state_for_resolved_case", side_effect=lambda **k: (k["mailbox_memory_result"], k["case_intelligence_result"])),
        patch(
            "policy_action_proposal.attach_policy_and_proposals",
            side_effect=lambda **k: (log.record("attach_policy_and_proposals"), (SimpleNamespace(to_dict=lambda: {}), {}))[1],
        ),
        patch("gmail_intake.build_projection_preview", return_value={"preview": True}),
        patch("gmail_intake.build_v2_projection", return_value={"v2": True}),
        patch("gmail_intake.push_v2_projection_to_daszek"),
        patch("gmail_intake.push_preview_to_daszek", return_value=True),
        patch("gmail_intake.check_runtime_stop_conditions", return_value=False),
        patch("gmail_intake._persist_stage_record"),
    )


class ProcessSnapshotRuntimeSpineTests(unittest.TestCase):
    def _invoke_process_snapshot(self, mode: str, log: _CallLog) -> bool:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            run_state = _run_state(tmp)
            settings = _settings(mode)
            stage_config: dict[str, Any] = {
                "settings": settings,
                "schema": {},
                "instructions": "",
                "model": None,
                "verbose": False,
                "snapshot": _minimal_snapshot(),
            }
            patches = _process_snapshot_patches(log)
            for p in patches:
                p.start()
            try:
                signal_patch = patch("gmail_signal_adapter.run_gmail_signal_runtime")
                with signal_patch as mock_signal_runtime:
                    if mode in {"shadow", "active"}:
                        primary = build_canonical_signal(
                            signal_kind="gmail_message_observed",
                            source_kind="gmail",
                            source_ref={"message_id": "msg-spine-1"},
                            observed_at="2026-04-13T09:00:00+02:00",
                            effective_at="2026-04-13T08:59:00+02:00",
                            signal_summary_pl="test",
                            payload={},
                        )
                        reconcile = _active_reconcile_result()
                        mock_signal_runtime.return_value = GmailSignalRuntimeResult(
                            primary_signal=primary,
                            reconcile_result=reconcile,
                        )
                        mock_signal_runtime.side_effect = lambda **kw: (
                            log.record("run_gmail_signal_runtime"),
                            mock_signal_runtime.return_value,
                        )[1]

                    return process_snapshot(
                        settings=settings,
                        schema={},
                        instructions="",
                        run_state=run_state,
                        snapshot=_minimal_snapshot(),
                        model=None,
                        verbose=False,
                        keep_going=True,
                    )
            finally:
                for p in reversed(patches):
                    p.stop()

    def test_active_mode_early_return_skips_legacy_tail(self) -> None:
        log = _CallLog()
        ok = self._invoke_process_snapshot("active", log)
        self.assertTrue(ok)
        self.assertIn("run_intake_reasoning", log.names)
        self.assertIn("run_gmail_signal_runtime", log.names)
        self.assertNotIn("link_case_context", log.names)
        self.assertNotIn("ingest_mailbox_memory", log.names)
        self.assertNotIn("attach_policy_and_proposals", log.names)

    def test_active_signal_runtime_invoked_without_dry_run(self) -> None:
        captured: dict[str, Any] = {}

        def _capture_signal_runtime(**kwargs: Any) -> GmailSignalRuntimeResult:
            captured["dry_run"] = kwargs.get("dry_run")
            primary = build_canonical_signal(
                signal_kind="gmail_message_observed",
                source_kind="gmail",
                source_ref={"message_id": "msg-spine-1"},
                observed_at="2026-04-13T09:00:00+02:00",
                effective_at="2026-04-13T08:59:00+02:00",
                signal_summary_pl="test",
                payload={},
            )
            return GmailSignalRuntimeResult(primary_signal=primary, reconcile_result=_active_reconcile_result())

        log = _CallLog()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            run_state = _run_state(tmp)
            settings = _settings("active")
            patches = _process_snapshot_patches(log)
            for p in patches:
                p.start()
            try:
                with patch("gmail_signal_adapter.run_gmail_signal_runtime", side_effect=_capture_signal_runtime):
                    process_snapshot(
                        settings=settings,
                        schema={},
                        instructions="",
                        run_state=run_state,
                        snapshot=_minimal_snapshot(),
                        model=None,
                        verbose=False,
                        keep_going=True,
                    )
            finally:
                for p in reversed(patches):
                    p.stop()

        self.assertIs(captured.get("dry_run"), False)


class ReconcileSpineGapTests(unittest.TestCase):
    """Documents open architectural question: reconcile downstream omits run_intake_reasoning."""

    def test_reconcile_gmail_signal_does_not_call_run_intake_reasoning(self) -> None:
        log = _CallLog()
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        settings = _settings("active")
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
        signal = build_canonical_signal(
            signal_kind="gmail_message_observed",
            source_kind="gmail",
            source_ref={"message_id": "msg-reconcile-1", "history_id": "1"},
            observed_at="2026-04-13T09:00:00+02:00",
            effective_at="2026-04-13T08:59:00+02:00",
            signal_summary_pl="reconcile spine test",
            payload={
                "snapshot": _minimal_snapshot(),
                "intake_result_final": {"decision": {"action": "review"}, "review_required": False},
                "preclassification_result": {"lane": "intake_llm"},
                "lane_stage_plan": {"run_case_linking": True, "run_business_reasoning": True},
                "context_bundle": {},
            },
        )

        def _mark(name: str):
            def _inner(*args: Any, **kwargs: Any) -> Any:
                log.record(name)
                if name == "link_case_context":
                    return {"selected_case_key": "case-1"}
                if name == "ingest_mailbox_memory":
                    return {"context_pack": {}}
                if name == "build_case_intelligence_layer":
                    return {}
                return {}

            return _inner

        with (
            patch("gmail_intake.run_intake_reasoning", side_effect=lambda *a, **k: log.record("run_intake_reasoning")),
            patch(
                "intake_shared_downstream.run_shared_downstream_stages",
                side_effect=lambda *a, **kw: (log.record("run_shared_downstream_stages"), _run_shared_downstream_real(*a, **kw))[1],
            ),
            patch("gmail_intake.link_case_context", side_effect=_mark("link_case_context")),
            patch("gmail_intake.ingest_mailbox_memory", side_effect=_mark("ingest_mailbox_memory")),
            patch("gmail_intake.load_hot_state_preflight_for_stage_config"),
            patch("gmail_intake.run_business_reasoning", side_effect=_mark("run_business_reasoning")),
            patch("gmail_intake.draft_reply", return_value={}),
            patch("gmail_intake.plan_actions", return_value={}),
            patch("gmail_intake.build_case_intelligence_layer", side_effect=_mark("build_case_intelligence_layer")),
            patch("gmail_intake.finalize_mailbox_memory", return_value={}),
            patch("gmail_intake.merge_hot_state_into_mailbox_memory_result", return_value={}),
            patch("policy_action_proposal.attach_policy_and_proposals", return_value=(SimpleNamespace(to_dict=lambda: {}), {})),
            patch("gmail_intake.build_projection_preview", return_value={}),
            patch(
                "projection_snapshot_transport.build_operator_projection_snapshot",
                return_value={"v2_projection": {}, "decision_view": {}},
            ),
            patch("signal_reconciler.apply_entity_link", return_value={}),
            patch("signal_reconciler.decide_projection_refresh", return_value=MagicMock(to_dict=lambda: {})),
        ):
            result = _reconcile_gmail_signal(
                signal,
                runtime_context=context,
                dry_run=True,
                entity_link_dict={},
            )

        self.assertNotIn("run_intake_reasoning", log.names)
        self.assertIn("run_shared_downstream_stages", log.names)
        self.assertIn("run_business_reasoning", log.names)
        self.assertEqual(result.processing_state, "shadowed")


if __name__ == "__main__":
    unittest.main()
