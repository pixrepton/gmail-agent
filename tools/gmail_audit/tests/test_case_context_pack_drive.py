from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from dash_projection_v2 import build_v2_shadow_projection
from graph_store import InMemoryGraphStore, build_graph_edge, build_graph_node, stable_graph_node_id
from mailbox_memory_health import VECTOR_PATH_UNAVAILABLE, VECTOR_PATH_USED
from mailbox_memory_runtime import MailboxMemoryRuntime, build_case_context_pack, build_case_snapshot, collect_drive_case_enrichment, rank_chunks
from mailbox_memory_store import InMemoryMailboxMemoryStore
from tests.fixture_helpers import run_fixture


class _Dim3DeterministicEmb:
    """Stable 3-D embeddings for semantic ranking tests."""

    def embed_texts(self, texts: list[str]) -> list[list[float] | None]:
        out: list[list[float] | None] = []
        for raw in texts:
            t = str(raw or "").lower()
            if "querymarker_xx" in t:
                out.append([1.0, 0.0, 0.0])
            elif "chunkmarker_aa" in t:
                out.append([0.95, 0.25, 0.0])
            elif "chunkmarker_bb" in t:
                out.append([0.0, 1.0, 0.0])
            else:
                out.append([0.0, 0.0, 1.0])
        return out


class CaseContextPackDriveTests(unittest.TestCase):
    def test_case_context_and_projection_include_drive_enrichment(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        case_id = "case_drive_context"
        timestamp = "2026-04-12T11:00:00+02:00"
        store.upsert_case(
            {
                "case_id": case_id,
                "case_key": "CASE-DRIVE-CONTEXT-1",
                "thread_id": "",
                "case_family": "heat_pump_offer",
                "mailbox": "drive",
                "subject": "Jaworzno Panasonic 9 kW",
                "status": "open",
                "customer_name": "Jan Kowalski",
                "customer_email": "jan@example.com",
                "metadata": {},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        store.upsert_next_action(
            case_id,
            {
                "next_action": "review_required",
                "rationale": "Need canonical service document.",
                "source_stage": "drive",
                "payload": {},
                "updated_at": timestamp,
            },
        )
        store.upsert_drive_document(
            {
                "document_id": "drv_case_1",
                "drive_item_id": "drv_case_1",
                "parent_drive_item_id": "",
                "parent_document_id": "",
                "case_id": case_id,
                "probable_case_key": "CASE-DRIVE-CONTEXT-1",
                "file_name": "karta gwarancyjna jaworzno.pdf",
                "mime_type": "application/pdf",
                "folder_path": "Serwis/Jaworzno",
                "lane": "service_warranty",
                "document_kind": "warranty_card",
                "scope": "case_specific",
                "source_ref": "https://drive.google.com/file/d/drv_case_1",
                "extraction_status": "extracted",
                "linkage_status": "deterministic",
                "classification_confidence": 0.92,
                "extraction_confidence": 0.85,
                "link_confidence": 0.97,
                "download_mime_type": "application/pdf",
                "content_sha256": "abc123",
                "blob_path": "/tmp/drv_case_1",
                "text_content": "Model Panasonic WH-ADC0309K3E5. Gwarancja 5 lat. Przeglad co 12 miesiecy.",
                "summary_text": "Warranty card for Panasonic heat pump.",
                "metadata": {},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        store.replace_drive_document_facts(
            document_id="drv_case_1",
            rows=[
                {
                    "fact_id": "gfact_case_model",
                    "drive_document_id": "drv_case_1",
                    "case_id": case_id,
                    "probable_case_key": "CASE-DRIVE-CONTEXT-1",
                    "fact_family": "technical_reference",
                    "entity_scope": "document",
                    "fact_key": "device_model",
                    "normalized_value": "WH-ADC0309K3E5",
                    "raw_value": "WH-ADC0309K3E5",
                    "confidence": 0.92,
                    "observed_at": timestamp,
                    "source_ref": "https://drive.google.com/file/d/drv_case_1",
                    "status": "active",
                    "metadata": {},
                    "created_at": timestamp,
                },
                {
                    "fact_id": "gfact_case_warranty",
                    "drive_document_id": "drv_case_1",
                    "case_id": case_id,
                    "probable_case_key": "CASE-DRIVE-CONTEXT-1",
                    "fact_family": "warranty",
                    "entity_scope": "document",
                    "fact_key": "warranty_term",
                    "normalized_value": "5 LAT",
                    "raw_value": "5 lat",
                    "confidence": 0.86,
                    "observed_at": timestamp,
                    "source_ref": "https://drive.google.com/file/d/drv_case_1",
                    "status": "active",
                    "metadata": {},
                    "created_at": timestamp,
                },
                {
                    "fact_id": "gfact_case_service",
                    "drive_document_id": "drv_case_1",
                    "case_id": case_id,
                    "probable_case_key": "CASE-DRIVE-CONTEXT-1",
                    "fact_family": "warranty",
                    "entity_scope": "document",
                    "fact_key": "service_frequency",
                    "normalized_value": "CO12MIESIECY",
                    "raw_value": "co 12 miesiecy",
                    "confidence": 0.82,
                    "observed_at": timestamp,
                    "source_ref": "https://drive.google.com/file/d/drv_case_1",
                    "status": "active",
                    "metadata": {},
                    "created_at": timestamp,
                },
            ],
        )
        store.replace_drive_document_chunks(
            document_id="drv_case_1",
            rows=[
                {
                    "chunk_id": "drv_chunk_case_1",
                    "document_id": "drv_case_1",
                    "case_id": case_id,
                    "ordinal": 0,
                    "chunk_text": "Panasonic WH-ADC0309K3E5 karta gwarancyjna przeglad co 12 miesiecy.",
                    "token_estimate": 8,
                    "embedding_model": "text-embedding-3-large",
                    "embedding_status": "ready",
                    "metadata": {"file_name": "karta gwarancyjna jaworzno.pdf", "source_type": "drive_document_chunk"},
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            ],
        )
        store.upsert_drive_document(
            {
                "document_id": "drv_ref_1",
                "drive_item_id": "drv_ref_1",
                "parent_drive_item_id": "",
                "parent_document_id": "",
                "case_id": "",
                "probable_case_key": "",
                "file_name": "Panasonic cennik 2026.pdf",
                "mime_type": "application/pdf",
                "folder_path": "Cenniki/Panasonic",
                "lane": "commercial_pricing",
                "document_kind": "price_list",
                "scope": "company_reference",
                "source_ref": "https://drive.google.com/file/d/drv_ref_1",
                "extraction_status": "extracted",
                "linkage_status": "unresolved_candidate",
                "classification_confidence": 0.88,
                "extraction_confidence": 0.8,
                "link_confidence": 0.0,
                "download_mime_type": "application/pdf",
                "content_sha256": "ref123",
                "blob_path": "/tmp/drv_ref_1",
                "text_content": "Panasonic WH-ADC0309K3E5 price list.",
                "summary_text": "Price list with Panasonic model references.",
                "metadata": {},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )

        graph_store = InMemoryGraphStore()
        case_node = build_graph_node(
            node_type="Case",
            natural_key=case_id,
            title="CASE-DRIVE-CONTEXT-1",
            source="gdrive",
            source_ref="https://drive.google.com/file/d/drv_case_1",
            confidence=0.97,
            payload={},
            observed_at=timestamp,
        )
        model_node = build_graph_node(
            node_type="Model",
            natural_key="wh-adc0309k3e5",
            title="WH-ADC0309K3E5",
            source="gdrive",
            source_ref="https://drive.google.com/file/d/drv_case_1",
            confidence=0.88,
            payload={},
            observed_at=timestamp,
        )
        graph_store.upsert_many(
            nodes=[case_node, model_node],
            edges=[
                build_graph_edge(
                    src_node_id=stable_graph_node_id("Case", case_id),
                    dst_node_id=model_node["node_id"],
                    relation_type="document_mentions_model",
                    source="gdrive",
                    source_ref="https://drive.google.com/file/d/drv_case_1",
                    confidence=0.88,
                    metadata={},
                    observed_at=timestamp,
                )
            ],
        )

        drive_enrichment = collect_drive_case_enrichment(
            store=store,
            case_id=case_id,
            query_text="Panasonic",
            graph_store=graph_store,
        )
        snapshot = build_case_snapshot(
            case_id=case_id,
            case_record=store.fetch_case(case_id) or {},
            messages=[],
            facts=[],
            documents=[],
            events=[],
            next_action=store.fetch_next_action(case_id) or {},
            drive_enrichment=drive_enrichment,
        )
        store.upsert_snapshot(
            case_id,
            {
                "status": snapshot["status"],
                "customer_name": "Jan Kowalski",
                "customer_email": "jan@example.com",
                "recommended_next_action": snapshot["recommended_next_action"],
                "snapshot_json": snapshot,
                "updated_at": timestamp,
            },
        )

        pack = build_case_context_pack(
            store=store,
            case_id=case_id,
            query_text="Panasonic",
            graph_store=graph_store,
        )

        self.assertTrue(pack.drive_documents_summary)
        self.assertTrue(any("Service requirement detected" in item for item in pack.completeness_gaps))
        self.assertTrue(pack.graph_hints)
        self.assertTrue(pack.reference_documents)
        self.assertTrue(any(chunk["source_type"] == "drive_document_chunk" for chunk in pack.relevant_chunks))
        self.assertTrue(any(chunk["embedding_status"] == "ready" for chunk in pack.relevant_chunks))
        self.assertTrue(any(chunk["retrieval_score"] > 0 for chunk in pack.relevant_chunks))
        self.assertTrue(snapshot["drive_documents_summary"])
        self.assertTrue(snapshot["graph_hints"])

        fixture = run_fixture("new_lead")
        projection = build_v2_shadow_projection(
            fixture["intake_result"],
            run_id="fixture:drive-context",
            stage_outputs={
                "intake_result_final": fixture["intake_result"],
                "preclassification_result": fixture["preclassification"],
                "case_link_result": fixture["case_link_result"],
                "business_reasoning_result": fixture["business_result"],
                "reply_draft_result": fixture["reply_result"],
                "action_plan_result": fixture["action_plan"],
                "case_intelligence_result": fixture["case_intelligence"],
                "mailbox_memory_result": {"context_pack": pack.to_dict()},
            },
        )

        self.assertTrue(projection["case_patch"]["drive_documents_summary"])
        self.assertTrue(projection["desk_note_patch"]["completeness_gaps"])
        self.assertEqual(projection["case_patch"]["warranty_service_state"]["has_warranty_card"], True)
        self.assertEqual(projection["desk_note_patch"]["media_evidence_presence"]["evidence_present"], False)
        self.assertTrue(projection["desk_note_patch"]["related_entities"])
        self.assertTrue(projection["desk_note_patch"]["operator_visible_conflicts"])
        self.assertTrue(projection["case_patch"]["evidence_cards"])
        self.assertTrue(projection["desk_note_patch"]["evidence_cards"])
        self.assertTrue(projection["case_patch"]["service_signals"])
        self.assertEqual(projection["desk_note_patch"]["service_signals"][0]["policy_status"], "allowed_for_projection")

    def test_projection_marks_media_evidence_when_case_has_linked_media_asset(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        case_id = "case_drive_media"
        timestamp = "2026-04-12T11:00:00+02:00"
        store.upsert_case(
            {
                "case_id": case_id,
                "case_key": "CASE-DRIVE-MEDIA-1",
                "thread_id": "",
                "case_family": "heat_pump_offer",
                "mailbox": "drive",
                "subject": "Jaworzno Panasonic 9 kW",
                "status": "open",
                "customer_name": "Jan Kowalski",
                "customer_email": "jan@example.com",
                "metadata": {},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        store.upsert_drive_document(
            {
                "document_id": "drv_media_1",
                "drive_item_id": "drv_media_1",
                "parent_drive_item_id": "bundle_case_1",
                "parent_document_id": "",
                "case_id": case_id,
                "probable_case_key": "CASE-DRIVE-MEDIA-1",
                "file_name": "IMG_0001.jpg",
                "mime_type": "image/jpeg",
                "folder_path": "Realizacje/Jaworzno",
                "lane": "case_folder",
                "document_kind": "media_asset",
                "scope": "case_specific",
                "source_ref": "https://drive.google.com/file/d/drv_media_1",
                "extraction_status": "skipped_binary",
                "linkage_status": "deterministic",
                "classification_confidence": 0.9,
                "extraction_confidence": 0.0,
                "link_confidence": 0.97,
                "download_mime_type": "image/jpeg",
                "content_sha256": "media123",
                "blob_path": "/tmp/drv_media_1",
                "text_content": "",
                "summary_text": "Media asset present: IMG_0001.jpg",
                "metadata": {"parent_drive_item_id": "bundle_case_1"},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )

        graph_store = InMemoryGraphStore()
        case_node = build_graph_node(
            node_type="Case",
            natural_key=case_id,
            title="CASE-DRIVE-MEDIA-1",
            source="gdrive",
            source_ref="https://drive.google.com/file/d/drv_media_1",
            confidence=0.97,
            payload={},
            observed_at=timestamp,
        )
        bundle_node = build_graph_node(
            node_type="MediaBundle",
            natural_key="bundle_case_1",
            title="Realizacje/Jaworzno",
            source="gdrive",
            source_ref="https://drive.google.com/file/d/drv_media_1",
            confidence=0.75,
            payload={},
            observed_at=timestamp,
        )
        asset_node = build_graph_node(
            node_type="MediaAsset",
            natural_key="drv_media_1",
            title="IMG_0001.jpg",
            source="gdrive",
            source_ref="https://drive.google.com/file/d/drv_media_1",
            confidence=0.75,
            payload={},
            observed_at=timestamp,
        )
        graph_store.upsert_many(
            nodes=[case_node, bundle_node, asset_node],
            edges=[
                build_graph_edge(
                    src_node_id=stable_graph_node_id("Case", case_id),
                    dst_node_id=bundle_node["node_id"],
                    relation_type="case_has_media_bundle",
                    source="gdrive",
                    source_ref="https://drive.google.com/file/d/drv_media_1",
                    confidence=0.97,
                    metadata={"asset_document_id": "drv_media_1"},
                    observed_at=timestamp,
                ),
                build_graph_edge(
                    src_node_id=bundle_node["node_id"],
                    dst_node_id=asset_node["node_id"],
                    relation_type="media_bundle_has_asset",
                    source="gdrive",
                    source_ref="https://drive.google.com/file/d/drv_media_1",
                    confidence=0.75,
                    metadata={},
                    observed_at=timestamp,
                ),
            ],
        )

        pack = build_case_context_pack(
            store=store,
            case_id=case_id,
            query_text="Jaworzno",
            graph_store=graph_store,
        )

        fixture = run_fixture("new_lead")
        projection = build_v2_shadow_projection(
            fixture["intake_result"],
            run_id="fixture:drive-media",
            stage_outputs={
                "intake_result_final": fixture["intake_result"],
                "preclassification_result": fixture["preclassification"],
                "case_link_result": fixture["case_link_result"],
                "business_reasoning_result": fixture["business_result"],
                "reply_draft_result": fixture["reply_result"],
                "action_plan_result": fixture["action_plan"],
                "case_intelligence_result": fixture["case_intelligence"],
                "mailbox_memory_result": {"context_pack": pack.to_dict()},
            },
        )

        self.assertEqual(projection["case_patch"]["media_evidence_presence"]["evidence_present"], True)
        self.assertEqual(projection["case_patch"]["media_evidence_presence"]["media_asset_count"], 1)
        self.assertTrue(
            any(item["relation_type"] == "case_has_media_bundle" for item in projection["desk_note_patch"]["related_entities"])
        )

    def test_relevant_chunks_used_vector_and_semantic_score(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        case_id = "case_vector_ctx"
        ts = "2026-04-12T12:00:00+02:00"
        store.upsert_case(
            {
                "case_id": case_id,
                "case_key": "CASE-VEC-1",
                "thread_id": "",
                "case_family": "heat_pump_offer",
                "mailbox": "drive",
                "subject": "Vector ctx",
                "status": "open",
                "customer_name": "Test",
                "customer_email": "t@example.com",
                "metadata": {},
                "created_at": ts,
                "updated_at": ts,
            }
        )
        store.upsert_drive_document(
            {
                "document_id": "drv_vec_1",
                "drive_item_id": "drv_vec_1",
                "parent_drive_item_id": "",
                "parent_document_id": "",
                "case_id": case_id,
                "probable_case_key": "CASE-VEC-1",
                "file_name": "doc.pdf",
                "mime_type": "application/pdf",
                "folder_path": "/",
                "lane": "service_warranty",
                "document_kind": "warranty_card",
                "scope": "case_specific",
                "source_ref": "https://drive.example/drv_vec_1",
                "extraction_status": "extracted",
                "linkage_status": "deterministic",
                "classification_confidence": 0.9,
                "extraction_confidence": 0.85,
                "link_confidence": 0.97,
                "download_mime_type": "application/pdf",
                "content_sha256": "sha",
                "blob_path": "/tmp/drv_vec_1",
                "text_content": "CHUNKMARKER_BB noise",
                "summary_text": "s",
                "metadata": {},
                "created_at": ts,
                "updated_at": ts,
            }
        )
        store.replace_drive_document_chunks(
            document_id="drv_vec_1",
            rows=[
                {
                    "chunk_id": "vec_chunk_a",
                    "document_id": "drv_vec_1",
                    "case_id": case_id,
                    "ordinal": 0,
                    "chunk_text": "CHUNKMARKER_AA semantic lane alpha",
                    "token_estimate": 6,
                    "embedding_model": "test-3d",
                    "embedding_status": "ready",
                    "embedding": [0.95, 0.25, 0.0],
                    "metadata": {"source_type": "drive_document_chunk"},
                    "created_at": ts,
                    "updated_at": ts,
                },
                {
                    "chunk_id": "vec_chunk_b",
                    "document_id": "drv_vec_1",
                    "case_id": case_id,
                    "ordinal": 1,
                    "chunk_text": "CHUNKMARKER_BB unrelated text zzz",
                    "token_estimate": 6,
                    "embedding_model": "test-3d",
                    "embedding_status": "ready",
                    "embedding": [0.0, 1.0, 0.0],
                    "metadata": {"source_type": "drive_document_chunk"},
                    "created_at": ts,
                    "updated_at": ts,
                },
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            blob_root = Path(tmp) / "blobs"
            rt = MailboxMemoryRuntime(
                store=store,
                blob_root=blob_root,
                stage_mode="shadow",
                vector_enabled=True,
                embedding_model="test-3d",
                embedding_runtime=_Dim3DeterministicEmb(),
            )
            rt.bootstrap()
            pack = build_case_context_pack(
                store=store,
                case_id=case_id,
                query_text="querymarker_xx",
                graph_store=None,
                retrieval_runtime=rt,
            )
            self.assertTrue(pack.relevant_chunks)
            top = pack.relevant_chunks[0]
            self.assertEqual(top["chunk_id"], "vec_chunk_a")
            self.assertTrue(top["retrieval_signals"]["used_vector"])
            self.assertGreater(top["retrieval_signals"]["vector_score"], 0.5)
            self.assertEqual(top["retrieval_signals"]["retrieval_mode"], "hybrid_vector_lexical")
            self.assertEqual(pack.vector_retrieval["vector_path_status"], VECTOR_PATH_USED)
            self.assertEqual(pack.vector_retrieval["retrieval_mode"], "hybrid_vector_lexical")
            self.assertEqual(pack.vector_retrieval["fallback_reason"], "")
            self.assertGreater(int(pack.vector_retrieval.get("semantic_candidate_count") or 0), 0)
            self.assertEqual(top["retrieval_signals"]["vector_path_status"], VECTOR_PATH_USED)

    def test_rank_chunks_vector_score_can_change_selection(self) -> None:
        ts = "2026-04-12T12:00:00+02:00"
        ranked = rank_chunks(
            [
                {
                    "chunk_id": "lexical_chunk",
                    "chunk_text": "service note with a weak lexical overlap",
                    "ordinal": 0,
                    "created_at": ts,
                },
                {
                    "chunk_id": "semantic_chunk",
                    "chunk_text": "archive context without direct query words",
                    "ordinal": 1,
                    "created_at": ts,
                },
            ],
            query_text="querymarker_xx service schedule warranty",
            limit=2,
            vector_scores={"lexical_chunk": 0.0, "semantic_chunk": 1.0},
            vector_path_status=VECTOR_PATH_USED,
            vector_path_detail="semantic_fetch_ok_candidates=1",
        )

        self.assertEqual(ranked[0]["chunk_id"], "semantic_chunk")
        self.assertTrue(ranked[0]["retrieval_signals"]["used_vector"])
        self.assertEqual(ranked[0]["retrieval_signals"]["vector_score"], 1.0)
        self.assertEqual(ranked[0]["retrieval_signals"]["retrieval_mode"], "hybrid_vector_lexical")
        self.assertEqual(
            ranked[0]["retrieval_signals"]["ranking_reason"],
            "hybrid_score=lexical_0.50+vector_0.35+freshness_0.15",
        )

    def test_rank_chunks_vector_unavailable_fallback_and_invalid_score_are_safe(self) -> None:
        ts = "2026-04-12T12:00:00+02:00"
        chunks = [
            {
                "chunk_id": "lexical_chunk",
                "chunk_text": "service schedule visible context",
                "ordinal": 0,
                "created_at": ts,
            },
            {
                "chunk_id": "semantic_chunk",
                "chunk_text": "archive context without direct query words",
                "ordinal": 1,
                "created_at": ts,
            },
        ]

        fallback_ranked = rank_chunks(
            chunks,
            query_text="service schedule",
            limit=2,
            vector_scores=None,
            vector_path_status=VECTOR_PATH_UNAVAILABLE,
            vector_path_detail="embedding_runtime_missing",
        )

        self.assertEqual(fallback_ranked[0]["chunk_id"], "lexical_chunk")
        self.assertFalse(fallback_ranked[0]["retrieval_signals"]["used_vector"])
        self.assertEqual(fallback_ranked[0]["retrieval_signals"]["retrieval_mode"], "lexical_freshness_fallback")
        self.assertEqual(fallback_ranked[0]["retrieval_signals"]["fallback_reason"], "embedding_runtime_missing")

        invalid_ranked = rank_chunks(
            chunks,
            query_text="querymarker_xx",
            limit=2,
            vector_scores={"semantic_chunk": "not-a-float"},
            vector_path_status=VECTOR_PATH_USED,
            vector_path_detail="semantic_fetch_ok_candidates=1",
        )
        semantic = next(item for item in invalid_ranked if item["chunk_id"] == "semantic_chunk")
        self.assertFalse(semantic["retrieval_signals"]["used_vector"])
        self.assertEqual(semantic["retrieval_signals"]["vector_score"], 0.0)
        self.assertEqual(semantic["retrieval_signals"]["fallback_reason"], "invalid_vector_score")


if __name__ == "__main__":
    unittest.main()
