from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory_runtime import MailboxMemoryRuntime
from mailbox_memory_store import InMemoryMailboxMemoryStore, PostgresMailboxMemoryStore


POSTGRES_TEST_DATABASE_URL = os.getenv("MAILBOX_MEMORY_TEST_DATABASE_URL", "").strip()


def _build_docx_bytes(paragraphs: list[str]) -> bytes:
    xml_body = "".join(
        f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{xml_body}</w:body>"
        "</w:document>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def _build_xlsx_bytes(rows: list[list[str]]) -> bytes:
    shared_strings: list[str] = []
    string_indexes: dict[str, int] = {}

    def shared_index(value: str) -> int:
        if value not in string_indexes:
            string_indexes[value] = len(shared_strings)
            shared_strings.append(value)
        return string_indexes[value]

    sheet_rows: list[str] = []
    for row_number, values in enumerate(rows, start=1):
        cells: list[str] = []
        for column_offset, value in enumerate(values):
            column = chr(ord("A") + column_offset)
            idx = shared_index(value)
            cells.append(f'<c r="{column}{row_number}" t="s"><v>{idx}</v></c>')
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')

    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join(f"<si><t>{value}</t></si>" for value in shared_strings)
        + "</sst>"
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(sheet_rows)}</sheetData>"
        "</worksheet>"
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/sharedStrings.xml", shared_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()


def _build_zip_payload() -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "parametry.docx",
            _build_docx_bytes(
                [
                    "Powierzchnia 180 m2",
                    "Lokalizacja Jaworzno",
                    "Model Panasonic 9 kW",
                ]
            ),
        )
        archive.writestr(
            "zestawienie.xlsx",
            _build_xlsx_bytes(
                [
                    ["Pole", "Wartosc"],
                    ["Powierzchnia", "190 m2"],
                    ["Miasto", "Jaworzno"],
                    ["Model", "Panasonic 9 kW"],
                ]
            ),
        )
    return buffer.getvalue()


def _build_snapshot(zip_bytes: bytes) -> dict[str, object]:
    attachment = {
        "name": "projekt.zip",
        "mime_type": "application/zip",
        "size": len(zip_bytes),
        "attachment_id": "gmail-zip-1",
    }
    return {
        "mailbox": "biuro.topinstal@gmail.com",
        "observed_at": "2026-04-12T08:15:00+02:00",
        "source_message": {
            "message_id": "msg-mailbox-memory-1",
            "thread_id": "thr-mailbox-memory-1",
            "date": "2026-04-12T08:15:00+02:00",
            "sender": 'Jan Kowalski <jan.kowalski@example.com>',
            "from": 'Jan Kowalski <jan.kowalski@example.com>',
            "to": ["biuro@topinstal.pl"],
            "subject": "Prosba o oferte pompy ciepla dla domu w Jaworzno",
            "snippet": "Dom 180 m2, prosze o wycene.",
            "body": "Dzien dobry. Prosze o oferte. Lokalizacja: Jaworzno. Telefon 123 456 789.",
            "labels": ["INBOX", "UNREAD"],
            "has_attachments": True,
            "attachment_names": ["projekt.zip"],
            "attachment_parts": [attachment],
            "raw": {"attachments": [attachment]},
        },
        "context_messages": [],
    }


class MailboxMemoryRuntimeTests(unittest.TestCase):
    def test_ingest_and_finalize_build_snapshot_conflicts_and_context(self) -> None:
        zip_bytes = _build_zip_payload()
        snapshot = _build_snapshot(zip_bytes)
        intake_result = {
            "case_assessment": {"case_family": "heat_pump_offer"},
            "decision": {"action": "review"},
        }
        case_link_result = {
            "decision": "create_new",
            "selected_case_key": "CASE-2026-MEM-001",
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime = MailboxMemoryRuntime(
                store=InMemoryMailboxMemoryStore(),
                blob_root=Path(tmp_dir) / "blobs",
                stage_mode="shadow",
            )
            runtime.bootstrap()

            ingest_result = runtime.ingest_message(
                snapshot=snapshot,
                intake_result=intake_result,
                case_link_result=case_link_result,
                attachment_fetcher=lambda _message_id, _attachment_id: zip_bytes,
            )

            self.assertTrue(ingest_result.enabled)
            self.assertEqual(len(ingest_result.attachments), 1)
            document_names = {item["file_name"] for item in ingest_result.documents}
            self.assertIn("projekt.zip", document_names)
            self.assertIn("parametry.docx", document_names)
            self.assertIn("zestawienie.xlsx", document_names)

            snapshot_row = ingest_result.snapshot
            self.assertEqual(snapshot_row["status"], "awaiting_review")
            self.assertTrue(any(item["fact_key"] == "heated_area_m2" for item in snapshot_row["key_facts"]))
            self.assertTrue(any(item["fact_key"] == "heated_area_m2" for item in snapshot_row["conflicting_facts"]))
            self.assertTrue(any("heated_area_m2" in question for question in snapshot_row["open_questions"]))

            context_pack = runtime.get_context_pack(case_id=ingest_result.case_id, query_text="Panasonic Jaworzno")
            self.assertEqual(context_pack.case_id, ingest_result.case_id)
            self.assertTrue(any("Panasonic 9 kW" in chunk["chunk_text"] for chunk in context_pack.relevant_chunks))
            self.assertTrue(any(ref["type"] == "document" for ref in context_pack.source_refs))

            final_result = runtime.finalize_case(
                case_id=ingest_result.case_id,
                message_id="msg-mailbox-memory-1",
                thread_id="thr-mailbox-memory-1",
                business_result={
                    "recommended_next_action": "review_required",
                    "recommended_action_reason": "Need operator confirmation before offer draft.",
                },
                reply_result={"draft_enabled": False, "drafts": []},
                action_plan_result={
                    "primary_action": "prepare_reply",
                    "why_this_action": "Need a controlled follow-up with the customer.",
                },
                case_intelligence_result={
                    "review_routing": {"review_mode": "review_before_merge"},
                    "next_best_action": {
                        "primary_next_action": {
                            "action_type": "prepare_offer_draft",
                            "reason_pl": "Dane techniczne sa prawie kompletne, ale konflikt wymagaja sprawdzenia.",
                        }
                    },
                },
            )

            self.assertEqual(final_result.snapshot["recommended_next_action"], "prepare_offer_draft")
            self.assertEqual(final_result.snapshot["status"], "awaiting_review")
            self.assertEqual(final_result.next_action["source_stage"], "case_intelligence")
            event_types = {event["event_type"] for event in runtime.store.fetch_events_for_case(ingest_result.case_id, limit=20)}
            self.assertIn("message_received", event_types)
            self.assertIn("attachment_parsed", event_types)
            self.assertIn("case_snapshot_updated", event_types)
            self.assertIn("next_action_updated", event_types)

    def test_refresh_document_intelligence_emits_completion_event(self) -> None:
        snapshot = {
            "mailbox": "biuro@example.com",
            "source_message": {
                "message_id": "msg-refresh-1",
                "thread_id": "thr-refresh-1",
                "date": "2026-04-12T09:00:00+02:00",
                "sender": "Jan <jan@example.com>",
                "from": "Jan <jan@example.com>",
                "to": ["biuro@example.com"],
                "subject": "Prosba o kontakt",
                "snippet": "Dzien dobry",
                "body": "Krotki tekst bez zalacznikow.",
                "labels": ["INBOX"],
                "has_attachments": False,
            },
            "context_messages": [],
        }
        intake_result = {"case_assessment": {"case_family": "unknown"}, "decision": {"action": "review"}}
        case_link_result = {"decision": "create_new", "selected_case_key": "CASE-REFRESH-1"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime = MailboxMemoryRuntime(
                store=InMemoryMailboxMemoryStore(),
                blob_root=Path(tmp_dir) / "blobs",
                stage_mode="shadow",
            )
            runtime.bootstrap()
            ingest_result = runtime.ingest_message(
                snapshot=snapshot,
                intake_result=intake_result,
                case_link_result=case_link_result,
                refresh_document_intelligence=True,
            )
            self.assertTrue(ingest_result.enabled)
            event_types = {event["event_type"] for event in runtime.store.fetch_events_for_case(ingest_result.case_id, limit=30)}
            self.assertIn("document_intelligence_refresh_completed", event_types)

    def test_bounded_refresh_materializes_drive_chunks_when_text_only(self) -> None:
        """Drive rows can exist with text but no chunks (projection path); refresh should chunk for the case."""
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        case_id = "case_drive_materialize_1"
        ts = "2026-04-20T10:00:00+02:00"
        store.upsert_case(
            {
                "case_id": case_id,
                "case_key": "CASE-MAT-1",
                "thread_id": "thr_mat_1",
                "case_family": "test",
                "subject": "Materialize drive chunks",
                "customer_name": "",
                "customer_email": "",
                "status": "open",
                "metadata": {},
                "created_at": ts,
                "updated_at": ts,
            }
        )
        store.upsert_drive_document(
            {
                "document_id": "drv_mat_1",
                "drive_item_id": "drv_mat_1",
                "parent_drive_item_id": "",
                "parent_document_id": "",
                "case_id": case_id,
                "probable_case_key": "CASE-MAT-1",
                "file_name": "warranty.pdf",
                "mime_type": "application/pdf",
                "folder_path": "Serwis",
                "lane": "service_warranty",
                "document_kind": "warranty_card",
                "scope": "case_specific",
                "source_ref": "https://drive.example/drv_mat_1",
                "extraction_status": "extracted",
                "linkage_status": "deterministic",
                "classification_confidence": 0.9,
                "extraction_confidence": 0.85,
                "link_confidence": 0.97,
                "download_mime_type": "application/pdf",
                "content_sha256": "sha_mat",
                "blob_path": "",
                "text_content": "Paragraph one about Panasonic heat pump service.\n\nParagraph two warranty terms.",
                "summary_text": "Warranty",
                "metadata": {},
                "created_at": ts,
                "updated_at": ts,
            }
        )
        self.assertEqual(store.fetch_drive_chunks_for_case(case_id, limit=50), [])

        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime = MailboxMemoryRuntime(
                store=store,
                blob_root=Path(tmp_dir) / "blobs",
                stage_mode="shadow",
            )
            stats = runtime.bounded_refresh_document_intelligence_for_case(case_id=case_id, occurred_at=ts)

        self.assertGreaterEqual(stats.get("drive_chunks_materialized", 0), 1)
        chunks = store.fetch_drive_chunks_for_case(case_id, limit=50)
        self.assertTrue(chunks)
        self.assertTrue(all(str(c.get("case_id") or "") == case_id for c in chunks))
        self.assertEqual({c.get("document_id") for c in chunks}, {"drv_mat_1"})
        self.assertTrue(all(str(c.get("embedding_status") or "") in {"missing", "ready", "provider_unavailable", "provider_unconfigured"} for c in chunks))

    def test_in_memory_upsert_drive_document_propagates_case_id_to_chunks(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        case_a = "case_prop_a"
        case_b = "case_prop_b"
        ts = "2026-04-20T11:00:00+02:00"
        for cid, key in ((case_a, "KEY-A"), (case_b, "KEY-B")):
            store.upsert_case(
                {
                    "case_id": cid,
                    "case_key": key,
                    "thread_id": f"thr_{cid}",
                    "case_family": "test",
                    "subject": "propagate",
                    "customer_name": "",
                    "customer_email": "",
                    "status": "open",
                    "metadata": {},
                    "created_at": ts,
                    "updated_at": ts,
                }
            )
        store.replace_drive_document_chunks(
            document_id="drv_prop_1",
            rows=[
                {
                    "chunk_id": "chunk_prop_1",
                    "document_id": "drv_prop_1",
                    "case_id": case_a,
                    "ordinal": 0,
                    "chunk_text": "linked chunk",
                    "token_estimate": 2,
                    "embedding_model": "",
                    "embedding_status": "missing",
                    "metadata": {},
                    "created_at": ts,
                    "updated_at": ts,
                }
            ],
        )
        store.upsert_drive_document(
            {
                "document_id": "drv_prop_1",
                "drive_item_id": "drv_prop_1",
                "parent_drive_item_id": "",
                "parent_document_id": "",
                "case_id": case_b,
                "probable_case_key": "KEY-B",
                "file_name": "moved.pdf",
                "mime_type": "application/pdf",
                "folder_path": "/",
                "lane": "service_warranty",
                "document_kind": "warranty_card",
                "scope": "case_specific",
                "source_ref": "ref",
                "extraction_status": "extracted",
                "linkage_status": "deterministic",
                "classification_confidence": 0.9,
                "extraction_confidence": 0.8,
                "link_confidence": 0.9,
                "download_mime_type": "application/pdf",
                "content_sha256": "",
                "blob_path": "",
                "text_content": "x",
                "summary_text": "s",
                "metadata": {},
                "created_at": ts,
                "updated_at": ts,
            }
        )
        stored = store.drive_chunks["drv_prop_1"][0]
        self.assertEqual(stored.get("case_id"), case_b)

    def test_in_memory_drive_semantic_candidates_use_document_case_id_when_chunk_case_id_legacy_empty(self) -> None:
        """Drive chunks with empty chunk.case_id must still be visible when parent document.case_id matches (Test C)."""
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        case_id = "case_drive_semantic_doc_join_1"
        ts = "2026-04-22T12:00:00+02:00"
        store.upsert_case(
            {
                "case_id": case_id,
                "case_key": "KEY-SEM-1",
                "thread_id": "thr_sem_1",
                "case_family": "test",
                "subject": "semantic join",
                "customer_name": "",
                "customer_email": "",
                "status": "open",
                "metadata": {},
                "created_at": ts,
                "updated_at": ts,
            }
        )
        store.upsert_drive_document(
            {
                "document_id": "drv_sem_join_1",
                "drive_item_id": "drv_sem_join_1",
                "parent_drive_item_id": "",
                "parent_document_id": "",
                "case_id": case_id,
                "probable_case_key": "KEY-SEM-1",
                "file_name": "legacy.pdf",
                "mime_type": "application/pdf",
                "folder_path": "/",
                "lane": "service_warranty",
                "document_kind": "warranty_card",
                "scope": "case_specific",
                "source_ref": "ref",
                "extraction_status": "extracted",
                "linkage_status": "deterministic",
                "classification_confidence": 0.9,
                "extraction_confidence": 0.8,
                "link_confidence": 0.9,
                "download_mime_type": "application/pdf",
                "content_sha256": "",
                "blob_path": "",
                "text_content": "body",
                "summary_text": "s",
                "metadata": {},
                "created_at": ts,
                "updated_at": ts,
            }
        )
        emb = [1.0, 0.0, 0.0]
        store.replace_drive_document_chunks(
            document_id="drv_sem_join_1",
            rows=[
                {
                    "chunk_id": "chunk_sem_legacy_1",
                    "document_id": "drv_sem_join_1",
                    "case_id": "",
                    "ordinal": 0,
                    "chunk_text": "drive warranty text",
                    "token_estimate": 3,
                    "embedding_model": "test-model",
                    "embedding_status": "ready",
                    "embedding_updated_at": ts,
                    "embedding_error": "",
                    "embedding": list(emb),
                    "metadata": {"source_type": "drive_document_chunk"},
                    "created_at": ts,
                    "updated_at": ts,
                }
            ],
        )
        listed = store.fetch_drive_chunks_for_case(case_id, limit=50)
        self.assertEqual(len(listed), 1)
        self.assertEqual(str(listed[0].get("case_id") or ""), "")

        q_lit = "[1.0,0.0,0.0]"
        candidates = store.fetch_semantic_chunk_candidates_for_case(case_id, q_lit, limit_mailbox=5, limit_drive=5)
        drive_hits = [c for c in candidates if str(c.get("document_id") or "") == "drv_sem_join_1"]
        self.assertEqual(len(drive_hits), 1)
        self.assertAlmostEqual(float(drive_hits[0].get("vector_similarity") or 0.0), 1.0, places=5)

    @unittest.skipUnless(POSTGRES_TEST_DATABASE_URL, "MAILBOX_MEMORY_TEST_DATABASE_URL is not set")
    def test_postgres_store_bootstrap_and_message_lookup_round_trip(self) -> None:
        store = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
        store.bootstrap()

        unique = uuid.uuid4().hex[:10]
        case_id = f"case_pg_{unique}"
        message_id = f"msg_pg_{unique}"
        timestamp = "2026-04-12T10:00:00+02:00"

        store.upsert_case(
            {
                "case_id": case_id,
                "case_key": f"CASE-PG-{unique}",
                "thread_id": f"thr_pg_{unique}",
                "case_family": "mailbox_memory_test",
                "mailbox": "test@example.com",
                "subject": "Postgres bootstrap check",
                "status": "open",
                "customer_name": "Test Operator",
                "customer_email": "test.operator@example.com",
                "metadata": {"purpose": "bootstrap_round_trip"},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        store.upsert_message(
            {
                "message_id": message_id,
                "case_id": case_id,
                "thread_id": f"thr_pg_{unique}",
                "mailbox": "test@example.com",
                "sender": "Test Operator <test.operator@example.com>",
                "sender_email": "test.operator@example.com",
                "recipients": ["ops@example.com"],
                "subject": "Postgres bootstrap check",
                "snippet": "Smoke test",
                "body_text": "Smoke test for mailbox memory Postgres store.",
                "labels": ["INBOX"],
                "received_at": timestamp,
                "raw_snapshot": {"smoke": True},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )

        fetched_case = store.fetch_case_by_message_id(message_id)
        self.assertIsNotNone(fetched_case)
        self.assertEqual(fetched_case["case_id"], case_id)
        self.assertEqual(store.fetch_case(case_id)["customer_email"], "test.operator@example.com")

    @unittest.skipUnless(POSTGRES_TEST_DATABASE_URL, "MAILBOX_MEMORY_TEST_DATABASE_URL is not set")
    def test_postgres_replace_paths_are_serially_idempotent_and_concurrency_safe(self) -> None:
        store = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
        store.bootstrap()

        unique = uuid.uuid4().hex[:10]
        case_id = f"case_pg_replace_{unique}"
        message_id = f"msg_pg_replace_{unique}"
        document_id = f"doc_pg_replace_{unique}"
        timestamp = "2026-04-21T10:00:00+02:00"

        store.upsert_case(
            {
                "case_id": case_id,
                "case_key": f"CASE-PG-REPLACE-{unique}",
                "thread_id": f"thr_pg_replace_{unique}",
                "case_family": "mailbox_memory_test",
                "mailbox": "test@example.com",
                "subject": "Postgres replace-path check",
                "status": "open",
                "customer_name": "Test Operator",
                "customer_email": "test.operator@example.com",
                "metadata": {"purpose": "replace_paths"},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        store.upsert_message(
            {
                "message_id": message_id,
                "case_id": case_id,
                "thread_id": f"thr_pg_replace_{unique}",
                "mailbox": "test@example.com",
                "sender": "Test Operator <test.operator@example.com>",
                "sender_email": "test.operator@example.com",
                "recipients": ["ops@example.com"],
                "subject": "Postgres replace-path check",
                "snippet": "Replace-path smoke test",
                "body_text": "Smoke test for mailbox memory replace-path concurrency.",
                "labels": ["INBOX"],
                "received_at": timestamp,
                "raw_snapshot": {"smoke": True},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        store.upsert_document(
            {
                "document_id": document_id,
                "case_id": case_id,
                "message_id": message_id,
                "attachment_id": "",
                "parent_document_id": "",
                "file_name": "replace-check.txt",
                "mime_type": "text/plain",
                "source_type": "attachment",
                "document_kind": "generic",
                "extraction_status": "parsed",
                "parser_name": "test",
                "content_sha256": "",
                "blob_path": "",
                "text_content": "replace path",
                "summary_text": "",
                "metadata": {"purpose": "replace_paths"},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )

        chunk_rows = [
            {
                "chunk_id": f"chunk_pg_replace_{unique}_1",
                "document_id": document_id,
                "case_id": case_id,
                "ordinal": 0,
                "chunk_text": "Chunk one",
                "token_estimate": 8,
                "embedding_model": "",
                "embedding_status": "missing",
                "embedding_updated_at": None,
                "embedding_error": "",
                "metadata": {"part": 1},
                "created_at": timestamp,
            },
            {
                "chunk_id": f"chunk_pg_replace_{unique}_2",
                "document_id": document_id,
                "case_id": case_id,
                "ordinal": 1,
                "chunk_text": "Chunk two",
                "token_estimate": 8,
                "embedding_model": "",
                "embedding_status": "missing",
                "embedding_updated_at": None,
                "embedding_error": "",
                "metadata": {"part": 2},
                "created_at": timestamp,
            },
        ]
        fact_rows = [
            {
                "fact_id": f"fact_pg_replace_{unique}_1",
                "case_id": case_id,
                "message_id": message_id,
                "document_id": document_id,
                "entity_scope": "case",
                "fact_key": "city",
                "normalized_value": "jaworzno",
                "raw_value": "Jaworzno",
                "confidence": 0.9,
                "observed_at": timestamp,
                "source_type": "message",
                "source_ref": message_id,
                "status": "active",
                "metadata": {"part": 1},
            },
            {
                "fact_id": f"fact_pg_replace_{unique}_2",
                "case_id": case_id,
                "message_id": message_id,
                "document_id": document_id,
                "entity_scope": "case",
                "fact_key": "product",
                "normalized_value": "panasonic",
                "raw_value": "Panasonic",
                "confidence": 0.85,
                "observed_at": timestamp,
                "source_type": "message",
                "source_ref": message_id,
                "status": "active",
                "metadata": {"part": 2},
            },
        ]

        store.replace_document_chunks(document_id, chunk_rows)
        store.replace_message_facts(message_id=message_id, rows=fact_rows)
        store.replace_document_chunks(document_id, chunk_rows)
        store.replace_message_facts(message_id=message_id, rows=fact_rows)

        serial_chunks = [
            row for row in store.fetch_chunks_for_case(case_id, limit=20) if row["document_id"] == document_id
        ]
        serial_facts = [
            row for row in store.fetch_facts_for_case(case_id) if row["message_id"] == message_id
        ]
        self.assertEqual({row["chunk_id"] for row in serial_chunks}, {row["chunk_id"] for row in chunk_rows})
        self.assertEqual({row["fact_id"] for row in serial_facts}, {row["fact_id"] for row in fact_rows})

        barrier = threading.Barrier(3)
        errors: list[str] = []

        def replace_rows() -> None:
            try:
                barrier.wait(timeout=10)
                store.replace_document_chunks(document_id, chunk_rows)
                store.replace_message_facts(message_id=message_id, rows=fact_rows)
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)

        threads = [threading.Thread(target=replace_rows) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])

        final_chunks = [
            row for row in store.fetch_chunks_for_case(case_id, limit=20) if row["document_id"] == document_id
        ]
        final_facts = [
            row for row in store.fetch_facts_for_case(case_id) if row["message_id"] == message_id
        ]
        self.assertEqual({row["chunk_id"] for row in final_chunks}, {row["chunk_id"] for row in chunk_rows})
        self.assertEqual({row["fact_id"] for row in final_facts}, {row["fact_id"] for row in fact_rows})

    @unittest.skipUnless(POSTGRES_TEST_DATABASE_URL, "MAILBOX_MEMORY_TEST_DATABASE_URL is not set")
    def test_postgres_mutate_case_serializes_two_connections_without_lost_update(self) -> None:
        store = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
        store.bootstrap()

        unique = uuid.uuid4().hex[:10]
        case_id = f"case_pg_mutate_{unique}"
        timestamp = "2026-07-13T10:00:00+02:00"
        store.upsert_case(
            {
                "case_id": case_id,
                "case_key": f"CASE-PG-MUTATE-{unique}",
                "thread_id": f"thr_pg_mutate_{unique}",
                "case_family": "mailbox_memory_test",
                "mailbox": "test@example.com",
                "subject": "Postgres mutate concurrency check",
                "status": "open",
                "customer_name": "Test Operator",
                "customer_email": "test.operator@example.com",
                "metadata": {"purpose": "mutate_case_concurrency"},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )

        store_a = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
        store_b = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
        tx_a_loaded = threading.Event()
        allow_tx_a_commit = threading.Event()
        tx_b_entered_mutator = threading.Event()
        tx_b_seen: dict[str, object] = {}
        thread_errors: list[BaseException] = []

        def mutate_a(row: dict[str, object]) -> dict[str, object]:
            updated = dict(row)
            tx_a_loaded.set()
            if not allow_tx_a_commit.wait(timeout=5):
                raise AssertionError("timed out waiting to resume transaction A")
            updated["latest_signal_id"] = "sig-pg-a"
            updated["updated_at"] = "2026-07-13T10:00:01+02:00"
            return updated

        def mutate_b(row: dict[str, object]) -> dict[str, object]:
            tx_b_seen["latest_signal_id"] = row.get("latest_signal_id")
            updated = dict(row)
            metadata = dict(updated.get("metadata") or {})
            metadata["operator_flag"] = True
            updated["metadata"] = metadata
            updated["updated_at"] = "2026-07-13T10:00:02+02:00"
            tx_b_entered_mutator.set()
            return updated

        def run_mutation(target_store: PostgresMailboxMemoryStore, mutator) -> None:
            try:
                target_store.mutate_case(case_id, mutator)
            except BaseException as exc:  # pragma: no cover
                thread_errors.append(exc)

        thread_a = threading.Thread(target=run_mutation, args=(store_a, mutate_a), daemon=True)
        thread_b = threading.Thread(target=run_mutation, args=(store_b, mutate_b), daemon=True)

        try:
            thread_a.start()
            self.assertTrue(tx_a_loaded.wait(timeout=5), "transaction A did not reach mutator")

            thread_b.start()
            self.assertFalse(
                tx_b_entered_mutator.wait(timeout=0.5),
                "transaction B entered mutator before transaction A committed",
            )

            allow_tx_a_commit.set()
            thread_a.join(timeout=10)
            thread_b.join(timeout=10)

            self.assertFalse(thread_a.is_alive(), "transaction A did not finish")
            self.assertFalse(thread_b.is_alive(), "transaction B did not finish")
            self.assertEqual(thread_errors, [])
            self.assertTrue(tx_b_entered_mutator.is_set(), "transaction B never entered mutator")
            self.assertEqual(tx_b_seen["latest_signal_id"], "sig-pg-a")

            final_row = store.fetch_case(case_id)
            self.assertIsNotNone(final_row)
            self.assertEqual(final_row["latest_signal_id"], "sig-pg-a")
            self.assertTrue((final_row.get("metadata") or {}).get("operator_flag"))
        finally:
            allow_tx_a_commit.set()
            thread_a.join(timeout=1)
            thread_b.join(timeout=1)
            with store._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM mailbox_memory_cases WHERE case_id = %(case_id)s", {"case_id": case_id})
                conn.commit()

    @unittest.skipUnless(POSTGRES_TEST_DATABASE_URL, "MAILBOX_MEMORY_TEST_DATABASE_URL is not set")
    def test_postgres_mutate_case_rolls_back_failed_mutator_and_releases_lock(self) -> None:
        store = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
        store.bootstrap()

        unique = uuid.uuid4().hex[:10]
        case_id = f"case_pg_mutate_rollback_{unique}"
        timestamp = "2026-07-13T10:10:00+02:00"
        store.upsert_case(
            {
                "case_id": case_id,
                "case_key": f"CASE-PG-MUTATE-ROLLBACK-{unique}",
                "thread_id": f"thr_pg_mutate_rollback_{unique}",
                "case_family": "mailbox_memory_test",
                "mailbox": "test@example.com",
                "subject": "Postgres mutate rollback check",
                "status": "open",
                "customer_name": "Test Operator",
                "customer_email": "test.operator@example.com",
                "metadata": {"purpose": "mutate_case_rollback"},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )

        try:
            def fail_mutator(row: dict[str, object]) -> dict[str, object]:
                updated = dict(row)
                metadata = dict(updated.get("metadata") or {})
                metadata["should_not_persist"] = True
                updated["metadata"] = metadata
                raise RuntimeError("forced mutate rollback")

            with self.assertRaisesRegex(RuntimeError, "forced mutate rollback"):
                store.mutate_case(case_id, fail_mutator)

            after_failure = store.fetch_case(case_id)
            self.assertIsNotNone(after_failure)
            self.assertNotIn("should_not_persist", after_failure.get("metadata") or {})

            release_started = time.monotonic()
            recovered = store.mutate_case(
                case_id,
                lambda row: {
                    **dict(row),
                    "metadata": {**dict(row.get("metadata") or {}), "recovered_after_rollback": True},
                    "updated_at": "2026-07-13T10:10:01+02:00",
                },
            )
            self.assertLess(time.monotonic() - release_started, 5.0)
            self.assertTrue((recovered.get("metadata") or {}).get("recovered_after_rollback"))
        finally:
            with store._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM mailbox_memory_cases WHERE case_id = %(case_id)s", {"case_id": case_id})
                conn.commit()

    @unittest.skipUnless(POSTGRES_TEST_DATABASE_URL, "MAILBOX_MEMORY_TEST_DATABASE_URL is not set")
    def test_postgres_mutate_case_create_if_missing_serializes_two_connections_without_lost_update(self) -> None:
        """CONC-01 real-Postgres proof: two connections materializing the SAME
        brand-new case_id via mutate_case(..., create_if_missing=True) must
        serialize on the advisory lock (even though no row exists yet), and
        the second connection must observe the first connection's committed
        row and merge onto it -- no lost update, no duplicate row.
        """
        store = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
        store.bootstrap()

        unique = uuid.uuid4().hex[:10]
        case_id = f"case_pg_new_case_conc01_{unique}"
        # Deliberately do NOT pre-create the row: the row must not exist at
        # the moment both connections attempt to materialize it.
        self.assertIsNone(store.fetch_case(case_id))

        store_a = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
        store_b = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
        tx_a_loaded = threading.Event()
        allow_tx_a_commit = threading.Event()
        tx_b_entered_mutator = threading.Event()
        tx_b_seen: dict[str, object] = {}
        thread_errors: list[BaseException] = []

        def mutate_a(row: dict[str, object]) -> dict[str, object]:
            # mutate_case must hand connection A an explicit "no record yet"
            # skeleton (case_id + empty metadata), not None and not a raw
            # empty dict, so the mutator can build a normalized row.
            self.assertEqual(row.get("case_id"), case_id)
            self.assertEqual(row.get("metadata"), {})
            self.assertNotIn("last_source_kinds_seen", row)
            tx_a_loaded.set()
            if not allow_tx_a_commit.wait(timeout=5):
                raise AssertionError("timed out waiting to resume transaction A")
            updated = dict(row)
            updated["case_key"] = f"CASE-PG-NEW-{unique}"
            updated["case_family"] = "mailbox_memory_test"
            updated["mailbox"] = "test@example.com"
            updated["subject"] = "Postgres new-case concurrency check"
            updated["status"] = "open"
            updated["latest_signal_id"] = "sig-pg-new-a"
            updated["last_source_kinds_seen"] = ["gmail"]
            updated["updated_at"] = "2026-07-14T10:00:01+02:00"
            return updated

        def mutate_b(row: dict[str, object]) -> dict[str, object]:
            # By the time B's mutator runs, it must see A's already-committed
            # row (read under the same lock after waiting for it), not the
            # missing-row skeleton again.
            tx_b_seen["case_key"] = row.get("case_key")
            tx_b_seen["latest_signal_id"] = row.get("latest_signal_id")
            tx_b_seen["last_source_kinds_seen"] = list(row.get("last_source_kinds_seen") or [])
            updated = dict(row)
            source_kinds = list(row.get("last_source_kinds_seen") or [])
            if "drive" not in source_kinds:
                source_kinds.append("drive")
            updated["last_source_kinds_seen"] = source_kinds
            metadata = dict(updated.get("metadata") or {})
            metadata["drive_seed_applied"] = True
            updated["metadata"] = metadata
            updated["updated_at"] = "2026-07-14T10:00:02+02:00"
            tx_b_entered_mutator.set()
            return updated

        def run_mutation(target_store: PostgresMailboxMemoryStore, mutator) -> None:
            try:
                target_store.mutate_case(case_id, mutator, create_if_missing=True)
            except BaseException as exc:  # pragma: no cover
                thread_errors.append(exc)

        thread_a = threading.Thread(target=run_mutation, args=(store_a, mutate_a), daemon=True)
        thread_b = threading.Thread(target=run_mutation, args=(store_b, mutate_b), daemon=True)

        try:
            thread_a.start()
            self.assertTrue(tx_a_loaded.wait(timeout=5), "transaction A did not reach mutator")

            thread_b.start()
            self.assertFalse(
                tx_b_entered_mutator.wait(timeout=0.5),
                "transaction B entered mutator before transaction A committed -- "
                "advisory lock did not serialize creation of a non-existent row",
            )

            allow_tx_a_commit.set()
            thread_a.join(timeout=10)
            thread_b.join(timeout=10)

            self.assertFalse(thread_a.is_alive(), "transaction A did not finish (possible deadlock)")
            self.assertFalse(thread_b.is_alive(), "transaction B did not finish (possible deadlock)")
            self.assertEqual(thread_errors, [])
            self.assertTrue(tx_b_entered_mutator.is_set(), "transaction B never entered mutator")
            self.assertEqual(tx_b_seen["latest_signal_id"], "sig-pg-new-a")
            self.assertEqual(tx_b_seen["last_source_kinds_seen"], ["gmail"])

            final_row = store.fetch_case(case_id)
            self.assertIsNotNone(final_row)
            self.assertEqual(final_row["case_id"], case_id)
            self.assertEqual(final_row["latest_signal_id"], "sig-pg-new-a")
            self.assertEqual(set(final_row.get("last_source_kinds_seen") or []), {"gmail", "drive"})
            self.assertTrue((final_row.get("metadata") or {}).get("drive_seed_applied"))

            with store._connect(row_factory=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM mailbox_memory_cases WHERE case_id = %(case_id)s",
                        {"case_id": case_id},
                    )
                    count_row = cur.fetchone()
            self.assertEqual(int(count_row["n"]), 1, "expected exactly one case row, found a duplicate")
        finally:
            allow_tx_a_commit.set()
            thread_a.join(timeout=1)
            thread_b.join(timeout=1)
            with store._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM mailbox_memory_cases WHERE case_id = %(case_id)s", {"case_id": case_id})
                conn.commit()
            after_cleanup = store.fetch_case(case_id)
            self.assertIsNone(after_cleanup)

    @unittest.skipUnless(POSTGRES_TEST_DATABASE_URL, "MAILBOX_MEMORY_TEST_DATABASE_URL is not set")
    def test_postgres_stamp_case_runtime_state_new_case_serializes_two_connections_without_lost_update(self) -> None:
        """CONC-01 real-Postgres proof against the actual fixed production
        function (not just the underlying mutate_case primitive): two real
        connections running signal_reconciler._stamp_case_runtime_state for
        two different signals (gmail, drive) that both materialize the SAME
        brand-new case_id must serialize on mutate_case's advisory lock and
        both independent contributions must survive in the final row.
        """
        from signal_reconciler import CanonicalSignal, ProjectionRefreshDecision, _stamp_case_runtime_state

        store = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
        store.bootstrap()

        unique = uuid.uuid4().hex[:10]
        case_id = f"case_pg_stamp_new_{unique}"
        self.assertIsNone(store.fetch_case(case_id))

        store_a = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
        store_b = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)

        tx_a_entered_mutator = threading.Event()
        allow_tx_a_commit = threading.Event()
        tx_b_entered_mutator = threading.Event()
        tx_b_seen: dict[str, object] = {}
        thread_errors: list[BaseException] = []

        real_mutate_case = PostgresMailboxMemoryStore.mutate_case

        def paused_mutate_case(self_store, cid, mutator, *, create_if_missing=False):
            def wrapped_mutator(row):
                tx_a_entered_mutator.set()
                if not allow_tx_a_commit.wait(timeout=5):
                    raise AssertionError("timed out waiting to resume transaction A")
                return mutator(row)

            return real_mutate_case(self_store, cid, wrapped_mutator, create_if_missing=create_if_missing)

        def observing_mutate_case(self_store, cid, mutator, *, create_if_missing=False):
            def wrapped_mutator(row):
                # Runs only once B has acquired mutate_case's advisory lock,
                # i.e. after waiting for A -- this must reflect A's committed
                # row, not the pre-existing-row skeleton again.
                tx_b_seen["last_source_kinds_seen"] = list(row.get("last_source_kinds_seen") or [])
                tx_b_seen["latest_signal_id"] = row.get("latest_signal_id")
                result = mutator(row)
                tx_b_entered_mutator.set()
                return result

            return real_mutate_case(self_store, cid, wrapped_mutator, create_if_missing=create_if_missing)

        # Instance-level overrides: attribute lookup on store_a/store_b picks
        # these up ahead of the class method, while _stamp_case_runtime_state
        # keeps calling the real production mutate_case underneath via
        # real_mutate_case -- only the observation/pause hooks are test-only.
        store_a.mutate_case = paused_mutate_case.__get__(store_a, PostgresMailboxMemoryStore)
        store_b.mutate_case = observing_mutate_case.__get__(store_b, PostgresMailboxMemoryStore)

        gmail_signal = CanonicalSignal(
            signal_id="sig-pg-stamp-gmail",
            schema_version="v1",
            signal_kind="mail_received",
            source_kind="gmail",
            source_ref={},
            observed_at="2026-07-14T10:00:00+02:00",
            effective_at=None,
            case_key_hint=None,
            thread_key_hint=None,
            business_lane=None,
            signal_summary_pl="CONC-01 postgres proof (gmail)",
            payload={},
            artifacts={},
            processing_state="new",
            idempotency_key="idem-sig-pg-stamp-gmail",
            content_hash=None,
            replayable=True,
            created_by_runtime="test",
        )
        drive_signal = CanonicalSignal(
            signal_id="sig-pg-stamp-drive",
            schema_version="v1",
            signal_kind="drive_document_added",
            source_kind="drive",
            source_ref={},
            observed_at="2026-07-14T10:00:05+02:00",
            effective_at=None,
            case_key_hint=None,
            thread_key_hint=None,
            business_lane=None,
            signal_summary_pl="CONC-01 postgres proof (drive)",
            payload={},
            artifacts={},
            processing_state="new",
            idempotency_key="idem-sig-pg-stamp-drive",
            content_hash=None,
            replayable=True,
            created_by_runtime="test",
        )
        decision = ProjectionRefreshDecision(should_refresh=False, refresh_kind="none", reason="pg-proof")

        def run_a() -> None:
            try:
                _stamp_case_runtime_state(store_a, case_id=case_id, signal=gmail_signal, projection_decision=decision)
            except BaseException as exc:  # pragma: no cover
                thread_errors.append(exc)

        def run_b() -> None:
            try:
                _stamp_case_runtime_state(store_b, case_id=case_id, signal=drive_signal, projection_decision=decision)
            except BaseException as exc:  # pragma: no cover
                thread_errors.append(exc)

        thread_a = threading.Thread(target=run_a, daemon=True)
        thread_b = threading.Thread(target=run_b, daemon=True)
        thread_b_started = False

        try:
            thread_a.start()
            self.assertTrue(tx_a_entered_mutator.wait(timeout=5), "transaction A did not reach mutator")

            thread_b.start()
            thread_b_started = True
            self.assertFalse(
                tx_b_entered_mutator.wait(timeout=0.5),
                "transaction B entered mutator before transaction A committed -- "
                "advisory lock did not serialize creation of a non-existent row",
            )

            allow_tx_a_commit.set()
            thread_a.join(timeout=10)
            thread_b.join(timeout=10)

            self.assertFalse(thread_a.is_alive(), "transaction A did not finish (possible deadlock)")
            self.assertFalse(thread_b.is_alive(), "transaction B did not finish (possible deadlock)")
            self.assertEqual(thread_errors, [])
            self.assertTrue(tx_b_entered_mutator.is_set(), "transaction B never entered mutator")
            self.assertEqual(tx_b_seen["latest_signal_id"], "sig-pg-stamp-gmail")
            self.assertEqual(tx_b_seen["last_source_kinds_seen"], ["gmail"])

            final_row = store.fetch_case(case_id)
            self.assertIsNotNone(final_row)
            self.assertEqual(final_row["case_id"], case_id)
            self.assertEqual(set(final_row.get("last_source_kinds_seen") or []), {"gmail", "drive"})

            with store._connect(row_factory=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM mailbox_memory_cases WHERE case_id = %(case_id)s",
                        {"case_id": case_id},
                    )
                    count_row = cur.fetchone()
            self.assertEqual(int(count_row["n"]), 1, "expected exactly one case row, found a duplicate")
        finally:
            allow_tx_a_commit.set()
            thread_a.join(timeout=1)
            if thread_b_started:
                thread_b.join(timeout=1)
            with store._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM mailbox_memory_cases WHERE case_id = %(case_id)s", {"case_id": case_id})
                conn.commit()
            after_cleanup = store.fetch_case(case_id)
            self.assertIsNone(after_cleanup)

    def test_finalize_case_status_write_uses_mutate_case_not_fetch_then_upsert(self) -> None:
        """RC-05: finalize_case used to fetch_case() then upsert_case() unlocked for
        the status/lifecycle write — the exact TOCTOU window mutate_case exists to
        close. A concurrent writer's field (simulated here as latest_signal_id, the
        field _stamp_case_runtime_state writes) landing between fetch and finalize's
        write must survive finalize, not be silently stomped by a stale re-upsert.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = InMemoryMailboxMemoryStore()
            runtime = MailboxMemoryRuntime(
                store=store,
                blob_root=Path(tmp_dir) / "blobs",
                stage_mode="shadow",
            )
            runtime.bootstrap()

            case_id = "case_finalize_toctou"
            store.upsert_case(
                {
                    "case_id": case_id,
                    "case_key": "CASE-FINALIZE-TOCTOU",
                    "thread_id": "thr_finalize_toctou",
                    "case_family": "test",
                    "mailbox": "test@example.com",
                    "subject": "TOCTOU check",
                    "status": "open",
                    "customer_name": "",
                    "customer_email": "",
                    "metadata": {},
                }
            )

            upsert_case_calls: list[dict] = []
            real_upsert_case = type(store).upsert_case
            real_mutate_case = type(store).mutate_case

            def _spy_upsert_case(self_store, row: dict) -> None:
                upsert_case_calls.append(dict(row))
                real_upsert_case(self_store, row)

            # Simulate a concurrent writer (e.g. _stamp_case_runtime_state landing
            # from a signal reconciled in parallel) that finalize_case's mutator
            # must see and preserve, proving it reads the CURRENT row under the
            # lock rather than a value captured before its own call started.
            def _mutate_case_with_interleaved_write(self_store, cid, mutator, *, create_if_missing=False):
                def _wrapped(row: dict) -> dict:
                    row = dict(row)
                    row["latest_signal_id"] = "sig-concurrent-writer"
                    return mutator(row)

                return real_mutate_case(self_store, cid, _wrapped, create_if_missing=create_if_missing)

            with patch.object(type(store), "upsert_case", _spy_upsert_case), patch.object(
                type(store), "mutate_case", _mutate_case_with_interleaved_write
            ):
                runtime.finalize_case(
                    case_id=case_id,
                    message_id="msg-toctou",
                    thread_id="thr_finalize_toctou",
                    business_result={},
                    reply_result={},
                    action_plan_result={},
                    case_intelligence_result={},
                )

            # The old fetch_case()+upsert_case() pattern is gone from this path.
            self.assertEqual(upsert_case_calls, [])

            final_row = store.fetch_case(case_id)
            self.assertIsNotNone(final_row)
            # The concurrently-written field survived finalize's status update.
            self.assertEqual(final_row["latest_signal_id"], "sig-concurrent-writer")
            self.assertIn(final_row["status"], {"open", "awaiting_review", "closed", "resolved"})


if __name__ == "__main__":
    unittest.main()
