"""Workspace path resolution for gmail-agent tests and tooling."""
from __future__ import annotations

import os
from pathlib import Path

_GMAIL_AGENT_ROOT = Path(__file__).resolve().parents[2]
_TOP_CODE_ROOT = Path(
    os.environ.get("TOP_CODE_ROOT", _GMAIL_AGENT_ROOT.parent)
).resolve()

DASZEK_ROOT = Path(os.environ.get("DASZEK_ROOT", _TOP_CODE_ROOT / "daszek")).resolve()
WP_BRIDGES_ROOT = Path(os.environ.get("WP_BRIDGES_ROOT", _TOP_CODE_ROOT / "wp-bridges")).resolve()
GMAIL_AGENT_ROOT = Path(os.environ.get("GMAIL_AGENT_ROOT", _GMAIL_AGENT_ROOT)).resolve()
TOP_CODE_ROOT = _TOP_CODE_ROOT

# Legacy alias used by tests
REPO_ROOT = GMAIL_AGENT_ROOT
DASZEK_DIR = DASZEK_ROOT
