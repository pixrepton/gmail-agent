"""Epik 2: signal-active spine-first — intake LLM deferred until optional legacy path."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from gmail_intake import (
    _build_spine_first_intake_validation_result,
    init_run_state,
    process_snapshot,
)
from gmail_signal_adapter import GmailSignalRuntimeResult
from signal_reconciler import ReconcileResult


class SpineFirstIntakeTests(unittest.TestCase):
    def test_spine_first_bundle_skips_llm_and_marks_origin(self) -> None:
        snapshot = {
            "mailbox": "ops@example.com",
            "observed_at": "2026-04-13T09:00:00+02:00",
            "source_message": {
                "message_id": "msg-spine-first-1",
                "thread_id": "thr-1",
                "history_id": "99",
                "date": "2026-04-13T08:59:00+02:00",
                "subject": "HVAC zapytanie",
                "attachment_parts": [],
            },
        }
        preclass = {"lane": "intake_llm", "confidence": 0.7}
        bundle = _build_spine_first_intake_validation_result(
            snapshot=snapshot,
            preclassification_result=preclass,
            lane_stage_plan={"lane": "intake_llm"},
        )
        self.assertTrue(bundle["is_valid"])
        self.assertEqual(bundle["final_output_origin"], "spine_first_preclassified")
        self.assertTrue(bundle["intake_reasoning_result"]["execution_metadata"]["skipped"])
        self.assertTrue(bundle["intake_output"]["review"]["required"])

    def test_process_snapshot_spine_first_does_not_call_run_intake_reasoning(self) -> None:
        calls: list[str] = []

        def _track(name: str):
            def _inner(*_a, **_k):
                calls.append(name)
                return None

            return _inner

        primary = SimpleNamespace(
            signal_id="sig-epik2",
            signal_kind="gmail_message_observed",
            source_kind="gmail",
            payload={},
        )
        runtime_result = GmailSignalRuntimeResult(
            primary_signal=primary,
            reconcile_result=ReconcileResult(
                signal_id="sig-epik2",
                source_kind="gmail",
                signal_kind="gmail_message_observed",
                processing_state="reconciled",
                preview={},
                v2_projection={},
                stage_outputs={"mailbox_memory_result": {}},
            ),
        )

        settings = SimpleNamespace(
            signal_runtime_mode="active",
            intake_llm_before_signal=False,
            daszek_v2_push_enabled=False,
            groq_model="test",
            mailbox_memory_stage_mode="live",
            mailbox_memory_database_url="postgresql://local/test",
            mailbox_memory_blob_root=Path(tempfile.gettempdir()),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            run_state = init_run_state(
                run_id="epik2-run",
                run_dir=tmp / "run",
                command="signal-run",
                selector={},
                mailbox="ops@example.com",
                model="test",
                schema_path=None,
                source_run=None,
                push_daszek=False,
                runtime_controls={},
            )
            patches = [
                patch("gmail_intake.append_jsonl"),
                patch("gmail_intake.run_intake_reasoning", side_effect=_track("run_intake_reasoning")),
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
                patch("gmail_intake.envelopes_for_telemetry", return_value={}),
                patch("gmail_intake.preclassify_snapshot", return_value={"lane": "intake_llm"}),
                patch(
                    "gmail_signal_adapter.run_gmail_signal_runtime",
                    side_effect=lambda **_k: (calls.append("run_gmail_signal_runtime"), runtime_result)[1],
                ),
                patch("gmail_intake.push_v2_projection_to_daszek"),
            ]
            for p in patches:
                p.start()
            try:
                ok = process_snapshot(
                    settings=settings,
                    schema={},
                    instructions="",
                    run_state=run_state,
                    snapshot={
                        "mailbox": "ops@example.com",
                        "observed_at": "2026-04-13T09:00:00+02:00",
                        "source_message": {
                            "message_id": "msg-epik2",
                            "thread_id": "thr-1",
                            "history_id": "1",
                            "date": "2026-04-13T08:59:00+02:00",
                            "subject": "Test",
                            "attachment_parts": [],
                        },
                    },
                    model=None,
                    verbose=False,
                    keep_going=True,
                )
            finally:
                for p in reversed(patches):
                    p.stop()

        self.assertTrue(ok)
        self.assertNotIn("run_intake_reasoning", calls)
        self.assertIn("run_gmail_signal_runtime", calls)
        self.assertTrue(run_state["manifest"].get("spine_first_intake"))


if __name__ == "__main__":
    unittest.main()
