"""Tests for Luka #3 operator-loop validate script."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
VALIDATE = REPO_ROOT / "deploy" / "vps-prove-operator-loop-validate.py"


class OperatorLoopValidateTests(unittest.TestCase):
    def test_validate_ok_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            (proof / "drain-output.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "results": [
                            {
                                "ok": True,
                                "bridge_out": {
                                    "truth_loop_executed": True,
                                    "reconcile_signal_ran": True,
                                    "reconcile_summary": {"processing_state": "reconciled"},
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (proof / "pending-after-drain.json").write_text(
                json.dumps({"items": []}),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, str(VALIDATE), str(proof)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("LUKA3_OPERATOR_LOOP_VALIDATE_OK", proc.stdout)

    def test_validate_fails_on_missing_truth_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            (proof / "drain-output.json").write_text(
                json.dumps([{"truth_loop_executed": False, "processing_state": "pending"}]),
                encoding="utf-8",
            )
            (proof / "pending-after-drain.json").write_text(json.dumps({"items": []}), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(VALIDATE), str(proof)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
