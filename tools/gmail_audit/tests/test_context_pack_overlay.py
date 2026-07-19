from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from context_assembler import AssembledContext
from context_pack_overlay import overlay_pack_onto_assembled


def test_overlay_merges_pack_facts_and_chunks() -> None:
    assembled = AssembledContext(
        company_context="x",
        case_facts={"existing": 1},
        relevant_chunks=[],
        assembled_at="2026-05-27T00:00:00+00:00",
    )
    bundle = {
        "case_context_pack": {
            "case_id": "case-abc",
            "engagement_id": "eng-1",
            "active_facts": [{"fact_key": "powierzchnia_m2", "value": 120}],
            "relevant_chunks": [{"chunk_id": "ch-1", "text": "fragment"}],
        }
    }
    out = overlay_pack_onto_assembled(assembled, bundle)
    assert out.case_id_used == "case-abc"
    assert out.engagement_id == "eng-1"
    assert out.case_facts["existing"] == 1
    assert out.case_facts["powierzchnia_m2"] == 120
    assert len(out.relevant_chunks) == 1
