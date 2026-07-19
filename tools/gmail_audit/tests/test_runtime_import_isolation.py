from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parent.parent


class RuntimeImportIsolationTests(unittest.TestCase):
    def test_runtime_helpers_import_without_google_auth_stack(self) -> None:
        script = textwrap.dedent(
            """
            import importlib.abc
            import sys

            class BlockGoogleStack(importlib.abc.MetaPathFinder):
                blocked = ("google", "googleapiclient")

                def find_spec(self, fullname, path=None, target=None):
                    if any(fullname == prefix or fullname.startswith(prefix + ".") for prefix in self.blocked):
                        raise RuntimeError(f"blocked import: {fullname}")
                    return None

            sys.meta_path.insert(0, BlockGoogleStack())

            import case_intelligence
            import dash_projection_v2
            import gmail_intake
            import v2_runtime

            assert gmail_intake.build_v2_ingest_payload is v2_runtime.build_v2_ingest_payload
            assert gmail_intake.extract_v2_projection_from_stage_record is v2_runtime.extract_v2_projection_from_stage_record
            print("ok")
            """
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(TOOL_DIR)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=TOOL_DIR,
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=f"subprocess import failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
