from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.business_pulse import _fallback_from_dash_projection
from signal_reconciler import ReconcileResult


def test_business_pulse_fallback_offers_not_desk_count() -> None:
    class _Settings:
        pass

    snap = {
        "feed": {
            "cases": [
                {"case_family": "lead_opportunity", "status": "open"},
                {"case_family": "reference_only", "status": "open"},
            ],
            "desk": [{"case_id": "d1"}, {"case_id": "d2"}, {"case_id": "d3"}],
        }
    }

    def _fake_build(store, **kwargs):
        return snap

    import agent_runtime.business_pulse as bp
    from unittest.mock import patch

    with patch("mailbox_memory_runtime.build_mailbox_memory_runtime", return_value=type("R", (), {"store": object()})()), patch(
        "daszek_v3_operational_feed.build_operational_feed_from_mailbox_store",
        return_value=snap,
    ):
        result = _fallback_from_dash_projection(_Settings())

    assert result is not None
    pipeline = result["pipeline"]
    assert pipeline["offers_in_progress"] == 1
    assert pipeline["desk_active_count"] == 3


def test_reconcile_result_linked_entity_fields() -> None:
    r = ReconcileResult(
        signal_id="sig1",
        source_kind="gmail",
        signal_kind="inbound",
        processing_state="skipped_duplicate",
        linked_entity_id="stg_abc",
        linked_entity_kind="engagement",
    )
    d = r.to_dict()
    assert d["linked_entity_id"] == "stg_abc"
    assert d["linked_entity_kind"] == "engagement"


# BUSINESS_PULSE_SEMANTICS_PROOF_OK · RECONCILE_FIELD_RENAME_PROOF_OK
