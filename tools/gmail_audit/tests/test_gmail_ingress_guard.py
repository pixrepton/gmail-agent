"""D3 ingress guard tests — signal-active only with edge cases."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from config import ConfigError
from gmail_ingress_guard import enforce_legacy_cli_ingress_allowed, ingress_owner_warnings


class GmailIngressGuardTests(unittest.TestCase):
    """Comprehensive tests for D3 ingress guard."""

    def test_blocks_message_command(self) -> None:
        settings = SimpleNamespace(gmail_ingress_owner="signal_worker", event_spine_processor_enabled=False)
        with self.assertRaises(ConfigError) as ctx:
            enforce_legacy_cli_ingress_allowed(settings, command="message")
        self.assertIn("signal-active only", str(ctx.exception))

    def test_blocks_period_command(self) -> None:
        settings = SimpleNamespace(gmail_ingress_owner="", event_spine_processor_enabled=False)
        with self.assertRaises(ConfigError):
            enforce_legacy_cli_ingress_allowed(settings, command="period")

    def test_blocks_batch_command(self) -> None:
        settings = SimpleNamespace(gmail_ingress_owner="signal_worker", event_spine_processor_enabled=False)
        with self.assertRaises(ConfigError):
            enforce_legacy_cli_ingress_allowed(settings, command="batch")

    def test_blocks_shadow_run_command(self) -> None:
        settings = SimpleNamespace(gmail_ingress_owner="", event_spine_processor_enabled=False)
        with self.assertRaises(ConfigError):
            enforce_legacy_cli_ingress_allowed(settings, command="shadow-run")

    def test_allows_signal_run(self) -> None:
        settings = SimpleNamespace(gmail_ingress_owner="signal_worker", event_spine_processor_enabled=False)
        enforce_legacy_cli_ingress_allowed(settings, command="signal-run")

    def test_allows_signal_worker(self) -> None:
        settings = SimpleNamespace(gmail_ingress_owner="signal_worker", event_spine_processor_enabled=True)
        enforce_legacy_cli_ingress_allowed(settings, command="signal-worker")

    def test_allows_doctor(self) -> None:
        settings = SimpleNamespace(gmail_ingress_owner="signal_worker", event_spine_processor_enabled=False)
        enforce_legacy_cli_ingress_allowed(settings, command="doctor")

    def test_allows_event_spine_processor(self) -> None:
        settings = SimpleNamespace(gmail_ingress_owner="signal_worker", event_spine_processor_enabled=True)
        enforce_legacy_cli_ingress_allowed(settings, command="event-spine-processor")

    def test_ingress_warnings_resolved_d3(self) -> None:
        """D3 resolved: signal_worker is sole Gmail ingress owner — no warnings."""
        settings = SimpleNamespace(gmail_ingress_owner="signal_worker", event_spine_processor_enabled=True)
        warnings = ingress_owner_warnings(settings)
        self.assertEqual(len(warnings), 0, f"D3 should be resolved, got warnings: {warnings}")

    def test_ingress_warnings_empty_with_legacy_cli_owner(self) -> None:
        settings = SimpleNamespace(gmail_ingress_owner="legacy_cli", event_spine_processor_enabled=True)
        warnings = ingress_owner_warnings(settings)
        self.assertEqual(len(warnings), 0)

    def test_allows_rerun_command(self) -> None:
        settings = SimpleNamespace(gmail_ingress_owner="signal_worker", event_spine_processor_enabled=False)
        enforce_legacy_cli_ingress_allowed(settings, command="rerun")

    def test_blocks_empty_command_raises(self) -> None:
        settings = SimpleNamespace(gmail_ingress_owner="signal_worker", event_spine_processor_enabled=False)
        enforce_legacy_cli_ingress_allowed(settings, command="")

    def test_blocks_none_command_raises(self) -> None:
        settings = SimpleNamespace(gmail_ingress_owner="signal_worker", event_spine_processor_enabled=False)
        enforce_legacy_cli_ingress_allowed(settings, command=None)

    def test_module_all_exports(self) -> None:
        from gmail_ingress_guard import __all__
        self.assertIn("enforce_legacy_cli_ingress_allowed", __all__)
        self.assertIn("ingress_owner_warnings", __all__)


if __name__ == "__main__":
    unittest.main()
