from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import ConfigError, load_settings


class SignalRuntimeSettingsTests(unittest.TestCase):
    def test_signal_runtime_mode_defaults_to_active(self) -> None:
        env = {
            "GROQ_API_KEY": "test",
            "GOOGLE_ACCESS_TOKEN": "token",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings(require_groq=True, require_google=True)

        self.assertEqual(settings.signal_runtime_mode, "active")
        self.assertTrue(settings.signal_runtime_enabled)
        self.assertEqual(settings.gmail_ingress_owner, "signal_worker")

    def test_legacy_mode_rejected(self) -> None:
        env = {
            "GROQ_API_KEY": "test",
            "GOOGLE_ACCESS_TOKEN": "token",
            "SIGNAL_RUNTIME_MODE": "legacy",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load_settings(require_groq=True, require_google=True)
        self.assertIn("signal-active only", str(ctx.exception))

    def test_shadow_mode_rejected(self) -> None:
        env = {
            "GROQ_API_KEY": "test",
            "GOOGLE_ACCESS_TOKEN": "token",
            "SIGNAL_RUNTIME_MODE": "shadow",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load_settings(require_groq=True, require_google=True)
        self.assertIn("signal-active only", str(ctx.exception))

    def test_signal_runtime_compat_rejected(self) -> None:
        env = {
            "GROQ_API_KEY": "test",
            "GOOGLE_ACCESS_TOKEN": "token",
            "SIGNAL_RUNTIME_COMPAT": "1",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load_settings(require_groq=True, require_google=True)
        self.assertIn("SIGNAL_RUNTIME_COMPAT", str(ctx.exception))

    def test_use_signal_runtime_alias_promotes_active_mode(self) -> None:
        env = {
            "GROQ_API_KEY": "test",
            "GOOGLE_ACCESS_TOKEN": "token",
            "USE_SIGNAL_RUNTIME": "1",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings(require_groq=True, require_google=True)

        self.assertEqual(settings.signal_runtime_mode, "active")
        self.assertTrue(settings.signal_runtime_enabled)

    def test_use_signal_runtime_off_rejected(self) -> None:
        env = {
            "GROQ_API_KEY": "test",
            "GOOGLE_ACCESS_TOKEN": "token",
            "USE_SIGNAL_RUNTIME": "0",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError):
                load_settings(require_groq=True, require_google=True)


if __name__ == "__main__":
    unittest.main()
