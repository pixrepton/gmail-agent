from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from observability_runtime import ObservabilityRuntime


class ObservabilityRuntimeTests(unittest.TestCase):
    def test_local_mirror_records_required_correlation_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime = ObservabilityRuntime(
                run_id="run-123",
                run_dir=Path(tmp_dir),
                command_name="message",
                enabled=False,
                local_mirror_enabled=True,
                service_name="gmail-agent",
                otlp_endpoint="",
                otlp_headers="authorization=secret-token",
            )

            with runtime.span(
                "mailbox_ingest",
                case_id="case-1",
                message_id="msg-1",
                thread_id="thr-1",
                signal_id="sig-1",
                trace_id="trace-domain-1",
                stage_name="mailbox_ingest",
            ):
                pass

            payload = runtime.read_local_events()

        self.assertEqual(len(payload), 1)
        event = payload[0]
        self.assertEqual(event["run_id"], "run-123")
        self.assertEqual(event["case_id"], "case-1")
        self.assertEqual(event["message_id"], "msg-1")
        self.assertEqual(event["thread_id"], "thr-1")
        self.assertEqual(event["signal_id"], "sig-1")
        self.assertEqual(event["trace_id"], "trace-domain-1")
        self.assertEqual(event["stage_name"], "mailbox_ingest")
        self.assertEqual(event["status"], "ok")
        self.assertIn("otel_trace_id", event)
        self.assertIn("otel_span_id", event)
        self.assertNotIn("secret-token", str(event))

    def test_inject_headers_adds_custom_correlation_headers(self) -> None:
        runtime = ObservabilityRuntime(
            run_id="run-456",
            run_dir=Path("."),
            command_name="replay-v2",
            enabled=False,
            local_mirror_enabled=False,
            service_name="gmail-agent",
            otlp_endpoint="",
            otlp_headers="",
        )

        headers = runtime.inject_headers(
            {
                "Accept": "application/json",
            },
            case_id="case-2",
            signal_id="sig-2",
            trace_id="trace-domain-2",
        )

        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["X-Gmail-Agent-Run-Id"], "run-456")
        self.assertEqual(headers["X-Gmail-Agent-Case-Id"], "case-2")
        self.assertEqual(headers["X-Gmail-Agent-Signal-Id"], "sig-2")
        self.assertEqual(headers["X-Gmail-Agent-Trace-Id"], "trace-domain-2")


if __name__ == "__main__":
    unittest.main()
