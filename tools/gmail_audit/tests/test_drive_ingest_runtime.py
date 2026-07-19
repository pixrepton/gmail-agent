from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from drive_client import DownloadedDriveContent, GOOGLE_DRIVE_FOLDER_MIME
from drive_ingest_runtime import DriveIngestRuntime
from graph_store import InMemoryGraphStore
from mailbox_memory_store import InMemoryMailboxMemoryStore


def _build_docx_bytes(paragraphs: list[str]) -> bytes:
    xml_body = "".join(f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>" for paragraph in paragraphs)
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


class _FakeDriveClient:
    def __init__(self, items: list[dict[str, object]], blobs: dict[str, bytes]) -> None:
        self._items = list(items)
        self._blobs = dict(blobs)

    def list_children(self, *, folder_id: str, page_token: str = "", page_size: int | None = None) -> dict[str, object]:
        if folder_id != "root":
            return {"items": [], "next_page_token": ""}
        return {"items": self._items, "next_page_token": ""}

    def build_source_ref(self, metadata: dict[str, object]) -> str:
        return str(metadata.get("webViewLink") or f"https://drive.google.com/file/d/{metadata.get('id')}")

    def describe_item(self, metadata: dict[str, object], *, folder_path: str = "") -> dict[str, object]:
        return {
            "drive_item_id": str(metadata.get("id") or ""),
            "title": str(metadata.get("name") or ""),
            "mime_type": str(metadata.get("mimeType") or ""),
            "parent_drive_item_id": str((metadata.get("parents") or [""])[0] or ""),
            "folder_path": folder_path,
            "source_ref": self.build_source_ref(metadata),
            "is_folder": str(metadata.get("mimeType") or "") == GOOGLE_DRIVE_FOLDER_MIME,
            "size_bytes": int(metadata.get("size") or 0),
            "modified_time": str(metadata.get("modifiedTime") or ""),
            "metadata": dict(metadata),
        }

    def download_content(self, metadata: dict[str, object], *, max_bytes: int) -> DownloadedDriveContent:
        item_id = str(metadata.get("id") or "")
        blob = self._blobs[item_id]
        return DownloadedDriveContent(
            data=blob,
            mime_type=str(metadata.get("mimeType") or ""),
            source_ref=self.build_source_ref(metadata),
            source_kind="binary_download",
        )


class DriveIngestRuntimeTests(unittest.TestCase):
    def test_ingest_batch_stores_drive_docs_handles_blocked_extraction_and_upserts_graph(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        store.upsert_case(
            {
                "case_id": "case_siedlec",
                "case_key": "siedlec_9kw_panasonic_adc0309k3e5",
                "thread_id": "",
                "case_family": "heat_pump_offer",
                "mailbox": "drive",
                "subject": "Siedlec Panasonic 9 kW",
                "status": "open",
                "customer_name": "Jan Kowalski",
                "customer_email": "jan@example.com",
                "metadata": {"installation_address": "Siedlec 12"},
                "created_at": "2026-04-12T08:00:00+02:00",
                "updated_at": "2026-04-12T08:00:00+02:00",
            }
        )
        store.upsert_snapshot(
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

        docx_bytes = _build_docx_bytes(
            [
                "Kupujacy: Jan Kowalski",
                "Adres montazu Siedlec 12",
                "Model Panasonic WH-ADC0309K3E5",
                "Gwarancja 5 lat",
            ]
        )
        items = [
            {
                "id": "drv_doc_1",
                "name": "umowa siedlec 9kw.docx",
                "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "parents": ["root"],
                "size": len(docx_bytes),
                "modifiedTime": "2026-04-12T09:00:00+02:00",
                "webViewLink": "https://drive.google.com/file/d/drv_doc_1",
            },
            {
                "id": "drv_doc_2",
                "name": "umowa-pc-plaza.pdf",
                "mimeType": "application/octet-stream",
                "parents": ["root"],
                "size": 42,
                "modifiedTime": "2026-04-12T09:05:00+02:00",
                "webViewLink": "https://drive.google.com/file/d/drv_doc_2",
            },
        ]
        client = _FakeDriveClient(items=items, blobs={"drv_doc_1": docx_bytes, "drv_doc_2": b"opaque-binary"})
        settings = SimpleNamespace(
            google_drive_root_folder_id="root",
            google_drive_batch_page_size=10,
            google_drive_max_download_bytes=1_000_000,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime = DriveIngestRuntime(
                settings=settings,
                store=store,
                blob_root=Path(tmp_dir) / "gdrive",
                client=client,
                graph_store=InMemoryGraphStore(),
            )
            runtime.bootstrap()
            result = runtime.ingest_batch(limit=2, root_folder_id="root", run_id="drive_test_run")

        self.assertEqual(result.processed_count, 2)
        self.assertEqual(result.stored_document_count, 2)

        documents = store.fetch_drive_documents(limit=10)
        by_name = {row["file_name"]: row for row in documents}
        self.assertEqual(by_name["umowa siedlec 9kw.docx"]["lane"], "formal_contracts")
        self.assertEqual(by_name["umowa siedlec 9kw.docx"]["document_kind"], "contract")
        self.assertEqual(by_name["umowa siedlec 9kw.docx"]["scope"], "case_specific")
        self.assertEqual(by_name["umowa siedlec 9kw.docx"]["case_id"], "case_siedlec")
        self.assertEqual(by_name["umowa siedlec 9kw.docx"]["extraction_status"], "extracted")
        self.assertEqual(by_name["umowa-pc-plaza.pdf"]["extraction_status"], "blocked")

        hints = runtime.graph_store.fetch_case_hints("case_siedlec", limit=20)
        self.assertTrue(any(item["relation_type"] == "case_has_document" for item in hints))
        self.assertTrue(any(event["event_type"] == "drive_document_ingested" for event in result.events))

        drive_facts = store.fetch_drive_facts_for_case("case_siedlec")
        self.assertTrue(drive_facts, "extracted Drive rows must persist document-scoped fact harvest")

    def test_upsert_drive_case_seed_preserves_existing_customer_identity(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        store.upsert_case(
            {
                "case_id": "case_existing",
                "case_key": "thread:msg_existing",
                "thread_id": "thr_existing",
                "case_family": "service",
                "mailbox": "gmail",
                "subject": "Twoje wnioski o uruchomienie",
                "status": "open",
                "customer_name": "PanasonicProClub",
                "customer_email": "no-reply@panasonicproclub.com",
                "metadata": {"source": "gmail"},
                "created_at": "2026-04-12T08:00:00+02:00",
                "updated_at": "2026-04-12T08:00:00+02:00",
            }
        )
        runtime = DriveIngestRuntime(
            settings=SimpleNamespace(
                google_drive_root_folder_id="root",
                google_drive_batch_page_size=10,
                google_drive_max_download_bytes=1_000_000,
            ),
            store=store,
            blob_root=Path(tempfile.gettempdir()) / "gdrive-test",
            client=_FakeDriveClient(items=[], blobs={}),
            graph_store=InMemoryGraphStore(),
        )

        runtime._upsert_drive_case_seed(
            case_id="case_existing",
            case_key="thread:msg_existing",
            candidate=SimpleNamespace(title="gwarancja-siedlec.pdf"),
            facts=[
                {"fact_key": "customer_name", "normalized_value": "samej spr"},
                {"fact_key": "customer_email", "normalized_value": "info.pl.hvac@eu.panasonic.com"},
                {"fact_key": "installation_address", "normalized_value": "Siedlec 229"},
                {"fact_key": "device_model_bundle", "normalized_value": "WH-ADC0309K3E5 | WH-UDZ09KE5"},
            ],
            observed_at="2026-04-12T08:05:00+02:00",
        )

        case_row = store.fetch_case("case_existing")
        self.assertEqual(case_row["customer_name"], "PanasonicProClub")
        self.assertEqual(case_row["customer_email"], "no-reply@panasonicproclub.com")
        self.assertEqual(case_row["metadata"]["installation_address"], "Siedlec 229")
        self.assertEqual(case_row["metadata"]["model_bundle"], "WH-ADC0309K3E5 | WH-UDZ09KE5")

    def test_media_asset_inherits_case_from_parent_folder_anchor(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        store.upsert_case(
            {
                "case_id": "case_media_anchor",
                "case_key": "siedlec_9kw_panasonic_adc0309k3e5",
                "thread_id": "",
                "case_family": "heat_pump_offer",
                "mailbox": "drive",
                "subject": "Siedlec Panasonic 9 kW",
                "status": "open",
                "customer_name": "Jan Kowalski",
                "customer_email": "jan@example.com",
                "metadata": {"installation_address": "Siedlec 12"},
                "created_at": "2026-04-12T08:00:00+02:00",
                "updated_at": "2026-04-12T08:00:00+02:00",
            }
        )
        store.upsert_snapshot(
            "case_media_anchor",
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

        docx_bytes = _build_docx_bytes(
            [
                "Kupujacy: Jan Kowalski",
                "Adres montazu Siedlec 12",
                "Model Panasonic WH-ADC0309K3E5",
                "Gwarancja 5 lat",
            ]
        )
        items = [
            {
                "id": "drv_contract_anchor",
                "name": "umowa siedlec 9kw.docx",
                "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "parents": ["case-folder-1"],
                "size": len(docx_bytes),
                "modifiedTime": "2026-04-12T09:00:00+02:00",
                "webViewLink": "https://drive.google.com/file/d/drv_contract_anchor",
            },
            {
                "id": "drv_media_anchor",
                "name": "IMG_0001.jpg",
                "mimeType": "image/jpeg",
                "parents": ["case-folder-1"],
                "size": 128,
                "modifiedTime": "2026-04-12T09:05:00+02:00",
                "webViewLink": "https://drive.google.com/file/d/drv_media_anchor",
            },
        ]
        client = _FakeDriveClient(
            items=items,
            blobs={
                "drv_contract_anchor": docx_bytes,
                "drv_media_anchor": b"\xff\xd8\xff\xd9",
            },
        )
        settings = SimpleNamespace(
            google_drive_root_folder_id="root",
            google_drive_batch_page_size=10,
            google_drive_max_download_bytes=1_000_000,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime = DriveIngestRuntime(
                settings=settings,
                store=store,
                blob_root=Path(tmp_dir) / "gdrive",
                client=client,
                graph_store=InMemoryGraphStore(),
            )
            runtime.bootstrap()
            runtime.ingest_batch(limit=2, root_folder_id="root", run_id="drive_media_anchor_run")

        documents = store.fetch_drive_documents(limit=10)
        by_name = {row["file_name"]: row for row in documents}
        self.assertEqual(by_name["IMG_0001.jpg"]["case_id"], "case_media_anchor")
        self.assertEqual(by_name["IMG_0001.jpg"]["probable_case_key"], "siedlec_9kw_panasonic_adc0309k3e5")
        self.assertEqual(by_name["IMG_0001.jpg"]["linkage_status"], "deterministic")

        hints = runtime.graph_store.fetch_case_hints("case_media_anchor", limit=20)
        self.assertTrue(any(item["relation_type"] == "case_has_media_bundle" for item in hints))


if __name__ == "__main__":
    unittest.main()
