"""Bounded Google Drive ingest runtime for shared memory + graph v1."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from attachment_content_extraction import parse_attachment_document
from document_parse_runtime import build_parse_config_from_settings
from config import Settings, document_intelligence_promote_facts_enabled
from drive_case_linker import link_drive_candidate
from drive_client import GoogleDriveClient, GoogleDriveClientError
from drive_ingest_models import DriveDocumentRecord, DriveIngestCandidate, DriveIngestResult, DriveGraphUpsert
from drive_lane_classifier import apply_classification
from case_routing import enrich_case_row_before_upsert
from embedding_runtime import build_embedding_runtime
from graph_store import build_graph_edge, build_graph_node, stable_graph_node_id
from mailbox_memory_runtime import (
    apply_embeddings_to_chunk_rows,
    build_case_context_pack,
    build_case_snapshot,
    build_document_chunks,
    collect_drive_case_enrichment,
    stable_id,
    summarize_document_text,
)
from mailbox_memory.active_facts import fetch_current_facts_for_case, is_live_fact
from mailbox_memory_store import InMemoryMailboxMemoryStore, MailboxMemoryStore, PostgresMailboxMemoryStore


ORDER_RE = re.compile(r"\bZAM[- /]?\d+\b", re.IGNORECASE)
INVOICE_RE = re.compile(r"\b(?:FV[- /]?\d+|FAKTURA[- /]?\d+)\b", re.IGNORECASE)
DEPOSIT_RE = re.compile(r"\bZAL[- /]?\d+\b", re.IGNORECASE)
MODEL_RE = re.compile(r"\b(?:WH|KIT|PUZ|ERST|MAC|PAR|LG|AS|AE)[-A-Z0-9]{3,}\b")
SERIAL_RE = re.compile(r"(?:S/N|SN|Serial(?: Number)?)\s*[:#]?\s*([A-Z0-9-]{5,})", re.IGNORECASE)
MONEY_RE = re.compile(r"(?P<value>\d[\d\s]{0,12}(?:[.,]\d{1,2})?)\s*(?:PLN|zł|zl)\b", re.IGNORECASE)
DATE_RE = re.compile(r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b")
SERVICE_FREQ_RE = re.compile(r"co\s+(\d+)\s+(?:miesi(?:a|ą)cy|mies\.?)", re.IGNORECASE)
WARRANTY_TERM_RE = re.compile(r"(\d+)\s*(?:lat|miesi(?:a|ą)cy)\s*(?:gwarancji|warranty)?", re.IGNORECASE)
ADDRESS_RE = re.compile(
    r"\b(?:[A-ZŻŹĆĄŚĘŁÓŃ][A-Za-zŻŹĆĄŚĘŁÓŃżźćńółęąś\-]+(?:\s+[A-ZŻŹĆĄŚĘŁÓŃ][A-Za-zŻŹĆĄŚĘŁÓŃżźćńółęąś\-]+)?\s+\d+[A-Za-z]?(?:/\d+)?)"
)
MANUFACTURERS = ("Panasonic", "Mitsubishi", "Haier", "LG")
BLOCKED_TITLES = {
    "umowa-pc-plaza.pdf",
    "umowa-pc-olkusz.pdf",
    "regulice-serwis-5-1-2026.pdf",
    "protokol-chorzow.pdf",
    "protokół-chorzów.pdf",
}
SKIP_TITLE_SUBSTRINGS = (
    "projekt techniczny",
    "projekt instalacyjny",
    "projekt instalacji",
    "projekt rekuperacji",
    "projekt wod-kan",
    "projekt wod/kan",
)
RELATION_BY_KIND = {
    "contract": "case_has_contract",
    "order": "case_has_order",
    "deposit_invoice": "case_has_invoice",
    "invoice": "case_has_invoice",
    "warranty_card": "case_has_warranty",
    "service_protocol": "case_has_service_protocol",
    "media_bundle": "case_has_media_bundle",
}
KIND_TO_NODE_TYPE = {
    "contract": "Contract",
    "order": "Order",
    "deposit_invoice": "Invoice",
    "invoice": "Invoice",
    "warranty_card": "WarrantyCard",
    "service_protocol": "ServiceProtocol",
    "price_list": "PriceList",
    "pricing_workbook": "PricingWorkbook",
    "technical_reference": "TechnicalReference",
    "media_bundle": "MediaBundle",
    "media_asset": "MediaAsset",
}


@dataclass(slots=True)
class DriveIngestRuntime:
    settings: Settings
    store: MailboxMemoryStore
    blob_root: Path
    client: GoogleDriveClient
    graph_store: Any | None = None
    docling_enabled: bool = False
    docling_options: dict[str, Any] = field(default_factory=dict)
    vector_enabled: bool = False
    embedding_model: str = ""
    embedding_runtime: Any | None = None

    def bootstrap(self) -> None:
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self.store.bootstrap()
        if self.graph_store is not None:
            self.graph_store.bootstrap()

    def ingest_batch(
        self,
        *,
        limit: int = 50,
        root_folder_id: str = "",
        page_token: str = "",
        run_id: str = "",
        refresh_document_intelligence: bool = False,
    ) -> DriveIngestResult:
        bounded_limit = max(1, int(limit))
        resolved_root = root_folder_id.strip() or str(self.settings.google_drive_root_folder_id or "").strip()
        if not resolved_root:
            raise GoogleDriveClientError("Drive ingest requires GOOGLE_DRIVE_ROOT_FOLDER_ID or --root-folder-id.")

        effective_run_id = run_id.strip() or f"drive_ingest_{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}"
        observed_at = datetime.now().astimezone().isoformat()
        processed = 0
        stored_document_count = 0
        linked_case_count = 0
        graph_node_count = 0
        graph_edge_count = 0
        events: list[dict[str, Any]] = []
        warnings: list[str] = []
        documents: list[dict[str, Any]] = []
        affected_case_ids: set[str] = set()
        signal_runtime_mode = str(getattr(self.settings, "signal_runtime_mode", "legacy") or "legacy").strip().lower()
        use_signal_runtime = signal_runtime_mode in {"shadow", "active"}
        if use_signal_runtime:
            from drive_signal_adapter import build_drive_signal_runtime_context

            signal_runtime_context = build_drive_signal_runtime_context(
                settings=self.settings,
                store=self.store,
                graph_store=self.graph_store,
            )
        queue: list[tuple[str, str, str]] = [(resolved_root, "", page_token.strip())]
        trailing_cursor = ""

        while queue and processed < bounded_limit:
            folder_id, folder_path, cursor = queue.pop(0)
            payload = self.client.list_children(
                folder_id=folder_id,
                page_token=cursor,
                page_size=min(self.settings.google_drive_batch_page_size, bounded_limit),
            )
            trailing_cursor = str(payload.get("next_page_token") or "").strip()
            for item in payload.get("items") or []:
                if processed >= bounded_limit:
                    break
                descriptor = self.client.describe_item(item, folder_path=folder_path)
                candidate = DriveIngestCandidate(**descriptor)
                apply_classification(candidate)
                processed_result = self.process_candidate(
                    candidate,
                    observed_at=observed_at,
                    signal_runtime_context=signal_runtime_context if use_signal_runtime else None,
                    signal_runtime_mode=signal_runtime_mode,
                )
                item_events = list(processed_result.get("events") or [])
                document_row = dict(processed_result.get("document_row") or {})
                graph_payload = dict(processed_result.get("graph_upsert") or {})
                graph_upsert = DriveGraphUpsert(
                    nodes=list(graph_payload.get("nodes") or []),
                    edges=list(graph_payload.get("edges") or []),
                )
                events.extend(item_events)
                if document_row is not None:
                    documents.append(document_row)
                    stored_document_count += 1
                    if str(document_row.get("case_id") or ""):
                        affected_case_ids.add(str(document_row.get("case_id") or ""))
                    if graph_upsert is not None:
                        graph_node_count += len(graph_upsert.nodes)
                        graph_edge_count += len(graph_upsert.edges)
                if candidate.is_folder:
                    next_path = "/".join(part for part in (folder_path, candidate.title) if part)
                    queue.append((candidate.drive_item_id, next_path, ""))
                processed += 1
            if trailing_cursor and processed < bounded_limit:
                queue.insert(0, (folder_id, folder_path, trailing_cursor))

        if not use_signal_runtime:
            for case_id in affected_case_ids:
                self.refresh_case_projection(case_id)
                linked_case_count += 1
        else:
            linked_case_count = len(affected_case_ids)

        stats = {
            "processed_count": processed,
            "stored_document_count": stored_document_count,
            "linked_case_count": linked_case_count,
            "graph_node_count": graph_node_count,
            "graph_edge_count": graph_edge_count,
        }
        self.store.upsert_drive_ingest_run(
            {
                "run_id": effective_run_id,
                "root_folder_id": resolved_root,
                "cursor": trailing_cursor,
                "status": "completed",
                "stats": stats,
                "created_at": observed_at,
                "updated_at": observed_at,
            }
        )
        if refresh_document_intelligence and affected_case_ids:
            self.bounded_refresh_document_intelligence_for_cases(affected_case_ids, observed_at=observed_at)
        return DriveIngestResult(
            enabled=True,
            run_id=effective_run_id,
            cursor=trailing_cursor,
            processed_count=processed,
            stored_document_count=stored_document_count,
            linked_case_count=linked_case_count,
            graph_node_count=graph_node_count,
            graph_edge_count=graph_edge_count,
            documents=documents,
            events=events,
            warnings=warnings,
        )

    def rebuild_graph(self, *, limit: int = 200, case_id: str = "") -> dict[str, Any]:
        if self.graph_store is None:
            raise RuntimeError("Drive graph rebuild requires GOOGLE_DRIVE_GRAPH_ENABLED and mailbox memory Postgres.")
        documents = (
            self.store.fetch_drive_documents_for_case(case_id, limit=limit)
            if case_id.strip()
            else self.store.fetch_drive_documents(limit=limit)
        )
        node_count = 0
        edge_count = 0
        for document_row in documents:
            facts = list(self.store.fetch_drive_facts_for_document(str(document_row.get("document_id") or "")) or [])
            graph_upsert = self._build_graph_upsert(document_row, facts=facts)
            self.graph_store.upsert_many(nodes=graph_upsert.nodes, edges=graph_upsert.edges)
            node_count += len(graph_upsert.nodes)
            edge_count += len(graph_upsert.edges)
        return {
            "rebuilt_documents": len(documents),
            "graph_node_count": node_count,
            "graph_edge_count": edge_count,
        }

    def refresh_case_projection(self, case_id: str) -> dict[str, Any]:
        case = self.store.fetch_case(case_id) or {
            "case_id": case_id,
            "case_key": "",
            "case_family": "unknown",
            "status": "open",
            "subject": "Drive-linked case",
            "metadata": {"source": "drive"},
        }
        snapshot_row = build_case_snapshot(
            case_id=case_id,
            case_record=case,
            messages=self.store.fetch_messages_for_case(case_id, limit=10),
            facts=fetch_current_facts_for_case(self.store, case_id),
            documents=self.store.fetch_documents_for_case(case_id, limit=8),
            events=self.store.fetch_events_for_case(case_id, limit=20),
            next_action=self.store.fetch_next_action(case_id) or {},
            drive_enrichment=collect_drive_case_enrichment(
                store=self.store,
                case_id=case_id,
                graph_store=self.graph_store,
            ),
        )
        now_iso = datetime.now().astimezone().isoformat()
        self.store.upsert_snapshot(
            case_id,
            {
                "status": str(snapshot_row.get("status") or "open"),
                "customer_name": str((snapshot_row.get("customer") or {}).get("name") or ""),
                "customer_email": str((snapshot_row.get("customer") or {}).get("email") or ""),
                "recommended_next_action": str(snapshot_row.get("recommended_next_action") or ""),
                "snapshot_json": snapshot_row,
                "updated_at": now_iso,
            },
        )
        context_pack = build_case_context_pack(
            store=self.store,
            case_id=case_id,
            query_text="drive",
            graph_store=self.graph_store,
            retrieval_runtime=self,
        )
        return {
            "case_id": case_id,
            "snapshot": snapshot_row,
            "context_pack": context_pack.to_dict(),
        }

    def _drive_skip_reason(self, candidate: DriveIngestCandidate) -> str:
        return drive_skip_reason(
            candidate,
            max_download_bytes=int(self.settings.google_drive_max_download_bytes),
        )

    def _build_skipped_candidate_result(
        self,
        candidate: DriveIngestCandidate,
        *,
        observed_at: str,
        skip_reason: str,
        signal_runtime_context: Any | None,
        signal_runtime_mode: str | None,
    ) -> dict[str, Any]:
        resolved_mode = str(signal_runtime_mode or getattr(self.settings, "signal_runtime_mode", "legacy") or "legacy").strip().lower()
        skip_event = self._build_event_row(
            case_id="",
            event_type="drive_document_skipped",
            summary_text=f"Drive ingest skipped ({skip_reason}): {candidate.title}",
            occurred_at=observed_at,
            payload={
                "drive_item_id": candidate.drive_item_id,
                "skip_reason": skip_reason,
                "size_bytes": int(candidate.size_bytes or 0),
            },
            source_refs=[{"type": "gdrive", "source_ref": candidate.source_ref}],
        )
        empty_document: dict[str, Any] = {}
        if resolved_mode in {"shadow", "active"}:
            from drive_signal_adapter import build_drive_signal_runtime_context, run_drive_signal_runtime

            normalized = {
                "change_kind": "drive_document_skipped",
                "source_ref": {
                    "file_id": candidate.drive_item_id,
                    "change_id": candidate.drive_item_id,
                    "revision_id": str(candidate.modified_time or candidate.drive_item_id),
                    "modified_time": str(candidate.modified_time or observed_at),
                    "source_ref": candidate.source_ref,
                },
                "signal_summary_pl": f"Drive pominięty ({skip_reason}): {candidate.title}",
                "document_row": {},
                "chunk_rows": [],
                "fact_rows": [],
                "event_rows": [skip_event],
                "graph_upsert": {"nodes": [], "edges": []},
                "case_seed_row": {},
                "case_id": "",
                "case_key": "",
                "linkage_status": "skipped_policy",
                "link_reasons": [skip_reason],
                "conflicts": [],
            }
            context = signal_runtime_context or build_drive_signal_runtime_context(
                settings=self.settings,
                store=self.store,
                graph_store=self.graph_store,
            )
            drive_signal_result = run_drive_signal_runtime(
                settings=self.settings,
                runtime_context=context,
                change_kind="drive_document_skipped",
                source_ref=dict(normalized["source_ref"]),
                observed_at=observed_at,
                signal_summary_pl=str(normalized["signal_summary_pl"]),
                payload=normalized,
                raw_observation=None,
                triage_result={"lane": candidate.lane, "skip_reason": skip_reason},
                dry_run=resolved_mode == "shadow",
            )
            return {
                "mode": resolved_mode,
                "triage_result": {"skip_reason": skip_reason},
                "normalized": normalized,
                "signal_runtime_result": drive_signal_result,
                "events": [skip_event],
                "document_row": empty_document,
                "graph_upsert": {"nodes": [], "edges": []},
            }
        return {
            "mode": "legacy",
            "triage_result": {"skip_reason": skip_reason},
            "events": [skip_event],
            "document_row": empty_document,
            "graph_upsert": {"nodes": [], "edges": []},
        }

    def process_candidate(
        self,
        candidate: DriveIngestCandidate,
        *,
        observed_at: str,
        signal_runtime_context: Any | None = None,
        signal_runtime_mode: str | None = None,
    ) -> dict[str, Any]:
        skip_reason = self._drive_skip_reason(candidate)
        if skip_reason:
            return self._build_skipped_candidate_result(
                candidate,
                observed_at=observed_at,
                skip_reason=skip_reason,
                signal_runtime_mode=signal_runtime_mode,
                signal_runtime_context=signal_runtime_context,
            )
        resolved_mode = str(signal_runtime_mode or getattr(self.settings, "signal_runtime_mode", "legacy") or "legacy").strip().lower()
        use_signal_runtime = resolved_mode in {"shadow", "active"}
        raw_observation = self._record_drive_candidate_observation(candidate=candidate, observed_at=observed_at)
        triage_result = self._build_drive_triage_result(raw_observation)
        if use_signal_runtime:
            from drive_signal_adapter import build_drive_signal_runtime_context, run_drive_signal_runtime

            normalized = self._normalize_candidate(candidate, observed_at=observed_at)
            context = signal_runtime_context or build_drive_signal_runtime_context(
                settings=self.settings,
                store=self.store,
                graph_store=self.graph_store,
            )
            drive_signal_result = run_drive_signal_runtime(
                settings=self.settings,
                runtime_context=context,
                change_kind=str(normalized.get("change_kind") or "drive_document_added"),
                source_ref=dict(normalized.get("source_ref") or {}),
                observed_at=observed_at,
                signal_summary_pl=str(normalized.get("signal_summary_pl") or candidate.title),
                payload=dict(normalized.get("signal_payload") or {}),
                raw_observation=raw_observation,
                triage_result=triage_result,
                dry_run=resolved_mode == "shadow",
            )
            mailbox_events = {}
            if drive_signal_result.reconcile_result is not None:
                mailbox_events = dict(drive_signal_result.reconcile_result.mailbox_memory_result or {})
            return {
                "mode": resolved_mode,
                "triage_result": triage_result,
                "normalized": normalized,
                "signal_runtime_result": drive_signal_result,
                "events": list(mailbox_events.get("events") or normalized.get("event_rows") or []),
                "document_row": dict(normalized.get("document_row") or {}),
                "graph_upsert": dict(normalized.get("graph_upsert") or {}),
            }

        item_events, document_row, graph_upsert = self._process_candidate(candidate, observed_at=observed_at)
        return {
            "mode": "legacy",
            "triage_result": triage_result,
            "events": list(item_events),
            "document_row": dict(document_row or {}),
            "graph_upsert": {"nodes": list((graph_upsert.nodes if graph_upsert else [])), "edges": list((graph_upsert.edges if graph_upsert else []))},
        }

    def process_removed_item(
        self,
        *,
        drive_item_id: str,
        change_id: str,
        observed_at: str,
        signal_runtime_context: Any | None = None,
        signal_runtime_mode: str | None = None,
    ) -> dict[str, Any]:
        resolved_mode = str(signal_runtime_mode or getattr(self.settings, "signal_runtime_mode", "legacy") or "legacy").strip().lower()
        raw_observation = self._record_drive_removed_observation(
            drive_item_id=drive_item_id,
            change_id=change_id,
            observed_at=observed_at,
        )
        triage_result = self._build_drive_triage_result(raw_observation)
        existing_document = self.store.fetch_drive_document_by_item_id(drive_item_id) or {}
        case_id = str(existing_document.get("case_id") or "").strip()
        case_key = str(existing_document.get("probable_case_key") or "").strip()
        removal_event = self._build_event_row(
            case_id=case_id,
            event_type="drive_document_removed",
            summary_text=f"Drive document removed: {str(existing_document.get('file_name') or drive_item_id)}",
            occurred_at=observed_at,
            payload={"drive_item_id": drive_item_id, "change_id": change_id},
            source_refs=[{"type": "gdrive", "source_ref": str(existing_document.get("source_ref") or "")}],
        )
        if resolved_mode not in {"shadow", "active"}:
            self.store.append_event(removal_event)
            return {
                "mode": "legacy",
                "triage_result": triage_result,
                "events": [removal_event],
                "document_row": dict(existing_document),
                "graph_upsert": {"nodes": [], "edges": []},
            }

        from drive_signal_adapter import build_drive_signal_runtime_context, run_drive_signal_runtime

        payload = {
            "document_row": dict(existing_document),
            "fact_rows": [],
            "event_rows": [removal_event],
            "graph_upsert": {"nodes": [], "edges": []},
            "case_seed_row": {},
            "case_id": case_id,
            "case_key": case_key,
            "linkage_status": "removed",
            "link_reasons": ["drive_change_removed"],
            "conflicts": [],
        }
        context = signal_runtime_context or build_drive_signal_runtime_context(
            settings=self.settings,
            store=self.store,
            graph_store=self.graph_store,
        )
        signal_result = run_drive_signal_runtime(
            settings=self.settings,
            runtime_context=context,
            change_kind="drive_document_removed",
            source_ref={
                "file_id": drive_item_id,
                "change_id": change_id,
                "revision_id": change_id,
                "modified_time": observed_at,
                "source_ref": str(existing_document.get("source_ref") or ""),
            },
            observed_at=observed_at,
            signal_summary_pl=f"Drive usuniecie dokumentu {str(existing_document.get('file_name') or drive_item_id)}",
            payload=payload,
            raw_observation=raw_observation,
            triage_result=triage_result,
            dry_run=resolved_mode == "shadow",
        )
        return {
            "mode": resolved_mode,
            "triage_result": triage_result,
            "signal_runtime_result": signal_result,
            "events": [removal_event],
            "document_row": dict(existing_document),
            "graph_upsert": {"nodes": [], "edges": []},
        }

    def _record_drive_candidate_observation(
        self,
        *,
        candidate: DriveIngestCandidate,
        observed_at: str,
    ):
        from drive_signal_adapter import build_drive_raw_observation
        from raw_observation_journal import RawObservationJournal

        journal = RawObservationJournal(
            self.store,
            jsonl_mirror_enabled=bool(getattr(self.settings, "signal_journal_jsonl_mirror_enabled", False)),
            jsonl_mirror_path=self._raw_observation_jsonl_path(),
        )
        observation = build_drive_raw_observation(
            source_ref={
                "file_id": candidate.drive_item_id,
                "revision_id": candidate.modified_time or candidate.drive_item_id,
                "modified_time": candidate.modified_time or observed_at,
                "parent_drive_item_id": candidate.parent_drive_item_id,
                "source_ref": candidate.source_ref,
            },
            observed_at=observed_at,
            payload={"candidate": candidate.to_dict()},
            created_by_runtime="drive_ingest_runtime.process_candidate",
            observation_kind="drive_candidate_observed",
        )
        return journal.append(observation).observation

    def _record_drive_removed_observation(
        self,
        *,
        drive_item_id: str,
        change_id: str,
        observed_at: str,
    ):
        from drive_signal_adapter import build_drive_raw_observation
        from raw_observation_journal import RawObservationJournal

        journal = RawObservationJournal(
            self.store,
            jsonl_mirror_enabled=bool(getattr(self.settings, "signal_journal_jsonl_mirror_enabled", False)),
            jsonl_mirror_path=self._raw_observation_jsonl_path(),
        )
        observation = build_drive_raw_observation(
            source_ref={
                "file_id": drive_item_id,
                "change_id": change_id,
                "revision_id": change_id,
                "modified_time": observed_at,
            },
            observed_at=observed_at,
            payload={
                "drive_item_id": drive_item_id,
                "change_id": change_id,
                "removed": True,
            },
            created_by_runtime="drive_ingest_runtime.process_removed_item",
            observation_kind="drive_removed_item_observed",
        )
        return journal.append(observation).observation

    def _raw_observation_jsonl_path(self) -> Path:
        target = self.blob_root.parent / "signal_runtime" / "raw_observations.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _build_drive_triage_result(self, raw_observation: Any) -> dict[str, Any]:
        from observation_triage import triage_drive_observation

        return triage_drive_observation(raw_observation)

    def _append_event(
        self,
        *,
        case_id: str,
        event_type: str,
        summary_text: str,
        occurred_at: str,
        payload: dict[str, Any],
        source_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        event = self._build_event_row(
            case_id=case_id,
            event_type=event_type,
            summary_text=summary_text,
            occurred_at=occurred_at,
            payload=payload,
            source_refs=source_refs,
        )
        self.store.append_event(event)
        return event

    def _build_event_row(
        self,
        *,
        case_id: str,
        event_type: str,
        summary_text: str,
        occurred_at: str,
        payload: dict[str, Any],
        source_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "event_id": stable_id("drvevt", case_id, event_type, summary_text[:64], occurred_at),
            "case_id": case_id,
            "message_id": "",
            "thread_id": "",
            "event_type": event_type,
            "occurred_at": occurred_at,
            "summary_text": summary_text,
            "payload": payload,
            "source_refs": source_refs,
        }

    def _write_blob(self, content_sha256: str, data: bytes) -> str:
        prefix = content_sha256[:2]
        target = self.blob_root / prefix / content_sha256
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        return str(target)

    def _normalize_candidate_meta(
        self, candidate: DriveIngestCandidate, *, observed_at: str
    ) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        document_id = stable_id("gdoc", candidate.drive_item_id)
        content_sha = ""
        blob_path = ""
        text_content = ""
        summary_text = ""
        extraction_confidence = 0.0
        extraction_status = "skipped_folder" if candidate.is_folder else "pending"
        download_mime_type = candidate.mime_type
        extraction_method = ""
        extraction_metadata: dict[str, Any] = {"warnings": []}

        events.append(
            self._build_event_row(
                case_id="",
                event_type="drive_lane_classified",
                summary_text=f"Drive lane {candidate.lane} dla {candidate.title}",
                occurred_at=observed_at,
                payload={
                    "drive_item_id": candidate.drive_item_id,
                    "lane": candidate.lane,
                    "document_kind": candidate.document_kind,
                    "scope": candidate.scope,
                },
                source_refs=[{"type": "gdrive", "source_ref": candidate.source_ref}],
            )
        )

        skip_reason = self._drive_skip_reason(candidate)
        if skip_reason:
            extraction_status = "skipped_policy"
            summary_text = f"Drive ingest skipped: {skip_reason}"
            events.append(
                self._build_event_row(
                    case_id="",
                    event_type="drive_document_skipped",
                    summary_text=summary_text,
                    occurred_at=observed_at,
                    payload={
                        "drive_item_id": candidate.drive_item_id,
                        "skip_reason": skip_reason,
                        "size_bytes": int(candidate.size_bytes or 0),
                    },
                    source_refs=[{"type": "gdrive", "source_ref": candidate.source_ref}],
                )
            )
        elif not candidate.is_folder and candidate.document_kind != "media_asset":
            try:
                downloaded = self.client.download_content(
                    candidate.metadata,
                    max_bytes=self.settings.google_drive_max_download_bytes,
                )
                raw_bytes = downloaded.data
                download_mime_type = downloaded.mime_type
                content_sha = hashlib.sha256(raw_bytes).hexdigest()
                blob_path = self._write_blob(content_sha, raw_bytes)
                parse_config = build_parse_config_from_settings(self.settings)
                parse_result = parse_attachment_document(
                    raw_bytes,
                    mime_type=download_mime_type,
                    file_name=candidate.title,
                    docling_enabled=parse_config.docling_enabled,
                    unstructured_enabled=parse_config.unstructured_enabled,
                    parser_chain=parse_config.resolved_chain(),
                    docling_options=dict(parse_config.docling_options),
                    structured_facts_enabled=parse_config.structured_facts_enabled,
                )
                extraction = parse_result.to_extraction_dict()
                text_content = str(extraction.get("extracted_text") or "")
                extraction_confidence = float(extraction.get("extraction_confidence") or 0.0)
                extraction_metadata = dict(extraction.get("metadata") or {})
                extraction_status = map_extraction_status(
                    extraction_status=str(extraction.get("extraction_status") or ""),
                    title=candidate.title,
                    extracted_text=text_content,
                )
                extraction_method = str(extraction.get("extraction_method") or "")
                summary_text = summarize_document_text(text_content, file_name=candidate.title)
            except GoogleDriveClientError as exc:
                extraction_status = "blocked" if "max bytes" in str(exc).lower() else "failed"
                summary_text = f"Drive ingest: {str(exc)}"
                events.append(
                    self._build_event_row(
                        case_id="",
                        event_type="drive_extraction_failed",
                        summary_text=f"Nie udalo sie pobrac/odczytac {candidate.title}",
                        occurred_at=observed_at,
                        payload={"error": str(exc), "drive_item_id": candidate.drive_item_id},
                        source_refs=[{"type": "gdrive", "source_ref": candidate.source_ref}],
                    )
                )
        elif candidate.document_kind == "media_asset":
            extraction_status = "skipped_binary"
            summary_text = f"Media asset present: {candidate.title}"
        else:
            summary_text = f"Drive folder present: {candidate.title}"

        return {
            "events": events,
            "document_id": document_id,
            "content_sha": content_sha,
            "blob_path": blob_path,
            "text_content": text_content,
            "summary_text": summary_text,
            "extraction_confidence": extraction_confidence,
            "extraction_status": extraction_status,
            "download_mime_type": download_mime_type,
            "extraction_method": extraction_method,
            "extraction_metadata": extraction_metadata,
            "skip_reason": skip_reason or "",
        }

    def _normalize_candidate_facts(
        self,
        candidate: DriveIngestCandidate,
        *,
        document_id: str,
        text_content: str,
        observed_at: str,
    ) -> dict[str, Any]:
        extracted_facts = extract_drive_facts(
            candidate,
            document_id=document_id,
            text=text_content,
            observed_at=observed_at,
            source_ref=candidate.source_ref,
        )
        link_result = link_drive_candidate(candidate, extracted_facts=extracted_facts, store=self.store)
        case_id = str(link_result.get("case_id") or "").strip()
        case_key = str(link_result.get("case_key") or candidate.probable_case_key or "").strip()
        linkage_status = str(link_result.get("linkage_status") or "unresolved_candidate")
        link_confidence = float(link_result.get("confidence") or 0.0)
        if not case_id and case_key and candidate.scope == "case_specific":
            case_id = stable_id("case", case_key)
            if linkage_status == "unresolved_candidate":
                linkage_status = "deterministic"
                link_confidence = max(link_confidence, 0.97)
                link_result["reasons"] = list(link_result.get("reasons") or []) + ["probable_case_key_seeded_case"]

        case_seed_row = {}
        if case_id:
            case_seed_row = build_drive_case_seed_row(
                existing_case=self.store.fetch_case(case_id) or {},
                case_id=case_id,
                case_key=case_key,
                candidate=candidate,
                facts=extracted_facts,
                observed_at=observed_at,
            )

        fact_rows = [
            build_drive_fact_row(
                item,
                document_id=document_id,
                case_id=case_id,
                probable_case_key=case_key,
                observed_at=observed_at,
            )
            for item in extracted_facts
        ]

        chunk_rows = self._build_drive_chunk_rows(
            case_id=case_id,
            document_id=document_id,
            file_name=candidate.title,
            text=text_content,
            observed_at=observed_at,
        )

        return {
            "extracted_facts": extracted_facts,
            "link_result": link_result,
            "case_id": case_id,
            "case_key": case_key,
            "linkage_status": linkage_status,
            "link_confidence": link_confidence,
            "case_seed_row": case_seed_row,
            "fact_rows": fact_rows,
            "chunk_rows": chunk_rows,
        }

    def _normalize_candidate_events(
        self,
        candidate: DriveIngestCandidate,
        *,
        case_id: str,
        document_id: str,
        linkage_status: str,
        link_result: dict[str, Any],
        extracted_facts: list[dict[str, Any]],
        observed_at: str,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        events.append(
            self._build_event_row(
                case_id=case_id,
                event_type="drive_document_ingested",
                summary_text=f"Drive document ingested: {candidate.title}",
                occurred_at=observed_at,
                payload={
                    "document_id": document_id,
                    "lane": candidate.lane,
                    "document_kind": candidate.document_kind,
                    "scope": candidate.scope,
                    "linkage_status": linkage_status,
                },
                source_refs=[{"type": "gdrive", "source_ref": candidate.source_ref}],
            )
        )
        if link_result.get("case_key") or link_result.get("case_id"):
            events.append(
                self._build_event_row(
                    case_id=case_id,
                    event_type="drive_case_link_candidate",
                    summary_text=f"Drive case link {linkage_status} dla {candidate.title}",
                    occurred_at=observed_at,
                    payload=link_result,
                    source_refs=[{"type": "gdrive", "source_ref": candidate.source_ref}],
                )
            )
        conflicts = detect_drive_conflicts(extracted_facts)
        for conflict in conflicts:
            events.append(
                self._build_event_row(
                    case_id=case_id,
                    event_type="drive_conflict_detected",
                    summary_text=conflict,
                    occurred_at=observed_at,
                    payload={"document_id": document_id, "conflict": conflict},
                    source_refs=[{"type": "gdrive", "source_ref": candidate.source_ref}],
                )
            )
        return events

    def _normalize_candidate(self, candidate: DriveIngestCandidate, *, observed_at: str) -> dict[str, Any]:
        meta = self._normalize_candidate_meta(candidate, observed_at=observed_at)
        facts = self._normalize_candidate_facts(
            candidate,
            document_id=meta["document_id"],
            text_content=meta["text_content"],
            observed_at=observed_at,
        )
        events = meta["events"]
        events.extend(
            self._normalize_candidate_events(
                candidate,
                case_id=facts["case_id"],
                document_id=meta["document_id"],
                linkage_status=facts["linkage_status"],
                link_result=facts["link_result"],
                extracted_facts=facts["extracted_facts"],
                observed_at=observed_at,
            )
        )

        record = DriveDocumentRecord(
            document_id=meta["document_id"],
            drive_item_id=candidate.drive_item_id,
            title=candidate.title,
            mime_type=candidate.mime_type,
            folder_path=candidate.folder_path,
            source_ref=candidate.source_ref,
            lane=candidate.lane,
            document_kind=candidate.document_kind,
            scope=candidate.scope,
            extraction_status=meta["extraction_status"],
            linkage_status=facts["linkage_status"],
            case_id=facts["case_id"],
            probable_case_key=facts["case_key"],
            classification_confidence=float(candidate.classification_confidence or 0.0),
            extraction_confidence=meta["extraction_confidence"],
            link_confidence=facts["link_confidence"],
            download_mime_type=meta["download_mime_type"],
            content_sha256=meta["content_sha"],
            blob_path=meta["blob_path"],
            text_content=meta["text_content"],
            summary_text=meta["summary_text"],
            metadata={
                "parent_drive_item_id": candidate.parent_drive_item_id,
                "is_folder": candidate.is_folder,
                "size_bytes": candidate.size_bytes,
                "modified_time": candidate.modified_time,
                "link_reasons": list(facts["link_result"].get("reasons") or []),
                "matched_facts": list(facts["link_result"].get("matched_facts") or []),
                "extraction_method": meta["extraction_method"],
                "parser_stack": list(build_parse_config_from_settings(self.settings).resolved_chain()),
                "parser_id": str(meta["extraction_metadata"].get("parser_id") or ""),
                "structured_parse": bool(meta["extraction_metadata"].get("structured")),
                "docling_used": str(meta["extraction_method"]).startswith("docling")
                or str(meta["extraction_metadata"].get("parser_id") or "") == "docling",
                "ocr_used": "ocr" in str(meta["extraction_method"]).lower(),
                "page_count": int(meta["extraction_metadata"].get("page_count") or 0),
                "table_count": int(meta["extraction_metadata"].get("table_count") or 0),
                "warnings": list(meta["extraction_metadata"].get("warnings") or []),
                "fallback_reason": ""
                if str(meta["extraction_method"]).startswith("docling") or not self.docling_enabled
                else "; ".join(list(meta["extraction_metadata"].get("warnings") or [])[:2]),
            },
        )
        document_row = build_drive_document_row(record, observed_at=observed_at)

        graph_upsert = self._build_graph_upsert(record.to_dict(), facts=facts["fact_rows"])
        existing_document = self.store.fetch_drive_document_by_item_id(candidate.drive_item_id)
        change_kind = "drive_document_updated" if existing_document else "drive_document_added"
        return {
            "change_kind": change_kind,
            "source_ref": {
                "file_id": candidate.drive_item_id,
                "change_id": candidate.drive_item_id,
                "revision_id": str(candidate.modified_time or candidate.drive_item_id),
                "modified_time": str(candidate.modified_time or observed_at),
                "source_ref": candidate.source_ref,
            },
            "signal_summary_pl": f"Drive dokument {candidate.title} ({candidate.document_kind})",
            "document_row": document_row,
            "chunk_rows": facts["chunk_rows"],
            "fact_rows": facts["fact_rows"],
            "event_rows": events,
            "graph_upsert": {"nodes": list(graph_upsert.nodes), "edges": list(graph_upsert.edges)},
            "case_seed_row": facts["case_seed_row"],
            "case_id": facts["case_id"],
            "case_key": facts["case_key"],
            "linkage_status": facts["linkage_status"],
            "link_reasons": list(facts["link_result"].get("reasons") or []),
            "conflicts": detect_drive_conflicts(facts["extracted_facts"]),
        }

    def _process_candidate(self, candidate: DriveIngestCandidate, *, observed_at: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None, DriveGraphUpsert | None]:
        normalized = self._normalize_candidate(candidate, observed_at=observed_at)
        return self._persist_normalized_candidate(normalized)

    def _persist_normalized_candidate(
        self,
        normalized: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None, DriveGraphUpsert | None]:
        case_seed_row = dict(normalized.get("case_seed_row") or {})
        document_row = dict(normalized.get("document_row") or {})
        chunk_rows = [dict(item) for item in (normalized.get("chunk_rows") or [])]
        drive_document_fact_rows = [dict(item) for item in (normalized.get("fact_rows") or [])]
        event_rows = [dict(item) for item in (normalized.get("event_rows") or [])]
        graph_payload = dict(normalized.get("graph_upsert") or {})
        graph_upsert = DriveGraphUpsert(
            nodes=list(graph_payload.get("nodes") or []),
            edges=list(graph_payload.get("edges") or []),
        )

        if case_seed_row:
            enriched, routing = enrich_case_row_before_upsert(case_seed_row, source_kind="drive")
            if routing.upsert_allowed:
                self.store.upsert_case(enriched)
        if document_row:
            self.store.upsert_drive_document(document_row)
            try:
                from document_intelligence_runtime import (
                    build_document_intelligence_result,
                    promote_document_intelligence_facts,
                )

                docintel = build_document_intelligence_result(
                    source_type="drive_file",
                    source_id=str(document_row.get("drive_item_id") or document_row.get("document_id") or ""),
                    case_id=str(document_row.get("case_id") or ""),
                    filename=str(document_row.get("file_name") or ""),
                    mime_type=str(document_row.get("mime_type") or ""),
                    text=str(document_row.get("text_content") or ""),
                    parser=str(document_row.get("parser_name") or document_row.get("extraction_status") or "fallback"),
                    parser_confidence=float(document_row.get("extraction_confidence") or 0.0),
                )
                docintel_row = docintel.to_dict()
                self.store.upsert_document_intelligence_result(docintel_row)
                if document_intelligence_promote_facts_enabled():
                    # 4.2: document fields → mailbox_memory_facts via supersession (not raw insert).
                    promote_document_intelligence_facts(self.store, docintel_row)
            except Exception as exc:  # noqa: BLE001
                event_rows.append(
                    {
                        "event_id": stable_id("ev", str(document_row.get("document_id") or ""), "document_intelligence_failed"),
                        "case_id": str(document_row.get("case_id") or ""),
                        "message_id": "",
                        "thread_id": "",
                        "event_type": "document_intelligence_failed",
                        "occurred_at": str(document_row.get("updated_at") or document_row.get("created_at") or ""),
                        "summary_text": "Drive document intelligence failed.",
                        "payload": {"document_id": document_row.get("document_id"), "error": str(exc)[:500]},
                        "source_refs": [{"type": "drive_document", "document_id": document_row.get("document_id")}],
                    }
                )
            self.store.replace_drive_document_chunks(
                document_id=str(document_row.get("document_id") or ""),
                rows=chunk_rows,
            )
            self.store.replace_drive_document_facts(
                document_id=str(document_row.get("document_id") or ""),
                rows=drive_document_fact_rows,
            )
        for event_row in event_rows:
            self.store.append_event(event_row)
        if self.graph_store is not None and (graph_upsert.nodes or graph_upsert.edges):
            self.graph_store.upsert_many(nodes=graph_upsert.nodes, edges=graph_upsert.edges)
        return event_rows, document_row or None, graph_upsert

    def _build_drive_chunk_rows(
        self,
        *,
        case_id: str,
        document_id: str,
        file_name: str,
        text: str,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        rows = build_document_chunks(
            case_id=case_id,
            document_id=document_id,
            file_name=file_name,
            text=text,
            created_at=observed_at,
            source_type="drive_document_chunk",
            updated_at=observed_at,
        )
        if not rows:
            return []
        return apply_embeddings_to_chunk_rows(
            rows,
            vector_enabled=bool(self.vector_enabled),
            embedding_model=str(self.embedding_model or ""),
            embedding_runtime=self.embedding_runtime,
            updated_at=str(observed_at),
            created_at_fallback=str(observed_at),
        )

    def bounded_refresh_document_intelligence_for_cases(self, case_ids: set[str], *, observed_at: str) -> None:
        """Bounded re-embed of stored Drive chunk rows for affected cases (no re-download)."""
        max_cases = int(os.getenv("DRIVE_BOUNDED_REFRESH_MAX_CASES", "5"))
        max_docs = int(os.getenv("DRIVE_BOUNDED_REFRESH_MAX_DOCS", "4"))
        max_chunks = int(os.getenv("DRIVE_BOUNDED_REFRESH_MAX_CHUNKS", "16"))
        for case_id in list(case_ids)[:max_cases]:
            cid = str(case_id or "").strip()
            if not cid:
                continue
            drive_docs = self.store.fetch_drive_documents_for_case(cid, limit=max_docs)
            for doc in drive_docs:
                doc_id = str(doc.get("document_id") or "").strip()
                if not doc_id:
                    continue
                pool = [
                    dict(c)
                    for c in (self.store.fetch_drive_chunks_for_case(cid, limit=400) or [])
                    if str(c.get("document_id") or "") == doc_id
                ]
                if not pool:
                    continue
                pool.sort(key=lambda row: int(row.get("ordinal") or 0))
                rows = pool[:max_chunks]
                apply_embeddings_to_chunk_rows(
                    rows,
                    vector_enabled=bool(self.vector_enabled),
                    embedding_model=str(self.embedding_model or ""),
                    embedding_runtime=self.embedding_runtime,
                    updated_at=str(observed_at),
                    created_at_fallback=str(rows[0].get("created_at") or observed_at),
                )
                self.store.replace_drive_document_chunks(document_id=doc_id, rows=rows)

    def _build_graph_nodes(
        self, record: dict[str, Any], *, facts: list[dict[str, Any]], observed_at: str, source_ref: str
    ) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        document_node = build_graph_node(
            node_type="Document",
            natural_key=str(record.get("document_id") or ""),
            title=str(record.get("title") or record.get("file_name") or ""),
            source="gdrive",
            source_ref=source_ref,
            confidence=float(record.get("classification_confidence") or 0.0),
            payload={"document_kind": record.get("document_kind"), "lane": record.get("lane"), "scope": record.get("scope")},
            observed_at=observed_at,
        )
        nodes.append(document_node)

        case_id = str(record.get("case_id") or "").strip()
        kind_node = None
        kind_node_type = KIND_TO_NODE_TYPE.get(str(record.get("document_kind") or ""), "Document")
        if kind_node_type != "Document":
            kind_node = build_graph_node(
                node_type=kind_node_type,
                natural_key=str(record.get("document_id") or ""),
                title=str(record.get("title") or record.get("file_name") or ""),
                source="gdrive",
                source_ref=source_ref,
                confidence=float(record.get("classification_confidence") or 0.0),
                payload={"document_id": record.get("document_id"), "document_kind": record.get("document_kind")},
                observed_at=observed_at,
            )
            nodes.append(kind_node)

        case_node = None
        if case_id:
            case_node = build_graph_node(
                node_type="Case",
                natural_key=case_id,
                title=str(record.get("probable_case_key") or case_id),
                source="gdrive",
                source_ref=source_ref,
                confidence=float(record.get("link_confidence") or 0.0),
                payload={"case_key": record.get("probable_case_key")},
                observed_at=observed_at,
            )
            nodes.append(case_node)

        manufacturer = infer_manufacturer(str(record.get("title") or "") + " " + str(record.get("summary_text") or ""))
        manufacturer_node = None
        if manufacturer:
            manufacturer_node = build_graph_node(
                node_type="Manufacturer",
                natural_key=manufacturer.lower(),
                title=manufacturer,
                source="gdrive",
                source_ref=source_ref,
                confidence=0.82,
                payload={},
                observed_at=observed_at,
            )
            nodes.append(manufacturer_node)

        offer_family_value = first_fact_value(facts, "offer_family")
        offer_family_node = None
        if offer_family_value:
            offer_family_node = build_graph_node(
                node_type="OfferFamily",
                natural_key=offer_family_value.lower(),
                title=offer_family_value,
                source="gdrive",
                source_ref=source_ref,
                confidence=0.86,
                payload={},
                observed_at=observed_at,
            )
            nodes.append(offer_family_node)

        model_nodes: list[dict[str, Any]] = []
        for model_value in collect_fact_values(facts, {"device_model", "device_model_bundle", "model_bundle"}):
            model_node = build_graph_node(
                node_type="Model",
                natural_key=model_value.lower(),
                title=model_value,
                source="gdrive",
                source_ref=source_ref,
                confidence=0.88,
                payload={},
                observed_at=observed_at,
            )
            model_nodes.append(model_node)
            nodes.append(model_node)

        customer_name = first_fact_value(facts, "customer_name") or first_fact_value(facts, "buyer_name")
        customer_node = None
        if case_id and customer_name:
            customer_node = build_graph_node(
                node_type="Customer",
                natural_key=customer_name.lower(),
                title=customer_name,
                source="gdrive",
                source_ref=source_ref,
                confidence=0.76,
                payload={},
                observed_at=observed_at,
            )
            nodes.append(customer_node)

        location_value = (
            first_fact_value(facts, "installation_address")
            or first_fact_value(facts, "investment_address")
            or first_fact_value(facts, "city")
        )
        location_node = None
        if case_id and location_value:
            location_node = build_graph_node(
                node_type="Location",
                natural_key=location_value.lower(),
                title=location_value,
                source="gdrive",
                source_ref=source_ref,
                confidence=0.76,
                payload={},
                observed_at=observed_at,
            )
            nodes.append(location_node)

        return {
            "nodes": nodes,
            "document_node": document_node,
            "kind_node": kind_node,
            "case_node": case_node,
            "manufacturer_node": manufacturer_node,
            "offer_family_node": offer_family_node,
            "model_nodes": model_nodes,
            "customer_node": customer_node,
            "location_node": location_node,
            "case_id": case_id,
        }

    def _build_graph_edges(
        self,
        record: dict[str, Any],
        *,
        facts: list[dict[str, Any]],
        node_map: dict[str, Any],
        observed_at: str,
        source_ref: str,
    ) -> list[dict[str, Any]]:
        edges: list[dict[str, Any]] = []
        document_node = node_map["document_node"]
        kind_node = node_map["kind_node"]
        case_node = node_map["case_node"]
        case_id = node_map["case_id"]

        if case_id and case_node:
            edges.append(
                build_graph_edge(
                    src_node_id=case_node["node_id"],
                    dst_node_id=document_node["node_id"],
                    relation_type="case_has_document",
                    source="gdrive",
                    source_ref=source_ref,
                    confidence=float(record.get("link_confidence") or 0.0),
                    metadata={"document_kind": record.get("document_kind")},
                    observed_at=observed_at,
                )
            )
            relation_type = RELATION_BY_KIND.get(str(record.get("document_kind") or ""))
            if relation_type and kind_node is not None:
                edges.append(
                    build_graph_edge(
                        src_node_id=case_node["node_id"],
                        dst_node_id=kind_node["node_id"],
                        relation_type=relation_type,
                        source="gdrive",
                        source_ref=source_ref,
                        confidence=float(record.get("link_confidence") or 0.0),
                        metadata={},
                        observed_at=observed_at,
                    )
                )

        if node_map["offer_family_node"] and case_id:
            edges.append(
                build_graph_edge(
                    src_node_id=stable_graph_node_id("Case", case_id),
                    dst_node_id=node_map["offer_family_node"]["node_id"],
                    relation_type="case_uses_offer_family",
                    source="gdrive",
                    source_ref=source_ref,
                    confidence=0.8,
                    metadata={},
                    observed_at=observed_at,
                )
            )

        for model_node in node_map["model_nodes"]:
            edges.append(
                build_graph_edge(
                    src_node_id=document_node["node_id"],
                    dst_node_id=model_node["node_id"],
                    relation_type="document_mentions_model",
                    source="gdrive",
                    source_ref=source_ref,
                    confidence=0.86,
                    metadata={"fact_source": "drive_fact"},
                    observed_at=observed_at,
                )
            )
            if node_map["manufacturer_node"] is not None:
                edges.append(
                    build_graph_edge(
                        src_node_id=model_node["node_id"],
                        dst_node_id=node_map["manufacturer_node"]["node_id"],
                        relation_type="model_belongs_to_manufacturer",
                        source="gdrive",
                        source_ref=source_ref,
                        confidence=0.82,
                        metadata={},
                        observed_at=observed_at,
                    )
                )
            if node_map["offer_family_node"] is not None:
                edges.append(
                    build_graph_edge(
                        src_node_id=node_map["offer_family_node"]["node_id"],
                        dst_node_id=model_node["node_id"],
                        relation_type="offer_family_uses_model",
                        source="gdrive",
                        source_ref=source_ref,
                        confidence=0.78,
                        metadata={},
                        observed_at=observed_at,
                    )
                )

        if node_map["customer_node"] and case_id:
            edges.append(
                build_graph_edge(
                    src_node_id=stable_graph_node_id("Case", case_id),
                    dst_node_id=node_map["customer_node"]["node_id"],
                    relation_type="case_has_customer",
                    source="gdrive",
                    source_ref=source_ref,
                    confidence=0.76,
                    metadata={},
                    observed_at=observed_at,
                )
            )

        if node_map["location_node"] and case_id:
            edges.append(
                build_graph_edge(
                    src_node_id=stable_graph_node_id("Case", case_id),
                    dst_node_id=node_map["location_node"]["node_id"],
                    relation_type="case_has_location",
                    source="gdrive",
                    source_ref=source_ref,
                    confidence=0.76,
                    metadata={},
                    observed_at=observed_at,
                )
            )

        return edges

    def _build_graph_relationship(
        self,
        record: dict[str, Any],
        *,
        node_map: dict[str, Any],
        observed_at: str,
        source_ref: str,
    ) -> list[dict[str, Any]]:
        edges: list[dict[str, Any]] = []
        kind_node = node_map["kind_node"]
        document_node = node_map["document_node"]
        relation_source_node_id = kind_node["node_id"] if kind_node is not None else document_node["node_id"]
        case_id = node_map["case_id"]

        if str(record.get("document_kind") or "") == "media_asset":
            parent_bundle_key = str((record.get("metadata") or {}).get("parent_drive_item_id") or "")
            if parent_bundle_key:
                bundle_node = build_graph_node(
                    node_type="MediaBundle",
                    natural_key=parent_bundle_key,
                    title=str(record.get("folder_path") or "Media bundle"),
                    source="gdrive",
                    source_ref=source_ref,
                    confidence=0.7,
                    payload={},
                    observed_at=observed_at,
                )
                asset_node = build_graph_node(
                    node_type="MediaAsset",
                    natural_key=str(record.get("document_id") or ""),
                    title=str(record.get("title") or record.get("file_name") or ""),
                    source="gdrive",
                    source_ref=source_ref,
                    confidence=0.75,
                    payload={},
                    observed_at=observed_at,
                )
                edges.append(
                    build_graph_edge(
                        src_node_id=bundle_node["node_id"],
                        dst_node_id=asset_node["node_id"],
                        relation_type="media_bundle_has_asset",
                        source="gdrive",
                        source_ref=source_ref,
                        confidence=0.75,
                        metadata={},
                        observed_at=observed_at,
                    )
                )
                if case_id:
                    edges.append(
                        build_graph_edge(
                            src_node_id=stable_graph_node_id("Case", case_id),
                            dst_node_id=bundle_node["node_id"],
                            relation_type="case_has_media_bundle",
                            source="gdrive",
                            source_ref=source_ref,
                            confidence=float(record.get("link_confidence") or 0.0),
                            metadata={"asset_document_id": record.get("document_id")},
                            observed_at=observed_at,
                        )
                    )

        for model_node in node_map["model_nodes"]:
            doc_kind = str(record.get("document_kind") or "")
            if doc_kind == "price_list":
                edges.append(
                    build_graph_edge(
                        src_node_id=relation_source_node_id,
                        dst_node_id=model_node["node_id"],
                        relation_type="price_list_prices_model",
                        source="gdrive",
                        source_ref=source_ref,
                        confidence=0.84,
                        metadata={},
                        observed_at=observed_at,
                    )
                )
            elif doc_kind == "pricing_workbook":
                edges.append(
                    build_graph_edge(
                        src_node_id=relation_source_node_id,
                        dst_node_id=model_node["node_id"],
                        relation_type="workbook_contains_cost_for_model",
                        source="gdrive",
                        source_ref=source_ref,
                        confidence=0.84,
                        metadata={},
                        observed_at=observed_at,
                    )
                )
            elif doc_kind == "technical_reference":
                edges.append(
                    build_graph_edge(
                        src_node_id=relation_source_node_id,
                        dst_node_id=model_node["node_id"],
                        relation_type="technical_reference_supports_model_family",
                        source="gdrive",
                        source_ref=source_ref,
                        confidence=0.76,
                        metadata={},
                        observed_at=observed_at,
                    )
                )

        return edges

    def _build_graph_upsert(self, record: dict[str, Any], *, facts: list[dict[str, Any]]) -> DriveGraphUpsert:
        observed_at = str((record.get("metadata") or {}).get("modified_time") or datetime.now().astimezone().isoformat())
        source_ref = str(record.get("source_ref") or "")

        node_map = self._build_graph_nodes(record, facts=facts, observed_at=observed_at, source_ref=source_ref)
        edges = self._build_graph_edges(record, facts=facts, node_map=node_map, observed_at=observed_at, source_ref=source_ref)
        relationship_edges = self._build_graph_relationship(record, node_map=node_map, observed_at=observed_at, source_ref=source_ref)
        edges.extend(relationship_edges)

        nodes = node_map["nodes"]
        dedup_nodes = {node["node_id"]: node for node in nodes}
        dedup_edges = {edge["edge_id"]: edge for edge in edges}
        return DriveGraphUpsert(nodes=list(dedup_nodes.values()), edges=list(dedup_edges.values()))

    def _upsert_drive_case_seed(
        self,
        *,
        case_id: str,
        case_key: str,
        candidate: DriveIngestCandidate,
        facts: list[dict[str, Any]],
        observed_at: str,
    ) -> None:
        seed_row = build_drive_case_seed_row(
            existing_case=self.store.fetch_case(case_id) or {},
            case_id=case_id,
            case_key=case_key,
            candidate=candidate,
            facts=facts,
            observed_at=observed_at,
        )
        enriched, routing = enrich_case_row_before_upsert(seed_row, source_kind="drive")
        if routing.upsert_allowed:
            self.store.upsert_case(enriched)


def build_drive_case_seed_row(
    *,
    existing_case: dict[str, Any],
    case_id: str,
    case_key: str,
    candidate: DriveIngestCandidate,
    facts: list[dict[str, Any]],
    observed_at: str,
) -> dict[str, Any]:
    customer_name = first_fact_value(facts, "customer_name") or first_fact_value(facts, "buyer_name")
    customer_email = first_fact_value(facts, "customer_email")
    address = first_fact_value(facts, "installation_address") or first_fact_value(facts, "investment_address")
    model_bundle = first_fact_value(facts, "device_model_bundle") or first_fact_value(facts, "model_bundle")
    existing_metadata = dict(existing_case.get("metadata") or {})
    return {
        "case_id": case_id,
        "case_key": case_key,
        "thread_id": str(existing_case.get("thread_id") or ""),
        "case_family": str(existing_case.get("case_family") or "unknown"),
        "mailbox": str(existing_case.get("mailbox") or "drive"),
        "subject": str(existing_case.get("subject") or candidate.title),
        "status": str(existing_case.get("status") or "open"),
        "customer_name": str(existing_case.get("customer_name") or "") or customer_name,
        "customer_email": str(existing_case.get("customer_email") or "") or customer_email,
        "metadata": {
            **existing_metadata,
            "source": "gdrive",
            "installation_address": address or str(existing_metadata.get("installation_address") or ""),
            "model_bundle": model_bundle or str(existing_metadata.get("model_bundle") or ""),
        },
        "created_at": str(existing_case.get("created_at") or observed_at),
        "updated_at": observed_at,
    }


def _drive_title_blob(candidate: DriveIngestCandidate) -> str:
    return " ".join(
        part
        for part in (str(candidate.title or ""), str(candidate.folder_path or ""))
        if part
    ).strip().lower()


def drive_skip_reason(candidate: DriveIngestCandidate, *, max_download_bytes: int) -> str:
    """Operator policy: skip heavy scans and technical project PDFs/DOCs without download."""
    if candidate.is_folder or candidate.document_kind == "media_asset":
        return ""
    title_blob = _drive_title_blob(candidate)
    if any(marker in title_blob for marker in SKIP_TITLE_SUBSTRINGS):
        return "technical_project_document"
    size_bytes = int(candidate.size_bytes or 0)
    if size_bytes > int(max_download_bytes):
        return f"file_too_large:{size_bytes}>{int(max_download_bytes)}"
    return ""


def map_extraction_status(*, extraction_status: str, title: str, extracted_text: str) -> str:
    normalized = str(extraction_status or "").strip().lower()
    title_key = str(title or "").strip().lower()
    has_text = bool(str(extracted_text or "").strip())
    if title_key in BLOCKED_TITLES:
        return "blocked"
    if normalized in {"ok", "archive_container", "tesseract_ocr", "xls_binary_strings"}:
        return "extracted" if has_text else "empty"
    if normalized in {"empty", "ocr_empty", "skipped_empty"}:
        return "empty"
    if normalized in {"unsupported_mime", "ocr_deps_missing", "ocr_binary_missing", "xls_binary_no_strings"}:
        return "blocked"
    if normalized in {"failed", "ocr_failed"}:
        return "failed"
    if normalized:
        return "extracted" if has_text else "blocked"
    return "empty" if not has_text else "extracted"


def _build_drive_fact_row(
    document_id: str,
    fact_family: str,
    fact_key: str,
    normalized_value: str,
    raw_value: str,
    confidence: float,
    observed_at: str,
    source_ref: str,
    lane: str,
    document_kind: str,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"lane": lane, "document_kind": document_kind}
    if extra_metadata:
        metadata.update(extra_metadata)
    return build_fact(
        document_id=document_id,
        fact_family=fact_family,
        fact_key=fact_key,
        normalized_value=normalized_value,
        raw_value=raw_value,
        confidence=confidence,
        observed_at=observed_at,
        source_ref=source_ref,
        metadata=metadata,
    )


def _extract_drive_fact_keys(
    candidate: DriveIngestCandidate,
    *,
    document_id: str,
    combined_text: str,
    lowered: str,
    observed_at: str,
    source_ref: str,
    lane: str,
    document_kind: str,
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []

    probable_case_key = str(candidate.probable_case_key or "").strip()
    if probable_case_key:
        facts.append(
            _build_drive_fact_row(
                document_id=document_id, fact_family="linkage", fact_key="probable_case_key",
                normalized_value=probable_case_key, raw_value=probable_case_key,
                confidence=0.98, observed_at=observed_at, source_ref=source_ref,
                lane=lane, document_kind=document_kind,
            )
        )

    for match in ORDER_RE.finditer(combined_text):
        facts.append(
            _build_drive_fact_row(
                document_id=document_id, fact_family="transaction", fact_key="order_number",
                normalized_value=clean_identifier(match.group(0)), raw_value=match.group(0),
                confidence=0.95, observed_at=observed_at, source_ref=source_ref,
                lane=lane, document_kind=document_kind,
            )
        )

    for match in INVOICE_RE.finditer(combined_text):
        facts.append(
            _build_drive_fact_row(
                document_id=document_id, fact_family="transaction", fact_key="invoice_number",
                normalized_value=clean_identifier(match.group(0)), raw_value=match.group(0),
                confidence=0.95, observed_at=observed_at, source_ref=source_ref,
                lane=lane, document_kind=document_kind,
            )
        )

    for match in DEPOSIT_RE.finditer(combined_text):
        facts.append(
            _build_drive_fact_row(
                document_id=document_id, fact_family="transaction", fact_key="deposit_invoice_number",
                normalized_value=clean_identifier(match.group(0)), raw_value=match.group(0),
                confidence=0.95, observed_at=observed_at, source_ref=source_ref,
                lane=lane, document_kind=document_kind,
            )
        )

    for match in MODEL_RE.finditer(combined_text.upper()):
        facts.append(
            _build_drive_fact_row(
                document_id=document_id, fact_family="technical_reference", fact_key="device_model",
                normalized_value=clean_identifier(match.group(0)), raw_value=match.group(0),
                confidence=0.92, observed_at=observed_at, source_ref=source_ref,
                lane=lane, document_kind=document_kind,
            )
        )

    manufacturer = infer_manufacturer(combined_text)
    if manufacturer:
        facts.append(
            _build_drive_fact_row(
                document_id=document_id, fact_family="technical_reference", fact_key="manufacturer",
                normalized_value=manufacturer, raw_value=manufacturer,
                confidence=0.85, observed_at=observed_at, source_ref=source_ref,
                lane=lane, document_kind=document_kind,
            )
        )

    offer_family = infer_offer_family(combined_text)
    if offer_family:
        facts.append(
            _build_drive_fact_row(
                document_id=document_id, fact_family="offer_family", fact_key="offer_family",
                normalized_value=offer_family, raw_value=offer_family,
                confidence=0.8, observed_at=observed_at, source_ref=source_ref,
                lane=lane, document_kind=document_kind,
            )
        )

    customer_name = infer_customer_name(combined_text)
    if customer_name:
        facts.append(
            _build_drive_fact_row(
                document_id=document_id, fact_family="contract", fact_key="customer_name",
                normalized_value=customer_name, raw_value=customer_name,
                confidence=0.72, observed_at=observed_at, source_ref=source_ref,
                lane=lane, document_kind=document_kind,
            )
        )

    email_match = re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", combined_text, re.IGNORECASE)
    if email_match:
        facts.append(
            _build_drive_fact_row(
                document_id=document_id, fact_family="contract", fact_key="customer_email",
                normalized_value=email_match.group(0).lower(), raw_value=email_match.group(0),
                confidence=0.88, observed_at=observed_at, source_ref=source_ref,
                lane=lane, document_kind=document_kind,
            )
        )

    address_match = ADDRESS_RE.search(combined_text)
    if address_match:
        address_value = re.sub(r"\s+", " ", address_match.group(0)).strip()
        facts.append(
            _build_drive_fact_row(
                document_id=document_id, fact_family="contract", fact_key="installation_address",
                normalized_value=address_value, raw_value=address_value,
                confidence=0.7, observed_at=observed_at, source_ref=source_ref,
                lane=lane, document_kind=document_kind,
            )
        )

    city_match = re.search(
        r"\b(?:Jaworzno|Sosnowiec|Chorzow|Olkusz|Regulice|Siedlec|Psary|Gleboka|Zubadan|Panasia)\b",
        combined_text, re.IGNORECASE,
    )
    if city_match:
        city_value = city_match.group(0).strip().title()
        facts.append(
            _build_drive_fact_row(
                document_id=document_id, fact_family="contract", fact_key="city",
                normalized_value=city_value, raw_value=city_value,
                confidence=0.68, observed_at=observed_at, source_ref=source_ref,
                lane=lane, document_kind=document_kind,
            )
        )

    for match in DATE_RE.finditer(combined_text):
        normalized_date = normalize_date(match.group(1))
        if normalized_date:
            facts.append(
                _build_drive_fact_row(
                    document_id=document_id, fact_family="document", fact_key="document_date",
                    normalized_value=normalized_date, raw_value=match.group(1),
                    confidence=0.7, observed_at=observed_at, source_ref=source_ref,
                    lane=lane, document_kind=document_kind,
                )
            )
            break

    warranty_match = WARRANTY_TERM_RE.search(lowered)
    if warranty_match:
        raw_val = warranty_match.group(0)
        facts.append(
            _build_drive_fact_row(
                document_id=document_id, fact_family="warranty", fact_key="warranty_term",
                normalized_value=clean_identifier(raw_val), raw_value=raw_val,
                confidence=0.86, observed_at=observed_at, source_ref=source_ref,
                lane=lane, document_kind=document_kind,
            )
        )

    service_match = SERVICE_FREQ_RE.search(lowered)
    if service_match:
        raw_val = service_match.group(0)
        facts.append(
            _build_drive_fact_row(
                document_id=document_id, fact_family="warranty", fact_key="service_frequency",
                normalized_value=clean_identifier(raw_val), raw_value=raw_val,
                confidence=0.82, observed_at=observed_at, source_ref=source_ref,
                lane=lane, document_kind=document_kind,
            )
        )

    serial_match = SERIAL_RE.search(combined_text)
    if serial_match:
        facts.append(
            _build_drive_fact_row(
                document_id=document_id, fact_family="technical_reference", fact_key="serial_number",
                normalized_value=clean_identifier(serial_match.group(1)), raw_value=serial_match.group(1),
                confidence=0.92, observed_at=observed_at, source_ref=source_ref,
                lane=lane, document_kind=document_kind,
            )
        )

    return facts

def _extract_drive_fact_values(
    candidate: DriveIngestCandidate,
    *,
    document_id: str,
    combined_text: str,
    observed_at: str,
    source_ref: str,
    lane: str,
    document_kind: str,
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []

    if document_kind in {"price_list", "pricing_workbook"} or lane == "commercial_pricing":
        facts.extend(
            extract_pricing_facts(
                candidate,
                document_id=document_id,
                combined_text=combined_text,
                observed_at=observed_at,
                source_ref=source_ref,
            )
        )

    return facts

def extract_drive_facts(
    candidate: DriveIngestCandidate,
    *,
    document_id: str,
    text: str,
    observed_at: str,
    source_ref: str,
) -> list[dict[str, Any]]:
    combined_text = "\n".join(part for part in (candidate.title, candidate.folder_path, text) if part)
    lowered = combined_text.lower()
    lane = candidate.lane
    document_kind = candidate.document_kind

    facts: list[dict[str, Any]] = []

    # Key-based regex fact extractions
    facts.extend(
        _extract_drive_fact_keys(
            candidate=candidate,
            document_id=document_id,
            combined_text=combined_text,
            lowered=lowered,
            observed_at=observed_at,
            source_ref=source_ref,
            lane=lane,
            document_kind=document_kind,
        )
    )

    # Build model bundle from individual device_model values
    model_values = [f["normalized_value"] for f in facts if f.get("fact_key") in {"device_model", "device_model_bundle", "model_bundle"}]
    model_values = [v for v in model_values if v]
    unique_models = sorted(set(model_values))
    if unique_models:
        facts.append(
            _build_drive_fact_row(
                document_id=document_id, fact_family="technical_reference", fact_key="device_model_bundle",
                normalized_value=" | ".join(unique_models), raw_value=", ".join(unique_models),
                confidence=0.84, observed_at=observed_at, source_ref=source_ref,
                lane=lane, document_kind=document_kind,
                extra_metadata={"count": len(unique_models)},
            )
        )

    # Value-based extractions (pricing)
    facts.extend(
        _extract_drive_fact_values(
            candidate=candidate,
            document_id=document_id,
            combined_text=combined_text,
            observed_at=observed_at,
            source_ref=source_ref,
            lane=lane,
            document_kind=document_kind,
        )
    )

    return dedupe_facts(facts)


def extract_pricing_facts(
    candidate: DriveIngestCandidate,
    *,
    document_id: str,
    combined_text: str,
    observed_at: str,
    source_ref: str,
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    money_matches = [match.group("value") for match in MONEY_RE.finditer(combined_text)]
    for raw_value in money_matches[:12]:
        normalized = normalize_money(raw_value)
        if not normalized:
            continue
        facts.append(
            build_fact(
                document_id=document_id,
                fact_family="pricing",
                fact_key="price_amount",
                normalized_value=normalized,
                raw_value=raw_value,
                confidence=0.82,
                observed_at=observed_at,
                source_ref=source_ref,
                metadata={"lane": candidate.lane, "document_kind": candidate.document_kind},
            )
        )
    if money_matches:
        facts.append(
            build_fact(
                document_id=document_id,
                fact_family="pricing",
                fact_key="price_fact_present",
                normalized_value="yes",
                raw_value=str(len(money_matches)),
                confidence=0.95,
                observed_at=observed_at,
                source_ref=source_ref,
                metadata={"lane": candidate.lane, "document_kind": candidate.document_kind, "count": len(money_matches)},
            )
        )
    return facts


def build_fact(
    *,
    document_id: str,
    fact_family: str,
    fact_key: str,
    normalized_value: str,
    raw_value: str,
    confidence: float,
    observed_at: str,
    source_ref: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "fact_id": stable_id("gfact", document_id, fact_key, normalized_value),
        "fact_family": fact_family,
        "entity_scope": "document",
        "fact_key": fact_key,
        "normalized_value": normalized_value,
        "raw_value": raw_value,
        "confidence": round(float(confidence), 4),
        "observed_at": observed_at,
        "source_ref": source_ref,
        "status": "active",
        "metadata": dict(metadata or {}),
    }


def dedupe_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for fact in facts:
        key = (str(fact.get("fact_key") or ""), str(fact.get("normalized_value") or ""))
        current = deduped.get(key)
        if current is None or float(fact.get("confidence") or 0.0) > float(current.get("confidence") or 0.0):
            deduped[key] = fact
    return list(deduped.values())


def detect_drive_conflicts(extracted_facts: list[dict[str, Any]]) -> list[str]:
    allowed_multi = {"device_model", "serial_number", "price_amount", "reference_token", "device_model_bundle"}
    grouped: dict[str, set[str]] = {}
    for fact in extracted_facts:
        fact_key = str(fact.get("fact_key") or "")
        value = str(fact.get("normalized_value") or "").strip()
        if not fact_key or not value or fact_key in allowed_multi:
            continue
        grouped.setdefault(fact_key, set()).add(value)
    conflicts = []
    for fact_key, values in grouped.items():
        if len(values) > 1:
            conflicts.append(f"Drive fact conflict for {fact_key}: {', '.join(sorted(values))}.")
    return conflicts


def infer_offer_family(text: str) -> str:
    lowered = str(text or "").lower()
    if any(token in lowered for token in {"pompa ciepla", "pompy ciepla", "heat pump", "aquarea"}):
        return "heat_pump"
    if any(token in lowered for token in {"klimatyz", "air conditioner", "split"}):
        return "air_conditioning"
    if any(token in lowered for token in {"rekuper", "wentylac"}):
        return "ventilation"
    if any(token in lowered for token in {"serwis", "przeglad", "maintenance"}):
        return "service"
    if any(token in lowered for token in {"cennik", "pricing", "price list"}):
        return "pricing_reference"
    return ""


def infer_manufacturer(text: str) -> str:
    lowered = str(text or "").lower()
    for name in MANUFACTURERS:
        if name.lower() in lowered:
            return name
    return ""


def infer_customer_name(text: str) -> str:
    for pattern in (
        r"(?:Kupujacy|Nabywca|Inwestor|Zamawiajacy|Klient)\s*[:\-]\s*([A-Z][A-Za-z -]{4,80})",
    ):
        match = re.search(pattern, str(text or ""), re.IGNORECASE)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" ,.;:-")
            if len(value.split()) >= 2:
                return value
    return ""


def normalize_money(value: str) -> str:
    raw = str(value or "").strip().replace(" ", "")
    if not raw:
        return ""
    normalized = raw.replace(",", ".")
    try:
        amount = float(normalized)
    except ValueError:
        return ""
    return f"{amount:.2f} PLN"


def normalize_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for separator in (".", "/", "-"):
        if separator in text:
            parts = [part.strip() for part in text.split(separator)]
            if len(parts) != 3 or not all(part.isdigit() for part in parts):
                return ""
            day, month, year = parts
            if len(year) == 2:
                year = f"20{year}"
            return f"{year.zfill(4)}-{month.zfill(2)}-{day.zfill(2)}"
    return ""


def clean_identifier(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).upper()


def looks_numeric(value: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?", str(value or "").strip()))


def first_fact_value(facts: list[dict[str, Any]], fact_key: str) -> str:
    for fact in facts:
        if not is_live_fact(fact):
            continue
        if str(fact.get("fact_key") or "") == fact_key:
            value = str(fact.get("normalized_value") or "").strip()
            if value:
                return value
    return ""


def collect_fact_values(facts: list[dict[str, Any]], fact_keys: set[str]) -> list[str]:
    values = []
    seen = set()
    for fact in facts:
        if not is_live_fact(fact):
            continue
        fact_key = str(fact.get("fact_key") or "")
        value = str(fact.get("normalized_value") or "").strip()
        if fact_key in fact_keys and value and value not in seen:
            seen.add(value)
            values.append(value)
    return values


def build_drive_document_row(record: DriveDocumentRecord, *, observed_at: str) -> dict[str, Any]:
    payload = record.to_dict()
    return {
        "document_id": payload["document_id"],
        "drive_item_id": payload["drive_item_id"],
        "parent_drive_item_id": str(payload.get("metadata", {}).get("parent_drive_item_id") or ""),
        "parent_document_id": str(payload.get("metadata", {}).get("parent_document_id") or ""),
        "case_id": payload["case_id"],
        "probable_case_key": payload["probable_case_key"],
        "file_name": payload["title"],
        "mime_type": payload["mime_type"],
        "folder_path": payload["folder_path"],
        "lane": payload["lane"],
        "document_kind": payload["document_kind"],
        "scope": payload["scope"],
        "source_ref": payload["source_ref"],
        "extraction_status": payload["extraction_status"],
        "linkage_status": payload["linkage_status"],
        "classification_confidence": payload["classification_confidence"],
        "extraction_confidence": payload["extraction_confidence"],
        "link_confidence": payload["link_confidence"],
        "download_mime_type": payload["download_mime_type"],
        "content_sha256": payload["content_sha256"],
        "blob_path": payload["blob_path"],
        "text_content": payload["text_content"],
        "summary_text": payload["summary_text"],
        "metadata": payload["metadata"],
        "created_at": observed_at,
        "updated_at": observed_at,
    }


def build_drive_fact_row(fact: dict[str, Any], *, document_id: str, case_id: str, probable_case_key: str, observed_at: str) -> dict[str, Any]:
    return {
        "fact_id": str(fact.get("fact_id") or stable_id("gfact", document_id, fact.get("fact_key"), fact.get("normalized_value"))),
        "drive_document_id": document_id,
        "case_id": case_id,
        "probable_case_key": probable_case_key,
        "fact_family": str(fact.get("fact_family") or ""),
        "entity_scope": str(fact.get("entity_scope") or "document"),
        "fact_key": str(fact.get("fact_key") or ""),
        "normalized_value": str(fact.get("normalized_value") or ""),
        "raw_value": str(fact.get("raw_value") or ""),
        "confidence": float(fact.get("confidence") or 0.0),
        "observed_at": str(fact.get("observed_at") or observed_at),
        "source_ref": str(fact.get("source_ref") or ""),
        "status": str(fact.get("status") or "active"),
        "metadata": dict(fact.get("metadata") or {}),
        "created_at": observed_at,
    }


def build_drive_ingest_runtime(
    settings: Settings,
    *,
    client: GoogleDriveClient | None = None,
    store: MailboxMemoryStore | None = None,
    graph_store: Any | None = None,
    allow_in_memory: bool = False,
) -> DriveIngestRuntime | None:
    if not bool(getattr(settings, "google_drive_enabled", False)) or not bool(getattr(settings, "google_drive_ingest_enabled", False)):
        return None
    database_url = str(getattr(settings, "mailbox_memory_database_url", "") or "").strip()
    resolved_store = store
    if resolved_store is None:
        if database_url:
            resolved_store = PostgresMailboxMemoryStore(
                database_url,
                vector_enabled=bool(getattr(settings, "mailbox_memory_vector_enabled", False)),
                embedding_dimensions=int(getattr(settings, "openai_compat_embedding_dimensions", 0) or 0),
            )
        elif allow_in_memory:
            resolved_store = InMemoryMailboxMemoryStore()
        else:
            return None
    resolved_graph_store = graph_store
    if resolved_graph_store is None and bool(getattr(settings, "google_drive_graph_enabled", False)) and database_url:
        from graph_store import PostgresGraphStore

        resolved_graph_store = PostgresGraphStore(database_url)
    return DriveIngestRuntime(
        settings=settings,
        store=resolved_store,
        blob_root=Path(getattr(settings, "mailbox_memory_blob_root")).resolve() / "gdrive",
        client=client or GoogleDriveClient(settings),
        graph_store=resolved_graph_store,
        docling_enabled=bool(getattr(settings, "docling_enabled", False)),
        docling_options={
            "max_pages": int(getattr(settings, "docling_max_pages", 0) or 0),
            "timeout_sec": int(getattr(settings, "docling_timeout_sec", 0) or 0),
        },
        vector_enabled=bool(getattr(settings, "mailbox_memory_vector_enabled", False)),
        embedding_model=str(getattr(settings, "openai_compat_embedding_model", "") or ""),
        embedding_runtime=build_embedding_runtime(settings),
    )


__all__ = [
    "DriveIngestRuntime",
    "build_fact",
    "build_drive_document_row",
    "build_drive_fact_row",
    "build_drive_ingest_runtime",
    "dedupe_facts",
    "detect_drive_conflicts",
    "extract_drive_facts",
    "map_extraction_status",
]
