"""RC-15 reproducer: the agent-runtime branch depends on loader order.

`load_agent_runtime_settings()` reads `AGENT_RUNTIME_ENABLED` straight from the
environment after loading the agent dotenv file. `config.load_settings()` writes
that same variable from the Case OS runtime profile. Whichever runs first in a
process decides the answer, so the retained agent branch is selected
non-deterministically.

This is NOT fixed. It is left as a strict xfail because closing it requires a
precedence decision that the current suite blocks in both directions:

* profile default filling in only what is unset (dotenv wins) breaks 13 tests
  that rely on the profile forcing the agent on
  (`test_signal_reconciler_runtime`, `test_runtime_doctor_checks`,
  `test_canonical_runtime_profile`, `test_x14_discard_audit`, ...);
* the profile assigning unconditionally in both entrypoints breaks
  `test_agent_primary_mode_pr_f`, which sets `AGENT_RUNTIME_MODE=primary`
  and would have it overwritten with the profile's `prep`.

Either resolution is a semantics change to the config plane plus test updates,
so it belongs in the early-decision queue, not in a repair commit. When it is
decided and implemented, this xfail turns into a failure and must become a
plain assertion.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

_PROBE = """
import json, os, sys
sys.path.insert(0, {tool_dir!r})
for _name in ("AGENT_RUNTIME_ENABLED", "AGENT_RUNTIME_MODE", "CASE_OS_RUNTIME_PROFILE",
              "EMERGENCY_INTELLIGENCE_KILLSWITCH"):
    os.environ.pop(_name, None)
{setup}
from agent_runtime.settings import load_agent_runtime_settings
s = load_agent_runtime_settings()
print(json.dumps({{"enabled": s.enabled, "mode": s.mode}}))
"""

_PROFILE_FIRST = (
    "from config import apply_case_os_runtime_profile_overrides\n"
    "apply_case_os_runtime_profile_overrides()"
)


def _probe(setup: str) -> dict:
    """Resolve the agent branch in a fresh process, so dotenv memoisation is honest."""
    src = _PROBE.format(tool_dir=str(TOOL_DIR), setup=setup)
    out = subprocess.run(
        [sys.executable, "-c", src], capture_output=True, text=True, cwd=str(TOOL_DIR), check=True
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.xfail(
    strict=True,
    reason="RC-15 open: agent-runtime branch still depends on which config plane loads first",
)
def test_branch_is_identical_whichever_plane_runs_first() -> None:
    agent_first = _probe("")
    profile_first = _probe(_PROFILE_FIRST)
    assert agent_first == profile_first
