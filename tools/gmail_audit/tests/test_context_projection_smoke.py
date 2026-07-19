"""PR-I: programmatic smoke wrapper for context_projection_smoke.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "context_projection_smoke.py"


def test_context_projection_smoke_script_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert '"ok": true' in proc.stdout.replace(" ", "") or '"ok":true' in proc.stdout.replace(" ", "")
