"""DQ-17 / RC-15: one canonical setting owns the agent-runtime branch.

`AGENT_RUNTIME_MODE` is canonical and owned by the server-side Node B control plane.
`AGENT_RUNTIME_ENABLED` survives only as a deprecated legacy fallback, consulted for
resolution only when `AGENT_RUNTIME_MODE` is unset.

Precedence: mode -> legacy translation of enabled -> off.

Both set and agreeing: mode wins. Both set and contradicting: the agent stays off and
the contradiction is raised explicitly — it is never silently resolved to one value.
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

from agent_runtime.settings import (
    AGENT_RUNTIME_DISABLED_MODE,
    resolve_agent_runtime_branch,
)
from agent_runtime.validate import AgentRuntimeConfigError
from config import apply_case_os_agent_runtime_plane

_PLANE_ENV = (
    "AGENT_RUNTIME_MODE",
    "AGENT_RUNTIME_ENABLED",
    "CASE_OS_RUNTIME_PROFILE",
    "EMERGENCY_INTELLIGENCE_KILLSWITCH",
)


@pytest.fixture(autouse=True)
def _clean_plane_env(monkeypatch: pytest.MonkeyPatch):
    for name in _PLANE_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


# --- precedence -------------------------------------------------------------


def test_mode_is_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "prep")
    assert resolve_agent_runtime_branch() == ("prep", True)

    monkeypatch.setenv("AGENT_RUNTIME_MODE", "legacy")
    assert resolve_agent_runtime_branch() == ("legacy", False)


def test_mode_wins_when_both_are_set_and_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "prep")
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "1")
    assert resolve_agent_runtime_branch() == ("prep", True)

    monkeypatch.setenv("AGENT_RUNTIME_MODE", "legacy")
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "0")
    assert resolve_agent_runtime_branch() == ("legacy", False)


def test_default_is_agent_runtime_off() -> None:
    assert resolve_agent_runtime_branch() == (AGENT_RUNTIME_DISABLED_MODE, False)


# --- contradiction ----------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "legacy"),
    [("prep", "0"), ("legacy", "1"), ("primary", "0")],
)
def test_contradiction_fails_closed_and_loudly(
    monkeypatch: pytest.MonkeyPatch, mode: str, legacy: str
) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_MODE", mode)
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", legacy)
    with pytest.raises(AgentRuntimeConfigError) as excinfo:
        resolve_agent_runtime_branch()
    message = str(excinfo.value)
    # The contradiction must be named, not silently resolved to one of the two.
    assert "AGENT_RUNTIME_MODE" in message
    assert "AGENT_RUNTIME_ENABLED" in message
    assert "stays off" in message


def test_invalid_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "banana")
    with pytest.raises(AgentRuntimeConfigError):
        resolve_agent_runtime_branch()


# --- deprecated legacy fallback --------------------------------------------
# These run only with AGENT_RUNTIME_MODE absent. That is the whole contract of the
# fallback: it is never a competing source of truth alongside the canonical setting.


def test_legacy_fallback_translates_enabled_when_mode_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "1")
    mode, enabled = resolve_agent_runtime_branch()
    assert enabled is True
    assert mode != AGENT_RUNTIME_DISABLED_MODE

    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "0")
    assert resolve_agent_runtime_branch() == (AGENT_RUNTIME_DISABLED_MODE, False)


def test_legacy_fallback_is_ignored_for_resolution_once_mode_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "1")
    assert resolve_agent_runtime_branch()[1] is True
    # Same legacy value, now with the canonical setting saying off -> contradiction,
    # never a silent win for either side.
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "legacy")
    with pytest.raises(AgentRuntimeConfigError):
        resolve_agent_runtime_branch()


# --- control plane ----------------------------------------------------------


def test_killswitch_forces_the_agent_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMERGENCY_INTELLIGENCE_KILLSWITCH", "1")
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "prep")
    apply_case_os_agent_runtime_plane()
    assert resolve_agent_runtime_branch() == ("legacy", False)


def test_permissive_profile_supplies_but_does_not_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CASE_OS_RUNTIME_PROFILE", "full")
    apply_case_os_agent_runtime_plane()
    assert resolve_agent_runtime_branch() == ("prep", True)

    monkeypatch.setenv("AGENT_RUNTIME_MODE", "primary")
    apply_case_os_agent_runtime_plane()
    assert resolve_agent_runtime_branch()[0] == "primary"


def test_the_profile_never_emits_the_deprecated_setting() -> None:
    from config import _case_os_profile_env_overrides

    for profile in ("minimal", "full"):
        overrides = _case_os_profile_env_overrides(profile)
        assert "AGENT_RUNTIME_ENABLED" not in overrides
        assert overrides["AGENT_RUNTIME_MODE"] in ("prep", "legacy")


# --- RC-15: loader order ----------------------------------------------------

_PROBE = """
import json, os, sys
sys.path.insert(0, {tool_dir!r})
for _name in {plane_env!r}:
    os.environ.pop(_name, None)
# Subprocess must not inherit Gate A hermetic flags from the parent pytest process.
os.environ.pop("GMAIL_AUDIT_SKIP_AGENT_DOTENV", None)
os.environ.pop("PYTEST_CURRENT_TEST", None)
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
    """Resolve the branch in a fresh process, so dotenv memoisation stays honest."""
    src = _PROBE.format(tool_dir=str(TOOL_DIR), plane_env=_PLANE_ENV, setup=setup)
    out = subprocess.run(
        [sys.executable, "-c", src], capture_output=True, text=True, cwd=str(TOOL_DIR), check=True
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_branch_is_identical_whichever_plane_runs_first() -> None:
    assert _probe("") == _probe(_PROFILE_FIRST)


_EXPLICIT_MODE_ENV_SETUP = (
    "os.environ['AGENT_RUNTIME_MODE'] = 'legacy'\n"
    "os.environ['CASE_OS_RUNTIME_PROFILE'] = 'full'\n"
)


def test_an_explicit_operator_mode_survives_the_permissive_profile_either_order() -> None:
    """The substantive RC-15 case: an explicit setting that DISAGREES with the
    profile's own default. Coincidental agreement (both defaulting to "prep") would
    pass even with the old order-dependent code; this does not.
    """
    agent_first = _probe(_EXPLICIT_MODE_ENV_SETUP)
    profile_first = _probe(_EXPLICIT_MODE_ENV_SETUP + _PROFILE_FIRST)
    assert agent_first == profile_first == {"enabled": False, "mode": "legacy"}
