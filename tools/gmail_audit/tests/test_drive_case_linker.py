from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from drive_case_linker import link_drive_candidate
from drive_ingest_models import DriveIngestCandidate
from mailbox_memory_store import InMemoryMailboxMemoryStore


class DriveCaseLinkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryMailboxMemoryStore()
        self.store.bootstrap()
        self.store.upsert_case(
            {
                "case_id": "case_siedlec",
                "case_key": "siedlec_9kw_panasonic_adc0309k3e5",
                "thread_id": "thr_1",
                "case_family": "heat_pump_offer",
                "mailbox": "drive",
                "subject": "Siedlec Panasonic 9 kW",
                "status": "open",
                "customer_name": "Jan Kowalski",
                "customer_email": "jan@example.com",
                "metadata": {
                    "installation_address": "Siedlec 12",
                    "model_bundle": "WH-ADC0309K3E5",
                },
                "created_at": "2026-04-12T08:00:00+02:00",
                "updated_at": "2026-04-12T08:00:00+02:00",
            }
        )
        self.store.upsert_snapshot(
            "case_siedlec",
            {
                "status": "open",
                "customer_name": "Jan Kowalski",
                "customer_email": "jan@example.com",
                "recommended_next_action": "",
                "snapshot_json": {
                    "key_facts": [
                        {"fact_key": "installation_address", "value": "Siedlec 12"},
                        {"fact_key": "device_model", "value": "WH-ADC0309K3E5"},
                    ]
                },
                "updated_at": "2026-04-12T08:00:00+02:00",
            },
        )

    def test_probable_case_key_match_is_deterministic(self) -> None:
        candidate = DriveIngestCandidate(
            drive_item_id="drv_1",
            title="Umowa Siedlec 9kW.pdf",
            mime_type="application/pdf",
            folder_path="Umowy/Siedlec 9kW",
            probable_case_key="siedlec_9kw_panasonic_adc0309k3e5",
        )

        result = link_drive_candidate(candidate, extracted_facts=[], store=self.store)

        self.assertEqual(result["case_id"], "case_siedlec")
        self.assertEqual(result["linkage_status"], "deterministic")
        self.assertTrue(result["confidence"] >= 0.99)

    def test_address_plus_customer_overlap_returns_medium_inference(self) -> None:
        candidate = DriveIngestCandidate(
            drive_item_id="drv_2",
            title="Karta gwarancyjna Siedlec.pdf",
            mime_type="application/pdf",
            folder_path="Serwis/Gwarancje",
        )
        extracted_facts = [
            {"fact_key": "customer_name", "normalized_value": "jan kowalski"},
            {"fact_key": "installation_address", "normalized_value": "siedlec 12"},
        ]

        result = link_drive_candidate(candidate, extracted_facts=extracted_facts, store=self.store)

        self.assertEqual(result["case_id"], "case_siedlec")
        self.assertEqual(result["linkage_status"], "inferred_medium")
        self.assertTrue(0.58 <= result["confidence"] < 0.82)

    def test_snapshot_latest_documents_summary_can_drive_high_inference(self) -> None:
        self.store.upsert_case(
            {
                "case_id": "case_warranty_mail",
                "case_key": "thread:msg_warranty",
                "thread_id": "thr_warranty",
                "case_family": "service",
                "mailbox": "gmail",
                "subject": "Twoje wnioski o uruchomienie",
                "status": "awaiting_review",
                "customer_name": "PanasonicProClub",
                "customer_email": "no-reply@panasonicproclub.com",
                "metadata": {},
                "created_at": "2026-04-12T08:00:00+02:00",
                "updated_at": "2026-04-12T08:00:00+02:00",
            }
        )
        self.store.upsert_snapshot(
            "case_warranty_mail",
            {
                "status": "awaiting_review",
                "customer_name": "PanasonicProClub",
                "customer_email": "no-reply@panasonicproclub.com",
                "recommended_next_action": "",
                "snapshot_json": {
                    "key_facts": [],
                    "latest_documents": [
                        {
                            "file_name": "W469055C517436.pdf",
                            "summary_text": "Adres montażu Siedlec 229 Krzeszowice - 32-065 Model WH-UDZ09KE5, WH-ADC0309K3E5",
                        }
                    ],
                },
                "updated_at": "2026-04-12T08:00:00+02:00",
            },
        )
        candidate = DriveIngestCandidate(
            drive_item_id="drv_3",
            title="gwarancja-siedlec.pdf",
            mime_type="application/pdf",
            folder_path="Karty gwarancyjne",
        )
        extracted_facts = [
            {"fact_key": "installation_address", "normalized_value": "siedlec 229"},
            {"fact_key": "device_model", "normalized_value": "wh-adc0309k3e5"},
            {"fact_key": "device_model", "normalized_value": "wh-udz09ke5"},
        ]

        result = link_drive_candidate(candidate, extracted_facts=extracted_facts, store=self.store)

        self.assertEqual(result["case_id"], "case_warranty_mail")
        self.assertEqual(result["linkage_status"], "inferred_high")
        self.assertTrue(result["confidence"] >= 0.82)

    def test_model_and_customer_without_address_stays_below_medium_threshold(self) -> None:
        self.store.upsert_case(
            {
                "case_id": "case_generic_warranty",
                "case_key": "thread:msg_generic",
                "thread_id": "thr_generic",
                "case_family": "service",
                "mailbox": "gmail",
                "subject": "Twoje wnioski o uruchomienie",
                "status": "awaiting_review",
                "customer_name": "PanasonicProClub",
                "customer_email": "no-reply@panasonicproclub.com",
                "metadata": {},
                "created_at": "2026-04-12T08:00:00+02:00",
                "updated_at": "2026-04-12T08:00:00+02:00",
            }
        )
        self.store.upsert_snapshot(
            "case_generic_warranty",
            {
                "status": "awaiting_review",
                "customer_name": "PanasonicProClub",
                "customer_email": "no-reply@panasonicproclub.com",
                "recommended_next_action": "",
                "snapshot_json": {
                    "key_facts": [],
                    "latest_documents": [
                        {
                            "file_name": "W469055C517436.pdf",
                            "summary_text": "Model WH-UDZ09KE5, WH-ADC0309K3E5 PanasonicProClub info.pl.hvac@eu.panasonic.com",
                        }
                    ],
                },
                "updated_at": "2026-04-12T08:00:00+02:00",
            },
        )
        candidate = DriveIngestCandidate(
            drive_item_id="drv_4",
            title="GwarancjaSosnowiecDojazdowa.pdf",
            mime_type="application/pdf",
            folder_path="Karty gwarancyjne",
        )
        extracted_facts = [
            {"fact_key": "device_model", "normalized_value": "wh-adc0309k3e5"},
            {"fact_key": "customer_email", "normalized_value": "info.pl.hvac@eu.panasonic.com"},
        ]

        result = link_drive_candidate(candidate, extracted_facts=extracted_facts, store=self.store)

        self.assertEqual(result["case_id"], "")
        self.assertEqual(result["linkage_status"], "unresolved_candidate")


if __name__ == "__main__":
    unittest.main()
