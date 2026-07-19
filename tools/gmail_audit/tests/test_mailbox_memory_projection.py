from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from dash_projection_v2 import build_v2_shadow_projection, validate_v2_shadow_projection
from tests.fixture_helpers import run_fixture


class MailboxMemoryProjectionTests(unittest.TestCase):
    def test_v2_projection_carries_case_snapshot_memory_fields(self) -> None:
        result = run_fixture("new_lead")
        context_pack = {
            "snapshot": {
                "case_id": "case_mailbox_memory_projection",
                "status": "awaiting_review",
                "customer": {"name": "Jan Kowalski", "email": "jan.kowalski@example.com"},
                "key_facts": [
                    {
                        "entity_scope": "document",
                        "fact_key": "heated_area_m2",
                        "value": "180",
                        "confidence": 0.92,
                        "source_ref": "doc_001",
                    }
                ],
                "latest_documents": [
                    {
                        "document_id": "doc_001",
                        "file_name": "charakterystyka.pdf",
                        "document_kind": "pdf",
                        "summary_text": "Projekt zawiera dane techniczne dla domu 180 m2.",
                        "updated_at": "2026-04-12T08:00:00+02:00",
                    }
                ],
                "conflicting_facts": [
                    {
                        "entity_scope": "document",
                        "fact_key": "heated_area_m2",
                        "values": ["180", "190"],
                    }
                ],
                "open_questions": ["Potwierdzic finalna powierzchnie ogrzewana."],
                "recommended_next_action": "generate_offer_draft",
            },
            "source_refs": [
                {"type": "message", "id": "msg_001"},
                {"type": "document", "id": "doc_001", "file_name": "charakterystyka.pdf"},
            ],
        }
        projection = build_v2_shadow_projection(
            result["intake_result"],
            run_id="fixture:mailbox-memory-projection",
            stage_outputs={
                "intake_result_final": result["intake_result"],
                "preclassification_result": result["preclassification"],
                "case_link_result": result["case_link_result"],
                "business_reasoning_result": result["business_result"],
                "reply_draft_result": result["reply_result"],
                "action_plan_result": result["action_plan"],
                "case_intelligence_result": result["case_intelligence"],
                "mailbox_memory_result": {"context_pack": context_pack},
            },
        )
        projection = validate_v2_shadow_projection(projection)

        self.assertEqual(projection["case_patch"]["case_snapshot"]["status"], "awaiting_review")
        self.assertEqual(projection["case_patch"]["key_facts"][0]["fact_key"], "heated_area_m2")
        self.assertEqual(projection["desk_note_patch"]["latest_documents"][0]["file_name"], "charakterystyka.pdf")
        self.assertEqual(projection["desk_note_patch"]["conflicting_facts"][0]["values"], ["180", "190"])
        self.assertEqual(projection["desk_note_patch"]["source_refs"][0]["type"], "message")


if __name__ == "__main__":
    unittest.main()
