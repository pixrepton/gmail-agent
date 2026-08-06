"""FACT-02: Neo4j projection / precedent readers must not treat superseded facts as current."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent

if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory_store import InMemoryMailboxMemoryStore
from neo4j_pilot import _best_fact_value, _document_has_location, build_case_projection_payload
from similar_cases_precedent import _active_fact_keys


class Fact02ActiveFactProjectionTests(unittest.TestCase):
    def test_best_fact_value_ignores_superseded_even_with_higher_confidence(self) -> None:
        facts = [
            {
                "fact_key": "installation_address",
                "normalized_value": "Stary Adres 1",
                "confidence": 0.99,
                "observed_at": "2026-08-01T10:00:00Z",
                "status": "superseded",
            },
            {
                "fact_key": "installation_address",
                "normalized_value": "Nowy Adres 2",
                "confidence": 0.55,
                "observed_at": "2026-08-02T10:00:00Z",
                "status": "active",
            },
            {
                "fact_key": "seller_nip",
                "normalized_value": "1111111111",
                "confidence": 0.95,
                "observed_at": "2026-08-01T10:00:00Z",
                "status": "superseded",
            },
            {
                "fact_key": "seller_nip",
                "normalized_value": "2222222222",
                "confidence": 0.5,
                "observed_at": "2026-08-02T10:00:00Z",
                "status": "active",
            },
            {
                "fact_key": "phone",
                "normalized_value": "+48111111111",
                "confidence": 0.9,
                "observed_at": "2026-08-01T10:00:00Z",
                "status": "superseded",
            },
            {
                "fact_key": "phone",
                "normalized_value": "+48222222222",
                "confidence": 0.4,
                "observed_at": "2026-08-02T10:00:00Z",
                # Missing status must still count as live (schema default / older producers).
            },
        ]
        self.assertEqual(_best_fact_value(facts, "installation_address"), "Nowy Adres 2")
        self.assertEqual(_best_fact_value(facts, "seller_nip"), "2222222222")
        self.assertEqual(_best_fact_value(facts, "phone"), "+48222222222")

    def test_document_has_location_ignores_superseded_only_rows(self) -> None:
        superseded_only = [
            {
                "fact_key": "installation_address",
                "normalized_value": "Stary Adres 1",
                "status": "superseded",
            }
        ]
        live = [
            {
                "fact_key": "city",
                "normalized_value": "Radlin",
                "status": "active",
            }
        ]
        self.assertFalse(_document_has_location(superseded_only))
        self.assertTrue(_document_has_location(live))

    def test_build_case_projection_payload_location_uses_active_not_superseded(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        case_id = "case_fact02_location"
        timestamp = "2026-08-03T09:00:00Z"
        store.upsert_case(
            {
                "case_id": case_id,
                "case_key": "thread:fact02",
                "thread_id": "thr-fact02",
                "case_family": "service",
                "mailbox": "biuro.topinstal@gmail.com",
                "subject": "FACT-02 location supersession",
                "status": "open",
                "customer_name": "Klient",
                "customer_email": "klient@example.com",
                # Empty metadata so Location comes from facts, not case row fallback.
                "metadata": {},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        store.upsert_snapshot(
            case_id,
            {
                "status": "open",
                "snapshot_json": {
                    "status": "open",
                    "key_facts": [],
                },
                "updated_at": timestamp,
            },
        )
        store.append_fact_rows(
            [
                {
                    "fact_id": "fact-old-address",
                    "case_id": case_id,
                    "message_id": "",
                    "document_id": "doc-old",
                    "entity_scope": "case",
                    "fact_key": "installation_address",
                    "normalized_value": "Stary Adres 1",
                    "raw_value": "Stary Adres 1",
                    "confidence": 0.99,
                    "observed_at": "2026-08-01T10:00:00Z",
                    "source_type": "document",
                    "source_ref": "doc-old",
                    "status": "superseded",
                    "metadata": {},
                    "created_at": timestamp,
                },
                {
                    "fact_id": "fact-new-address",
                    "case_id": case_id,
                    "message_id": "",
                    "document_id": "doc-new",
                    "entity_scope": "case",
                    "fact_key": "installation_address",
                    "normalized_value": "Nowy Adres 2",
                    "raw_value": "Nowy Adres 2",
                    "confidence": 0.55,
                    "observed_at": "2026-08-02T10:00:00Z",
                    "source_type": "document",
                    "source_ref": "doc-new",
                    "status": "active",
                    "metadata": {},
                    "created_at": timestamp,
                },
                {
                    "fact_id": "fact-city",
                    "case_id": case_id,
                    "message_id": "",
                    "document_id": "doc-new",
                    "entity_scope": "case",
                    "fact_key": "city",
                    "normalized_value": "Radlin",
                    "raw_value": "Radlin",
                    "confidence": 0.8,
                    "observed_at": "2026-08-02T10:00:00Z",
                    "source_type": "document",
                    "source_ref": "doc-new",
                    "status": "active",
                    "metadata": {},
                    "created_at": timestamp,
                },
            ]
        )

        payload = build_case_projection_payload(store=store, case_id=case_id)
        locations = [node for node in payload.nodes if node.get("label") == "Location"]
        self.assertEqual(len(locations), 1)
        props = locations[0].get("properties") or {}
        self.assertEqual(props.get("address"), "Nowy Adres 2")
        self.assertNotEqual(props.get("address"), "Stary Adres 1")
        self.assertEqual(props.get("city"), "Radlin")

        # Document with only a superseded location fact must not mint MENTIONS_LOCATION.
        store.upsert_document(
            {
                "document_id": "doc-old",
                "case_id": case_id,
                "message_id": "",
                "attachment_id": "",
                "parent_document_id": "",
                "file_name": "old.pdf",
                "mime_type": "application/pdf",
                "source_type": "attachment",
                "document_kind": "pdf",
                "extraction_status": "extracted",
                "parser_name": "fallback",
                "content_sha256": "old-sha",
                "blob_path": "/tmp/old.pdf",
                "text_content": "Stary Adres 1",
                "summary_text": "old",
                "metadata": {},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        store.upsert_document(
            {
                "document_id": "doc-new",
                "case_id": case_id,
                "message_id": "",
                "attachment_id": "",
                "parent_document_id": "",
                "file_name": "new.pdf",
                "mime_type": "application/pdf",
                "source_type": "attachment",
                "document_kind": "pdf",
                "extraction_status": "extracted",
                "parser_name": "fallback",
                "content_sha256": "new-sha",
                "blob_path": "/tmp/new.pdf",
                "text_content": "Nowy Adres 2",
                "summary_text": "new",
                "metadata": {},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        payload2 = build_case_projection_payload(store=store, case_id=case_id)
        mention_srcs = {
            rel["src_node_key"]
            for rel in payload2.relationships
            if rel.get("type") == "MENTIONS_LOCATION"
        }
        self.assertNotIn("Document:mailbox:doc-old", mention_srcs)
        self.assertIn("Document:mailbox:doc-new", mention_srcs)

    def test_active_fact_keys_excludes_superseded(self) -> None:
        keys = _active_fact_keys(
            [
                {"fact_key": "heated_area_m2", "status": "superseded"},
                {"fact_key": "heated_area_m2", "status": "active"},
                {"fact_key": "seller_nip", "status": "superseded"},
                {"fact_key": "city", "status": "rejected"},
                {"fact_key": "phone", "status": "stale"},
                {"fact_key": "installation_address"},  # missing status => live
            ]
        )
        self.assertEqual(keys, {"heated_area_m2", "installation_address"})


if __name__ == "__main__":
    unittest.main()
