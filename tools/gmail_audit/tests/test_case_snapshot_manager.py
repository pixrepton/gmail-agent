from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_snapshot_manager import CaseSnapshotManager
from mailbox_memory_store import InMemoryMailboxMemoryStore
from signal_contract import build_canonical_signal


def _build_drive_signal(*, signal_id_suffix: str, signal_summary: str, invoice_value: str, observed_at: str):
    return build_canonical_signal(
        signal_kind="drive_document_added",
        source_kind="drive",
        source_ref={
            "file_id": f"drv-{signal_id_suffix}",
            "change_id": f"chg-{signal_id_suffix}",
            "revision_id": f"rev-{signal_id_suffix}",
            "modified_time": observed_at,
        },
        observed_at=observed_at,
        effective_at=observed_at,
        case_key_hint="case-key-1",
        thread_key_hint="case-key-1",
        business_lane="finance",
        signal_summary_pl=signal_summary,
        payload={
            "case_id": "case-1",
            "case_key": "case-key-1",
            "fact_rows": [
                {
                    "fact_id": f"fact-{signal_id_suffix}",
                    "case_id": "case-1",
                    "message_id": "",
                    "document_id": f"doc-{signal_id_suffix}",
                    "entity_scope": "document",
                    "fact_key": "invoice_number",
                    "normalized_value": invoice_value,
                    "raw_value": invoice_value,
                    "confidence": 0.93,
                    "observed_at": observed_at,
                    "source_type": "drive_signal",
                    "source_ref": f"https://drive.google.com/file/d/drv-{signal_id_suffix}",
                    "status": "active",
                    "metadata": {},
                }
            ],
        },
        artifacts={
            "raw_observation_id": f"obs-{signal_id_suffix}",
        },
        revision_marker=f"rev-{signal_id_suffix}",
        created_by_runtime="test",
    )


class CaseSnapshotManagerTests(unittest.TestCase):
    def test_apply_signal_appends_new_snapshot_versions_without_overwriting_history(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        store.upsert_case(
            {
                "case_id": "case-1",
                "case_key": "case-key-1",
                "thread_id": "",
                "case_family": "finance",
                "mailbox": "drive",
                "subject": "invoice.pdf",
                "status": "open",
                "customer_name": "",
                "customer_email": "",
                "metadata": {},
                "created_at": "2026-04-15T10:00:00+02:00",
                "updated_at": "2026-04-15T10:00:00+02:00",
            }
        )
        manager = CaseSnapshotManager(store=store)
        signal_a = _build_drive_signal(
            signal_id_suffix="1",
            signal_summary="Nowa faktura Drive",
            invoice_value="FV-1",
            observed_at="2026-04-15T10:01:00+02:00",
        )
        signal_b = _build_drive_signal(
            signal_id_suffix="2",
            signal_summary="Korekta faktury Drive",
            invoice_value="FV-2",
            observed_at="2026-04-15T10:05:00+02:00",
        )

        first = manager.apply_signal(signal_a)
        second = manager.apply_signal(signal_b)
        versions = store.fetch_case_snapshot_versions("case-1")

        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual([row["version"] for row in versions], [1, 2])
        self.assertIn("Nowa faktura Drive", versions[0]["snapshot_json"]["summary"])
        self.assertIn("Korekta faktury Drive", versions[1]["snapshot_json"]["summary"])
        self.assertEqual(versions[0]["snapshot_json"]["schema_version"], "case_snapshot_hot_state.v1")
        self.assertEqual(store.fetch_latest_case_snapshot_version("case-1")["version"], 2)

    def test_hot_state_surfaces_document_conflicts_and_calendar_deadline(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        store.upsert_case(
            {
                "case_id": "case-1",
                "case_key": "case-key-1",
                "case_family": "service",
                "status": "open",
                "metadata": {},
            }
        )
        store.upsert_document_intelligence_result(
            {
                "document_id": "doc-conflict",
                "source_type": "gmail_attachment",
                "source_id": "att-1",
                "case_id": "case-1",
                "filename": "faktura.pdf",
                "mime_type": "application/pdf",
                "document_type": "invoice",
                "document_type_confidence": 0.8,
                "summary": "Faktura z konfliktem kwoty",
                "extracted_fields": [],
                "evidence_refs": [],
                "conflicts": [
                    {
                        "conflict_type": "attachment_vs_attachment",
                        "field_name": "amount_total",
                        "severity": "medium",
                        "requires_human_review": True,
                        "values": [],
                    }
                ],
                "parser": "pdf_text",
                "parser_confidence": 0.8,
                "created_at": "2026-04-15T10:00:00+02:00",
                "requires_human_review": True,
            }
        )
        store.upsert_calendar_event(
            {
                "calendar_event_id": "cal-1",
                "source": "google_calendar",
                "summary": "Serwis",
                "description": "",
                "location": "Krakow",
                "start_at": "2026-04-16T10:00:00+02:00",
                "end_at": "2026-04-16T11:00:00+02:00",
                "attendees": [],
                "organizer": "",
                "html_link": "",
                "recurring": False,
                "ingested_at": "2026-04-15T10:00:00+02:00",
                "visibility_scope": "default",
                "case_id": "case-1",
                "link_confidence": 0.8,
            }
        )
        manager = CaseSnapshotManager(store=store)
        hot = manager.apply_signal(
            _build_drive_signal(
                signal_id_suffix="doccal",
                signal_summary="Nowy dokument i termin",
                invoice_value="FV-3",
                observed_at="2026-04-15T10:01:00+02:00",
            )
        )
        self.assertTrue(any(c.get("source_kind") == "document_intelligence" for c in hot["active_conflicts"]))
        self.assertTrue(any(d.get("source_kind") == "google_calendar" for d in hot["deadlines"]))
        self.assertIn("document", hot["summary_text"].lower())
        self.assertIn("calendar", hot["recommended_next_step"].lower())


if __name__ == "__main__":
    unittest.main()
