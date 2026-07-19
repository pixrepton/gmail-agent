from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_routing import case_row_requires_action


def test_legacy_row_without_requires_action_defaults_false() -> None:
    row = {
        "case_id": "legacy-1",
        "case_family": "reference_only",
        "metadata": {"export_case_type": "unknown_low_value"},
    }
    assert case_row_requires_action(row) is False


def test_explicit_requires_action_preserved() -> None:
    row = {
        "case_id": "legacy-2",
        "metadata": {"requires_action": True, "export_case_type": "lead_oferta"},
    }
    assert case_row_requires_action(row) is True


# REQUIRES_ACTION_BACKFILL_PROOF_OK
