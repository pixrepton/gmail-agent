from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parent.parent

if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import Settings
from gmail_intake import run_case_context_command
from mailbox_memory_models import CaseContextPack
from mailbox_memory_store import InMemoryMailboxMemoryStore
from neo4j_pilot import build_case_context_neo4j_pilot_block, build_case_projection_payload


def make_settings(**overrides: object) -> Settings:
    base = {
        "llm_backend": "groq",
        "openai_compat_base_url": "",
        "openai_compat_api_key": "",
        "groq_api_key": "",
        "google_access_token": "",
        "google_client_id": "",
        "google_client_secret": "",
        "google_refresh_token": "",
        "google_token_endpoint": "https://oauth2.googleapis.com/token",
        "google_oauth_scopes": ("https://www.googleapis.com/auth/gmail.readonly",),
        "groq_model": "openai/gpt-oss-120b",
        "groq_native_model": "openai/gpt-oss-120b",
        "groq_base_url": "https://api.groq.com",
        "daszek_base_url": "",
        "daszek_login": "",
        "daszek_password": "",
        "daszek_v2_push_enabled": False,
        "case_guidance_enabled": False,
        "case_guidance_model": "openai/gpt-oss-120b",
        "case_guidance_remote_state_enabled": False,
        "attachment_extraction_enabled": True,
        "attachment_extraction_max_bytes": 8_000_000,
        "mailbox_memory_database_url": "postgresql://mailbox_memory:memorka@127.0.0.1:54329/mailbox_memory",
        "mailbox_memory_blob_root": Path("tools/gmail_audit/data/mailbox_memory/blobs"),
        "mailbox_memory_stage_mode": "shadow",
        "mailbox_memory_stage_allowlist": (),
        "google_drive_enabled": False,
        "google_drive_credentials_path": None,
        "google_drive_shared_drive_id": "",
        "google_drive_root_folder_id": "",
        "google_drive_batch_page_size": 100,
        "google_drive_max_download_bytes": 10_000_000,
        "google_drive_ingest_enabled": False,
        "google_drive_graph_enabled": False,
        "neo4j_pilot_enabled": False,
        "neo4j_uri": "",
        "neo4j_username": "",
        "neo4j_password": "",
        "neo4j_database": "neo4j",
        "gmail_agent_otel_enabled": False,
        "gmail_agent_otel_local_mirror_enabled": True,
        "otel_service_name": "gmail-agent",
        "otel_exporter_otlp_endpoint": "",
        "otel_exporter_otlp_headers": "",
        "mailbox_memory_vector_enabled": False,
        "openai_compat_embedding_model": "",
        "openai_compat_embedding_dimensions": 0,
        "docling_enabled": False,
        "docling_max_pages": 40,
        "docling_timeout_sec": 45,
        "signal_runtime_mode": "legacy",
        "signal_journal_jsonl_mirror_enabled": False,
        "gmail_change_detection_enabled": False,
        "drive_change_detection_enabled": False,
        "signal_worker_enabled": False,
        "gmail_history_poll_interval_sec": 120,
        "drive_changes_poll_interval_sec": 180,
        "http_timeout": 60,
        "http_max_retries": 4,
        "http_retry_base_delay": 2.0,
        "env_path": Path("tools/gmail_audit/.env"),
        "config_sources": {},
        "config_warnings": [],
        "google_access_token_had_bearer_prefix": False,
        "google_runtime_access_token": "",
        "google_runtime_access_token_expires_at": 0.0,
        "google_runtime_token_type": "",
        "google_active_token_source": "",
    }
    base.update(overrides)
    return Settings(**base)


def populate_cross_source_case(store: InMemoryMailboxMemoryStore, *, case_id: str = "case_cross_source") -> None:
    store.bootstrap()
    timestamp = "2026-04-22T18:00:00+02:00"
    store.upsert_case(
        {
            "case_id": case_id,
            "case_key": "thread:msg-1",
            "thread_id": "thr-1",
            "case_family": "service",
            "mailbox": "biuro.topinstal@gmail.com",
            "subject": "Twoje wnioski o uruchomienie",
            "status": "awaiting_review",
            "customer_name": "PanasonicProClub",
            "customer_email": "no-reply@panasonicproclub.com",
            "metadata": {"installation_address": "Siedlec 229", "city": "Siedlec"},
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    )
    store.upsert_snapshot(
        case_id,
        {
            "status": "awaiting_review",
            "customer_name": "PanasonicProClub",
            "customer_email": "no-reply@panasonicproclub.com",
            "recommended_next_action": "hold",
            "snapshot_json": {
                "status": "awaiting_review",
                "customer": {
                    "name": "PanasonicProClub",
                    "email": "no-reply@panasonicproclub.com",
                },
                "key_facts": [
                    {"fact_key": "installation_address", "value": "Siedlec 229"},
                    {"fact_key": "city", "value": "Siedlec"},
                ],
            },
            "updated_at": timestamp,
        },
    )
    store.upsert_message(
        {
            "message_id": "msg-1",
            "case_id": case_id,
            "thread_id": "thr-1",
            "mailbox": "biuro.topinstal@gmail.com",
            "sender": "PanasonicProClub <no-reply@panasonicproclub.com>",
            "sender_email": "no-reply@panasonicproclub.com",
            "recipients": ["biuro.topinstal@gmail.com"],
            "subject": "Twoje wnioski o uruchomienie",
            "snippet": "",
            "body_text": "",
            "labels": [],
            "received_at": timestamp,
            "raw_snapshot": {},
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    )
    store.upsert_document(
        {
            "document_id": "doc-mail-1",
            "case_id": case_id,
            "message_id": "msg-1",
            "attachment_id": "att-1",
            "parent_document_id": "",
            "file_name": "W469055C517436.pdf",
            "mime_type": "application/pdf",
            "source_type": "attachment",
            "document_kind": "pdf",
            "extraction_status": "extracted",
            "parser_name": "fallback",
            "content_sha256": "mailbox-sha",
            "blob_path": "/tmp/mailbox.pdf",
            "text_content": "Adres montazu Siedlec 229",
            "summary_text": "Mailbox warranty attachment.",
            "metadata": {},
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    )
    store.append_fact_rows(
        [
            {
                "fact_id": "fact-mail-location",
                "case_id": case_id,
                "message_id": "msg-1",
                "document_id": "doc-mail-1",
                "entity_scope": "document",
                "fact_key": "installation_address",
                "normalized_value": "Siedlec 229",
                "raw_value": "Siedlec 229",
                "confidence": 0.9,
                "observed_at": timestamp,
                "source_type": "document",
                "source_ref": "doc-mail-1",
                "status": "active",
                "metadata": {},
                "created_at": timestamp,
            },
            {
                "fact_id": "fact-mail-city",
                "case_id": case_id,
                "message_id": "msg-1",
                "document_id": "doc-mail-1",
                "entity_scope": "document",
                "fact_key": "city",
                "normalized_value": "Siedlec",
                "raw_value": "Siedlec",
                "confidence": 0.85,
                "observed_at": timestamp,
                "source_type": "document",
                "source_ref": "doc-mail-1",
                "status": "active",
                "metadata": {},
                "created_at": timestamp,
            },
        ]
    )
    store.upsert_drive_document(
        {
            "document_id": "gdoc-drive-1",
            "drive_item_id": "drive-item-1",
            "parent_drive_item_id": "",
            "parent_document_id": "",
            "case_id": case_id,
            "probable_case_key": "thread:msg-1",
            "file_name": "gwarancja-siedlec.pdf",
            "mime_type": "application/pdf",
            "folder_path": "Karty gwarancyjne",
            "lane": "service_warranty",
            "document_kind": "warranty_card",
            "scope": "case_specific",
            "source_ref": "https://drive.google.com/file/d/drive-item-1",
            "extraction_status": "extracted",
            "linkage_status": "inferred_high",
            "classification_confidence": 0.95,
            "extraction_confidence": 0.91,
            "link_confidence": 0.88,
            "download_mime_type": "application/pdf",
            "content_sha256": "drive-sha",
            "blob_path": "/tmp/drive.pdf",
            "text_content": "Adres montazu Siedlec 229",
            "summary_text": "Drive warranty document.",
            "metadata": {},
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    )
    store.replace_drive_document_facts(
        document_id="gdoc-drive-1",
        rows=[
            {
                "fact_id": "fact-drive-location",
                "drive_document_id": "gdoc-drive-1",
                "case_id": case_id,
                "probable_case_key": "thread:msg-1",
                "fact_family": "warranty",
                "entity_scope": "document",
                "fact_key": "installation_address",
                "normalized_value": "Siedlec 229",
                "raw_value": "Siedlec 229",
                "confidence": 0.91,
                "observed_at": timestamp,
                "source_ref": "https://drive.google.com/file/d/drive-item-1",
                "status": "active",
                "metadata": {},
                "created_at": timestamp,
            },
            {
                "fact_id": "fact-drive-city",
                "drive_document_id": "gdoc-drive-1",
                "case_id": case_id,
                "probable_case_key": "thread:msg-1",
                "fact_family": "warranty",
                "entity_scope": "document",
                "fact_key": "city",
                "normalized_value": "Siedlec",
                "raw_value": "Siedlec",
                "confidence": 0.9,
                "observed_at": timestamp,
                "source_ref": "https://drive.google.com/file/d/drive-item-1",
                "status": "active",
                "metadata": {},
                "created_at": timestamp,
            },
        ],
    )


def make_context_pack(*, case_id: str = "case_cross_source") -> dict[str, object]:
    return {
        "relevant_chunks": [
            {"document_id": "doc-mail-1", "source_type": "mailbox_document_chunk"},
            {"document_id": "gdoc-drive-1", "source_type": "drive_document_chunk"},
            {"document_id": "doc-mail-1", "source_type": "mailbox_document_chunk"},
        ],
        "graph_hints": [],
        "completeness_gaps": [],
        "conflicting_facts": [],
        "case_id": case_id,
    }


class _FakeRuntime:
    def __init__(self, *, store: InMemoryMailboxMemoryStore, pack: CaseContextPack) -> None:
        self.store = store
        self._pack = pack

    def bootstrap(self) -> None:
        return None

    def get_context_pack(self, *, case_id: str = "", message_id: str = "", query_text: str = "") -> CaseContextPack:
        return self._pack


class _FakeNeo4jBackend:
    def __init__(self) -> None:
        self.anchor_calls: list[list[str]] = []
        self.replace_payloads: list[object] = []

    def replace_case_projection(self, payload):  # noqa: ANN001
        self.replace_payloads.append(payload)
        return {
            "status": "ok",
            "case_id": payload.case_id,
            "projected": True,
            "deleted_existing_subgraph": True,
            "node_count": len(payload.nodes),
            "relationship_count": len(payload.relationships),
            "warnings": [],
        }

    def fetch_case_neighborhood(self, *, case_id: str, anchor_node_keys: list[str], max_hops: int, limit: int) -> dict[str, object]:
        self.anchor_calls.append(list(anchor_node_keys))
        case_node = {
            "node_key": f"Case:{case_id}",
            "labels": ["Case"],
            "value": f"thread:{case_id}",
            "case_id": case_id,
            "source_kind": "mailbox_memory_case",
        }
        message_node = {
            "node_key": "Message:msg-1",
            "labels": ["Message"],
            "value": "Twoje wnioski o uruchomienie",
            "case_id": case_id,
            "message_id": "msg-1",
            "source_kind": "gmail_message",
        }
        mailbox_document = {
            "node_key": "Document:mailbox:doc-mail-1",
            "labels": ["Document"],
            "value": "W469055C517436.pdf",
            "case_id": case_id,
            "document_id": "doc-mail-1",
            "document_kind": "pdf",
            "source_kind": "mailbox_document",
        }
        drive_document = {
            "node_key": "Document:drive:gdoc-drive-1",
            "labels": ["Document"],
            "value": "gwarancja-siedlec.pdf",
            "case_id": case_id,
            "document_id": "gdoc-drive-1",
            "document_kind": "warranty_card",
            "source_kind": "drive_document",
        }
        location_node = {
            "node_key": f"Location:{case_id}:siedlec-229",
            "labels": ["Location"],
            "value": "Siedlec 229",
            "case_id": case_id,
            "address": "Siedlec 229",
            "city": "Siedlec",
            "source_kind": "case_location",
        }
        contact_node = {
            "node_key": f"Contact:{case_id}:no-reply@panasonicproclub.com",
            "labels": ["Contact"],
            "value": "PanasonicProClub",
            "case_id": case_id,
            "email": "no-reply@panasonicproclub.com",
            "name": "PanasonicProClub",
            "source_kind": "case_customer",
        }
        nodes_by_key = {
            case_node["node_key"]: case_node,
            message_node["node_key"]: message_node,
            mailbox_document["node_key"]: mailbox_document,
            drive_document["node_key"]: drive_document,
            location_node["node_key"]: location_node,
            contact_node["node_key"]: contact_node,
        }
        anchor_nodes = [nodes_by_key[key] for key in anchor_node_keys if key in nodes_by_key]
        anchor_documents = [node for node in anchor_nodes if "Document" in list(node.get("labels") or [])]
        paths: list[dict[str, object]] = [
            {
                "origin": "case",
                "rel_chain": ["HAS_MESSAGE"],
                "nodes": [case_node, message_node],
            },
            {
                "origin": "case",
                "rel_chain": ["HAS_DOCUMENT", "MESSAGE_HAS_DOCUMENT"],
                "nodes": [case_node, mailbox_document, message_node],
            },
            {
                "origin": "case",
                "rel_chain": ["HAS_DOCUMENT", "MENTIONS_LOCATION"],
                "nodes": [case_node, drive_document, location_node],
            },
            {
                "origin": "case",
                "rel_chain": ["HAS_CONTACT"],
                "nodes": [case_node, contact_node],
            },
            {
                "origin": "case",
                "rel_chain": ["HAS_LOCATION"],
                "nodes": [case_node, location_node],
            },
        ]
        for anchor in anchor_nodes:
            labels = list(anchor.get("labels") or [])
            if "Document" in labels and "mailbox_document" == str(anchor.get("source_kind") or ""):
                paths.append(
                    {
                        "origin": "anchor",
                        "rel_chain": ["MESSAGE_HAS_DOCUMENT"],
                        "nodes": [anchor, message_node],
                    }
                )
            elif "Document" in labels:
                paths.append(
                    {
                        "origin": "anchor",
                        "rel_chain": ["MENTIONS_LOCATION"],
                        "nodes": [anchor, location_node],
                    }
                )
            elif "Location" in labels:
                paths.append(
                    {
                        "origin": "anchor",
                        "rel_chain": ["MENTIONS_LOCATION"],
                        "nodes": [anchor, mailbox_document],
                    }
                )
                paths.append(
                    {
                        "origin": "anchor",
                        "rel_chain": ["MENTIONS_LOCATION"],
                        "nodes": [anchor, drive_document],
                    }
                )
            elif "Contact" in labels:
                paths.append(
                    {
                        "origin": "anchor",
                        "rel_chain": ["HAS_CONTACT"],
                        "nodes": [anchor, case_node],
                    }
                )
        neighborhood_nodes = [case_node, message_node, mailbox_document, drive_document, location_node, contact_node]
        return {
            "status": "ok",
            "case_id": case_id,
            "anchor_documents": anchor_documents,
            "anchor_nodes": anchor_nodes,
            "neighborhood_nodes": neighborhood_nodes,
            "paths": paths[:limit],
            "max_hops": max_hops,
            "limit": limit,
            "warnings": [],
        }

    def close(self) -> None:
        return None


class _SparseNeo4jBackend(_FakeNeo4jBackend):
    def fetch_case_neighborhood(self, *, case_id: str, anchor_node_keys: list[str], max_hops: int, limit: int) -> dict[str, object]:
        self.anchor_calls.append(list(anchor_node_keys))
        case_node = {
            "node_key": f"Case:{case_id}",
            "labels": ["Case"],
            "value": f"thread:{case_id}",
            "case_id": case_id,
            "source_kind": "mailbox_memory_case",
        }
        mailbox_document = {
            "node_key": "Document:mailbox:doc-mail-1",
            "labels": ["Document"],
            "value": "W469055C517436.pdf",
            "case_id": case_id,
            "document_id": "doc-mail-1",
            "document_kind": "pdf",
            "source_kind": "mailbox_document",
        }
        return {
            "status": "ok",
            "case_id": case_id,
            "anchor_documents": [mailbox_document],
            "anchor_nodes": [mailbox_document],
            "neighborhood_nodes": [case_node, mailbox_document],
            "paths": [
                {
                    "origin": "case",
                    "rel_chain": ["HAS_DOCUMENT"],
                    "nodes": [case_node, mailbox_document],
                }
            ],
            "max_hops": max_hops,
            "limit": limit,
            "warnings": [],
        }


class _FailingNeo4jBackend(_FakeNeo4jBackend):
    def fetch_case_neighborhood(self, *, case_id: str, anchor_node_keys: list[str], max_hops: int, limit: int) -> dict[str, object]:
        raise RuntimeError("Neo4j unavailable for bounded retrieval.")


class Neo4jPilotTests(unittest.TestCase):
    def test_build_case_projection_payload_for_cross_source_case(self) -> None:
        store = InMemoryMailboxMemoryStore()
        populate_cross_source_case(store)

        payload = build_case_projection_payload(store=store, case_id="case_cross_source")

        labels = [row["label"] for row in payload.nodes]
        rel_types = [row["type"] for row in payload.relationships]
        self.assertEqual(labels.count("Case"), 1)
        self.assertEqual(labels.count("Message"), 1)
        self.assertEqual(labels.count("Document"), 2)
        self.assertEqual(labels.count("Contact"), 1)
        self.assertEqual(labels.count("Location"), 1)
        self.assertEqual(rel_types.count("HAS_MESSAGE"), 1)
        self.assertEqual(rel_types.count("HAS_DOCUMENT"), 2)
        self.assertEqual(rel_types.count("MESSAGE_HAS_DOCUMENT"), 1)
        self.assertEqual(rel_types.count("HAS_CONTACT"), 1)
        self.assertEqual(rel_types.count("HAS_LOCATION"), 1)
        self.assertEqual(rel_types.count("MENTIONS_LOCATION"), 2)

    def test_case_context_omits_neo4j_block_without_flags(self) -> None:
        store = InMemoryMailboxMemoryStore()
        pack = CaseContextPack(case_id="case_cli", snapshot={"case_id": "case_cli"})
        runtime = _FakeRuntime(store=store, pack=pack)
        settings = make_settings(neo4j_pilot_enabled=False)
        emitted: list[dict[str, object]] = []
        args = argparse.Namespace(
            case_id="case_cli",
            message_id="",
            query_text="",
            neo4j_project=False,
            neo4j_graph_aware=False,
            neo4j_max_hops=2,
            neo4j_limit=10,
            neo4j_anchor_mode="auto",
            verbose=False,
            proof_telemetry_dir=None,
        )

        with mock.patch("gmail_intake.load_settings", return_value=settings):
            with mock.patch("gmail_intake._require_mailbox_memory_runtime", return_value=runtime):
                with mock.patch("gmail_intake._emit_json", side_effect=emitted.append):
                    exit_code = run_case_context_command(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(emitted[0]["case_id"], "case_cli")
        self.assertNotIn("neo4j_pilot", emitted[0])

    def test_case_context_keeps_normal_payload_when_neo4j_config_is_incomplete(self) -> None:
        store = InMemoryMailboxMemoryStore()
        pack = CaseContextPack(case_id="case_cli", snapshot={"case_id": "case_cli", "status": "open"})
        runtime = _FakeRuntime(store=store, pack=pack)
        settings = make_settings(
            neo4j_pilot_enabled=True,
            neo4j_uri="",
            neo4j_username="",
            neo4j_password="",
        )
        emitted: list[dict[str, object]] = []
        args = argparse.Namespace(
            case_id="case_cli",
            message_id="",
            query_text="gwarancja Panasonic Siedlec",
            neo4j_project=True,
            neo4j_graph_aware=True,
            neo4j_max_hops=2,
            neo4j_limit=10,
            neo4j_anchor_mode="auto",
            verbose=False,
            proof_telemetry_dir=None,
        )

        with mock.patch("gmail_intake.load_settings", return_value=settings):
            with mock.patch("gmail_intake._require_mailbox_memory_runtime", return_value=runtime):
                with mock.patch("gmail_intake._emit_json", side_effect=emitted.append):
                    exit_code = run_case_context_command(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(emitted[0]["case_id"], "case_cli")
        self.assertIn("neo4j_pilot", emitted[0])
        self.assertEqual(emitted[0]["neo4j_pilot"]["status"], "failed")
        self.assertEqual(emitted[0]["neo4j_pilot"]["projection"]["status"], "failed")
        self.assertEqual(emitted[0]["neo4j_pilot"]["retrieval"]["status"], "failed")

    def test_graph_aware_auto_builds_explanations_cards_and_snapshot(self) -> None:
        store = InMemoryMailboxMemoryStore()
        populate_cross_source_case(store)
        backend = _FakeNeo4jBackend()
        settings = make_settings(
            neo4j_pilot_enabled=True,
            neo4j_uri="neo4j://127.0.0.1:7687",
            neo4j_username="neo4j",
            neo4j_password="secret",
        )

        block = build_case_context_neo4j_pilot_block(
            settings=settings,
            store=store,
            case_id="case_cross_source",
            context_pack=make_context_pack(),
            project=False,
            graph_aware=True,
            max_hops=2,
            limit=10,
            anchor_mode="auto",
            backend=backend,
        )

        self.assertEqual(block["status"], "ok")
        self.assertEqual(block["anchoring"]["resolved_mode"], "document")
        self.assertEqual(
            [item["anchor_type"] for item in block["anchoring"]["selected_anchors"]],
            ["document", "document"],
        )
        self.assertEqual(
            backend.anchor_calls[0],
            ["Document:mailbox:doc-mail-1", "Document:drive:gdoc-drive-1"],
        )
        self.assertEqual(block["retrieval"]["status"], "ok")
        self.assertGreaterEqual(len(block["retrieval"]["paths"]), 3)
        self.assertIn("path_summary", block["retrieval"]["paths"][0])
        self.assertIn("importance_reason", block["retrieval"]["paths"][0])
        self.assertIn("priority_score", block["retrieval"]["paths"][0])
        self.assertTrue(block["why_this_matters"])
        self.assertTrue(block["evidence_cards"])
        self.assertEqual(block["snapshot"]["anchor_mode_used"], "document")
        self.assertIn("node_summary", block["snapshot"])
        self.assertIn("gap_summary", block["snapshot"])
        self.assertEqual(block["gaps"], [])
        self.assertEqual(block["inconsistencies"], [])
        self.assertEqual(block["graph_warnings"], [])
        self.assertTrue(
            any(
                "MESSAGE_HAS_DOCUMENT" in item["importance_reason"] or "MENTIONS_LOCATION" in item["importance_reason"]
                for item in block["why_this_matters"]
            )
        )
        self.assertTrue(
            any(card["confidence_mode"] == "hard_relation_chain_cross_source" for card in block["evidence_cards"])
        )

    def test_anchor_mode_location_and_contact_change_selected_anchors(self) -> None:
        store = InMemoryMailboxMemoryStore()
        populate_cross_source_case(store)
        settings = make_settings(
            neo4j_pilot_enabled=True,
            neo4j_uri="neo4j://127.0.0.1:7687",
            neo4j_username="neo4j",
            neo4j_password="secret",
        )
        location_backend = _FakeNeo4jBackend()
        location_block = build_case_context_neo4j_pilot_block(
            settings=settings,
            store=store,
            case_id="case_cross_source",
            context_pack=make_context_pack(),
            project=False,
            graph_aware=True,
            max_hops=2,
            limit=10,
            anchor_mode="location",
            backend=location_backend,
        )
        self.assertEqual(location_block["anchoring"]["resolved_mode"], "location")
        self.assertEqual(location_block["anchoring"]["selected_anchors"][0]["anchor_type"], "location")
        self.assertTrue(location_backend.anchor_calls[0][0].startswith("Location:case_cross_source:"))

        contact_backend = _FakeNeo4jBackend()
        contact_block = build_case_context_neo4j_pilot_block(
            settings=settings,
            store=store,
            case_id="case_cross_source",
            context_pack=make_context_pack(),
            project=False,
            graph_aware=True,
            max_hops=2,
            limit=10,
            anchor_mode="contact",
            backend=contact_backend,
        )
        self.assertEqual(contact_block["anchoring"]["resolved_mode"], "contact")
        self.assertEqual(contact_block["anchoring"]["selected_anchors"][0]["anchor_type"], "contact")
        self.assertTrue(contact_backend.anchor_calls[0][0].startswith("Contact:case_cross_source:"))

    def test_gap_detection_flags_missing_contact_and_location(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        timestamp = "2026-04-22T18:00:00+02:00"
        store.upsert_case(
            {
                "case_id": "case_sparse",
                "case_key": "thread:msg-1",
                "thread_id": "thr-1",
                "case_family": "service",
                "mailbox": "biuro.topinstal@gmail.com",
                "subject": "Sparse case",
                "status": "open",
                "customer_name": "",
                "customer_email": "",
                "metadata": {},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        store.upsert_snapshot(
            "case_sparse",
            {
                "status": "open",
                "snapshot_json": {"status": "open"},
                "updated_at": timestamp,
            },
        )
        store.upsert_document(
            {
                "document_id": "doc-mail-1",
                "case_id": "case_sparse",
                "message_id": "",
                "attachment_id": "att-1",
                "parent_document_id": "",
                "file_name": "sparse.pdf",
                "mime_type": "application/pdf",
                "source_type": "attachment",
                "document_kind": "pdf",
                "extraction_status": "extracted",
                "parser_name": "fallback",
                "content_sha256": "mailbox-sha",
                "blob_path": "/tmp/mailbox.pdf",
                "text_content": "",
                "summary_text": "Sparse document.",
                "metadata": {},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        settings = make_settings(
            neo4j_pilot_enabled=True,
            neo4j_uri="neo4j://127.0.0.1:7687",
            neo4j_username="neo4j",
            neo4j_password="secret",
        )

        block = build_case_context_neo4j_pilot_block(
            settings=settings,
            store=store,
            case_id="case_sparse",
            context_pack={
                "relevant_chunks": [
                    {"document_id": "doc-mail-1", "source_type": "mailbox_document_chunk"},
                ]
            },
            project=False,
            graph_aware=True,
            max_hops=2,
            limit=10,
            anchor_mode="auto",
            backend=_SparseNeo4jBackend(),
        )

        gap_codes = {item["code"] for item in block["gaps"]}
        warning_codes = {item["code"] for item in block["graph_warnings"]}
        self.assertIn("documents_without_contact", gap_codes)
        self.assertIn("documents_without_location", gap_codes)
        self.assertIn("neighborhood_too_sparse_for_cross_source_claim", warning_codes)

    def test_failure_safe_when_backend_is_unavailable(self) -> None:
        store = InMemoryMailboxMemoryStore()
        populate_cross_source_case(store)
        settings = make_settings(
            neo4j_pilot_enabled=True,
            neo4j_uri="neo4j://127.0.0.1:7687",
            neo4j_username="neo4j",
            neo4j_password="secret",
        )

        block = build_case_context_neo4j_pilot_block(
            settings=settings,
            store=store,
            case_id="case_cross_source",
            context_pack=make_context_pack(),
            project=False,
            graph_aware=True,
            max_hops=2,
            limit=10,
            anchor_mode="auto",
            backend=_FailingNeo4jBackend(),
        )

        self.assertEqual(block["status"], "failed")
        self.assertEqual(block["retrieval"]["status"], "failed")
        self.assertIn("Neo4j unavailable", block["retrieval"]["error"])


if __name__ == "__main__":
    unittest.main()
