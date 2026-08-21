"""P0.5 provenance residual: attachment/RAG provenance storage round-trip."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from evidence_authority import (
    ensure_provenance_defaults,
    provenance_defaults,
)
from mailbox_memory_runtime import build_document_chunks
from mailbox_memory_store import InMemoryMailboxMemoryStore


def test_attachment_ingest_metadata_has_provenance_dims() -> None:
    metadata = provenance_defaults(origin="ATTACHMENT")
    assert metadata == {
        "source_origin": "ATTACHMENT",
        "evidence_authority": "CUSTOMER_DOCUMENT",
        "instruction_authority": "NONE",
    }


def test_attachment_metadata_roundtrip_via_store() -> None:
    store = InMemoryMailboxMemoryStore()
    store.upsert_attachment(
        {
            "attachment_id": "att_1",
            "case_id": "case_1",
            "message_id": "msg_1",
            "file_name": "faktura.pdf",
            "mime_type": "application/pdf",
            "metadata": {
                "attachment_business_type": "invoice",
                **provenance_defaults(origin="ATTACHMENT"),
            },
        }
    )
    store.upsert_document(
        {
            "document_id": "doc_1",
            "case_id": "case_1",
            "message_id": "msg_1",
            "attachment_id": "att_1",
            "file_name": "faktura.pdf",
            "mime_type": "application/pdf",
            "source_type": "attachment",
            "metadata": {
                "parser_id": "legacy_structured",
                **provenance_defaults(origin="ATTACHMENT"),
            },
        }
    )
    documents = store.fetch_documents_for_case("case_1")
    assert documents
    metadata = documents[0]["metadata"]
    assert metadata["source_origin"] == "ATTACHMENT"
    assert metadata["evidence_authority"] == "CUSTOMER_DOCUMENT"
    assert metadata["instruction_authority"] == "NONE"


def test_rag_chunk_metadata_roundtrip_via_store() -> None:
    store = InMemoryMailboxMemoryStore()
    store.upsert_document(
        {
            "document_id": "doc_rag",
            "case_id": "case_1",
            "message_id": "msg_1",
            "file_name": "manual.pdf",
            "mime_type": "application/pdf",
            "source_type": "attachment",
            "metadata": provenance_defaults(origin="ATTACHMENT"),
        }
    )
    chunks = build_document_chunks(
        case_id="case_1",
        document_id="doc_rag",
        file_name="manual.pdf",
        text="Panasonic manual: error code H70 means sensor failure.",
        created_at="2026-08-21T00:00:00Z",
    )
    assert chunks
    assert chunks[0]["metadata"]["source_origin"] == "ATTACHMENT"
    assert chunks[0]["metadata"]["instruction_authority"] == "NONE"

    store.replace_document_chunks("doc_rag", chunks)
    fetched = store.fetch_chunks_for_case("case_1")
    assert fetched
    assert fetched[0]["metadata"]["source_origin"] == "ATTACHMENT"
    assert fetched[0]["metadata"]["evidence_authority"] == "CUSTOMER_DOCUMENT"
    assert fetched[0]["metadata"]["instruction_authority"] == "NONE"


def test_retrieval_transport_does_not_upgrade_instruction_authority() -> None:
    # Same helper used by the RAG transport read path: explicit provenance is
    # preserved, never upgraded to a trusted origin.
    stored = provenance_defaults(origin="ATTACHMENT")
    normalized = ensure_provenance_defaults(stored, default_origin="RAG")
    assert normalized["source_origin"] == "ATTACHMENT"
    assert normalized["evidence_authority"] == "CUSTOMER_DOCUMENT"
    assert normalized["instruction_authority"] == "NONE"

    rag_explicit = {
        "source_origin": "RAG",
        "evidence_authority": "AUTHORITATIVE_DOCUMENT",
    }
    normalized_rag = ensure_provenance_defaults(rag_explicit, default_origin="RAG")
    assert normalized_rag["source_origin"] == "RAG"
    assert normalized_rag["evidence_authority"] == "AUTHORITATIVE_DOCUMENT"
    assert normalized_rag["instruction_authority"] == "NONE"


def test_legacy_missing_provenance_fails_safe() -> None:
    legacy_document = {"file_name": "manual.pdf", "parser_id": "legacy"}
    normalized = ensure_provenance_defaults(
        legacy_document,
        default_origin="ATTACHMENT",
    )
    assert normalized["source_origin"] == "ATTACHMENT"
    assert normalized["evidence_authority"] == "UNKNOWN"
    assert normalized["instruction_authority"] == "NONE"

    legacy_chunk = {"chunk_text": "old chunk"}
    normalized_chunk = ensure_provenance_defaults(
        legacy_chunk,
        default_origin="RAG",
    )
    assert normalized_chunk["source_origin"] == "RAG"
    assert normalized_chunk["evidence_authority"] == "UNKNOWN"
    assert normalized_chunk["instruction_authority"] == "NONE"
