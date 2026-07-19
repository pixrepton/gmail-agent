from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from export_hardening import ExportPolicyError, build_clean_export, verify_export_tree


class ExportHardeningTests(unittest.TestCase):
    def test_build_clean_export_keeps_templates_and_excludes_runtime_and_secret_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "src"
            export = root / "export"
            (source / "tools" / "gmail_audit" / "runs" / "run-1").mkdir(parents=True)
            (source / "tools" / "gmail_audit" / "runs" / "run-1" / "summary.json").write_text(
                "{}",
                encoding="utf-8",
            )
            (source / "tools" / "gmail_audit").mkdir(parents=True, exist_ok=True)
            (source / "tools" / "gmail_audit" / ".env").write_text("GROQ_API_KEY=live\n", encoding="utf-8")
            (source / "tools" / "gmail_audit" / ".env.example").write_text("GROQ_API_KEY=\n", encoding="utf-8")
            (source / ".venv" / "Scripts").mkdir(parents=True)
            (source / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
            (source / "test-results").mkdir()
            (source / "test-results" / "result.json").write_text("{}", encoding="utf-8")
            (source / ".env.mailbox-memory").write_text("MAILBOX_MEMORY_POSTGRES_PASSWORD=live\n", encoding="utf-8")
            (source / ".env.mailbox-memory.example").write_text(
                "MAILBOX_MEMORY_POSTGRES_PASSWORD=CHANGE_ME_LOCAL_ONLY\n",
                encoding="utf-8",
            )
            (source / ".coverage").write_text("", encoding="utf-8")
            (source / "mailbox-memory.db").write_text("", encoding="utf-8")
            (source / "Daszek" / "uploads" / "daszek").mkdir(parents=True)
            (source / "Daszek" / "uploads" / "daszek" / "tasks.json").write_text("[]", encoding="utf-8")
            (source / "docs").mkdir()
            (source / "docs" / "implementation — skrót .lnk").write_text("", encoding="utf-8")

            build_clean_export(source, export)

            self.assertTrue((export / "tools" / "gmail_audit" / ".env.example").is_file())
            self.assertTrue((export / ".env.mailbox-memory.example").is_file())
            self.assertFalse((export / "tools" / "gmail_audit" / ".env").exists())
            self.assertFalse((export / ".venv").exists())
            self.assertFalse((export / ".coverage").exists())
            self.assertFalse((export / "mailbox-memory.db").exists())
            self.assertFalse((export / ".env.mailbox-memory").exists())
            self.assertFalse((export / "tools" / "gmail_audit" / "runs").exists())
            self.assertFalse((export / "test-results").exists())
            self.assertFalse((export / "Daszek" / "uploads").exists())
            self.assertFalse((export / "docs" / "implementation — skrót .lnk").exists())

    def test_verify_export_tree_rejects_secret_and_runtime_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            export = Path(tmpdir) / "export"
            (export / "tools" / "gmail_audit").mkdir(parents=True)
            (export / "tools" / "gmail_audit" / ".env").write_text("GROQ_API_KEY=live\n", encoding="utf-8")
            (export / "tools" / "gmail_audit" / "runs").mkdir(parents=True)

            with self.assertRaises(ExportPolicyError) as exc:
                verify_export_tree(export)

        message = str(exc.exception)
        self.assertIn(".env", message)
        self.assertIn("runs", message)

    def test_build_clean_export_rejects_destination_inside_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "src"
            source.mkdir()
            nested_export = source / "handoff"

            with self.assertRaises(ExportPolicyError):
                build_clean_export(source, nested_export)


if __name__ == "__main__":
    unittest.main()
