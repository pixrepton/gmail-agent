from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from scripts.agent_checklist_gate import run_checks


def test_agent_checklist_gate_passes() -> None:
    report = run_checks()
    failures = [k for k, v in report["checks"].items() if v["status"] != "DONE"]
    assert report["ok"] is True, f"failed checks: {failures}"
