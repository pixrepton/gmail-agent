"""Mailbox-memory ingest, document intelligence, snapshotting, and context retrieval."""

from __future__ import annotations

import hashlib
import json
import os as _os

# E4: skip legacy mailbox_memory_snapshots writes when engagement_snapshot_v2 feed is active.
# This prevents dual-write without removing the read path (backfill / legacy consumers).
def _legacy_snapshot_write_enabled() -> bool:
    """Return False when engagement_snapshot_v2 is the active feed source (profile full)."""
    from daszek_engagement_feed import engagement_feed_source_enabled  # noqa: PLC0415
    return not engagement_feed_source_enabled()
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from attachment_content_extraction import parse_attachment_document
from document_field_extractor import extract_structured_fields, structured_fields_to_fact_rows
from document_parse_contract import should_skip_regex_document_facts
from document_parse_runtime import build_parse_config_from_runtime
from config import document_intelligence_promote_facts_enabled
from attachment_intelligence import build_attachment_records
from case_identity import derive_canonical_case_id
from case_routing import apply_routing_to_case_row, route_gmail_message
from embedding_runtime import build_embedding_runtime
from mailbox_memory_health import (
    VECTOR_PATH_DISABLED,
    VECTOR_PATH_FAILED,
    VECTOR_PATH_UNAVAILABLE,
    VECTOR_PATH_USED,
)
from mailbox_memory_models import CaseContextPack, MailboxMemoryIngestResult
from correlation_registry.service import CorrelationRegistryService, build_correlation_registry_service
from mailbox_memory_store import (
    InMemoryMailboxMemoryStore,
    MailboxMemoryStore,
    PostgresMailboxMemoryStore,
    _vector_literal,
)


MAX_ZIP_CHILDREN = 25
MAX_ZIP_CHILD_BYTES = 4_000_000
CHUNK_TARGET_CHARS = 900

def apply_embeddings_to_chunk_rows(
    rows: list[dict[str, Any]],
    *,
    vector_enabled: bool,
    embedding_model: str,
    embedding_runtime: Any | None,
    updated_at: str,
    created_at_fallback: str,
) -> list[dict[str, Any]]:
    """Apply bounded OpenAI-compatible embeddings to existing chunk rows (re-embed path)."""
    if not rows:
        return rows
    if not vector_enabled:
        return rows
    ts = updated_at or created_at_fallback
    if embedding_runtime is None:
        reason = "Embedding provider is not configured for this environment."
        for row in rows:
            row["embedding_model"] = embedding_model
            row["embedding_status"] = "provider_unavailable" if embedding_model else "provider_unconfigured"
            row["embedding_updated_at"] = ts
            row["embedding_error"] = reason
        return rows
    try:
        vectors = list(embedding_runtime.embed_texts([str(row.get("chunk_text") or "") for row in rows]))
    except Exception as exc:  # noqa: BLE001
        error_text = str(exc)
        for row in rows:
            row["embedding_model"] = embedding_model
            row["embedding_status"] = "failed"
            row["embedding_updated_at"] = ts
            row["embedding_error"] = error_text
        return rows
    for index, row in enumerate(rows):
        row["embedding_model"] = embedding_model
        row["embedding_updated_at"] = ts
        vector = vectors[index] if index < len(vectors) else None
        if vector:
            row["embedding"] = vector
            row["embedding_status"] = "ready"
            row["embedding_error"] = ""
        else:
            row["embedding_status"] = "failed"
            row["embedding_error"] = "Embedding provider returned no vector for chunk."
    return rows


EMAIL_RE = re.compile(r"(?P<email>[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})", re.IGNORECASE)
# Mime Content-ID pattern: filename.ext@HEXID.XX — not a real email address
_MIME_CID_RE = re.compile(r"^[^\s@]+\.(png|jpg|jpeg|gif|bmp|svg|tiff?|ico|webp)@[0-9A-F]{6,}\.", re.IGNORECASE)


def _is_real_email(email: str) -> bool:
    """Return False for MIME Content-ID CID references like image007.png@01DCE8F7.DA."""
    return not _MIME_CID_RE.match(email)
PHONE_RE = re.compile(r"(?:(?:\+48)?\s*)?(\d{3}[\s\-]?\d{3}[\s\-]?\d{3})")
_REGON_9_WEIGHTS = (8, 9, 2, 3, 4, 5, 6, 7)
_NIP_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)
_REGISTRY_LABEL_CONTEXT_RE = re.compile(r"(?:\bnip\b|\bkrs\b|\bregon\b)\s*[:\s|]*$", re.IGNORECASE)
_PHONE_LABEL_CONTEXT_RE = re.compile(r"\b(?:tel\.?|telefon|phone|fax\.?|komórka|komorka|gsm)\b", re.IGNORECASE)
AREA_RE = re.compile(r"(?:(?:powierzchni|powierzchnia|dom(?:u)?)\D{0,24})?(\d{2,4}(?:[.,]\d{1,2})?)\s*(?:m2|m²)", re.IGNORECASE)
CASE_TOKEN_RE = re.compile(r"\b([A-Z]{2,}[A-Z0-9\-_\/]{2,}\d{1,}[A-Z0-9\-_\/]*)\b")
CITY_HINT_RE = re.compile(r"(?:lokalizacja|miasto|miejscowość|miejscowosc|w)\s*[:\-]?\s*([A-ZŁŚŻŹĆŃÓ][A-Za-zÀ-ÿąćęłńóśżźŁŚŻŹĆŃÓ\-]+)")

# building_type: (pattern, normalized_value, confidence) — checked in order; best confidence wins
_BUILDING_TYPE_RULES: tuple[tuple[re.Pattern[str], str, float], ...] = (
    (re.compile(r"dom\s+jednorodzinny", re.IGNORECASE), "single_family_house", 0.7),
    (re.compile(r"dom\s+wolnostoj[aą]cy", re.IGNORECASE), "single_family_house", 0.7),
    (re.compile(r"bli[zź]niak", re.IGNORECASE), "semi_detached", 0.7),
    (re.compile(r"szeregowiec", re.IGNORECASE), "terraced", 0.7),
    (re.compile(r"\bszereg\b", re.IGNORECASE), "terraced", 0.5),
    (re.compile(r"mieszkanie", re.IGNORECASE), "apartment", 0.7),
    (re.compile(r"\blokal\b", re.IGNORECASE), "apartment", 0.5),
    (re.compile(r"\bdom\b", re.IGNORECASE), "single_family_house", 0.5),
    (re.compile(r"\b(?:budynek|obiekt)\b", re.IGNORECASE), "other", 0.5),
)

_POWER_KW_MIN = 3.0
_POWER_KW_MAX = 100.0
_POWER_KW_CONTEXTUAL_RE = re.compile(
    r"(?:moc|pompa|pomp[aąęy]|zapotrzebowanie|zapotrzebowania|grzewcz[aą]|ogrzewani[aę])\b"
    r"(?:\W{0,40})?"
    r"(\d+(?:[.,]\d+)?)\s*(?:kW|kilowat(?:[óo]w|y|a)?)",
    re.IGNORECASE,
)
_POWER_KW_POMPA_GLUED_RE = re.compile(
    r"pomp[aąęy]\s*(\d+(?:[.,]\d+)?)\s*kW",
    re.IGNORECASE,
)
_POWER_KW_BARE_RE = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s*(?:kW|kilowat(?:[óo]w|y|a)?)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class MailboxMemoryRuntime:
    """Python-owned mailbox-memory layer with safe staged rollout."""

    store: MailboxMemoryStore
    blob_root: Path
    stage_mode: str = "disabled"
    stage_allowlist: tuple[str, ...] = ()
    graph_store: Any | None = None
    docling_enabled: bool = False
    docling_options: dict[str, Any] = field(default_factory=dict)
    attachment_parser_chain: tuple[str, ...] = ()
    unstructured_enabled: bool = False
    document_structured_facts_enabled: bool = True
    vector_enabled: bool = False
    embedding_model: str = ""
    embedding_runtime: Any | None = None
    correlation_registry: CorrelationRegistryService | None = None
    signal_extraction_mode: str = "llm"

    def bootstrap(self) -> None:
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self.store.bootstrap()
        if self.correlation_registry is not None:
            self.correlation_registry.bootstrap()
        if self.graph_store is not None:
            self.graph_store.bootstrap()

    @property
    def enabled(self) -> bool:
        return self.stage_mode in {"shadow", "live"}

    def stage_allows(self, *, message_id: str, thread_id: str, case_id: str) -> bool:
        if not self.enabled:
            return False
        if self.stage_mode == "live":
            return True
        if not self.stage_allowlist:
            return True
        tokens = {message_id.strip(), thread_id.strip(), case_id.strip()}
        tokens.discard("")
        allow = {item.strip() for item in self.stage_allowlist if item.strip()}
        return bool(tokens.intersection(allow))

    def fetch_thread_memory(self, thread_id: str) -> dict[str, Any]:
        resolved_thread_id = str(thread_id or "").strip()
        if not resolved_thread_id:
            return {}
        row = self.store.fetch_thread_memory(resolved_thread_id) or {}
        memory = row.get("memory_json")
        if not isinstance(memory, dict):
            return {}
        if str(memory.get("thread_id") or "").strip() != resolved_thread_id:
            return {}
        return dict(memory)

    def persist_thread_memory(
        self,
        thread_memory: dict[str, Any],
        *,
        case_id: str = "",
        message_id: str = "",
        source_kind: str = "node_b_generated",
        only_if_absent: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(thread_memory, dict):
            return {}
        thread_id = str(thread_memory.get("thread_id") or "").strip()
        if not thread_id:
            return {}
        serialized = json.dumps(
            thread_memory,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        memory_json = json.loads(serialized)
        updated_at = datetime.now().astimezone().isoformat()
        self.store.upsert_thread_memory(
            {
                "thread_id": thread_id,
                "case_id": str(case_id or memory_json.get("case_id") or "").strip(),
                "source_message_id": str(message_id or "").strip(),
                "memory_json": memory_json,
                "memory_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                "source_kind": str(source_kind or "node_b_generated").strip(),
                "version": 1,
                "created_at": updated_at,
                "updated_at": updated_at,
            },
            only_if_absent=only_if_absent,
        )
        return self.fetch_thread_memory(thread_id)

    def _message_source_facts(
        self,
        *,
        case_id: str,
        message_id: str,
        message: dict[str, Any],
        observed_at: str,
        hvac_signals: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], str]:
        """Message-level facts: LLM hvac_signals (B3) or legacy regex, with optional regex fallback."""
        mode = str(self.signal_extraction_mode or "llm").strip().lower()
        body_text = " ".join(
            part
            for part in (
                str(message.get("subject") or ""),
                str(message.get("snippet") or ""),
                str(message.get("body") or ""),
            )
            if part
        )
        base_meta = {"origin": "source_message", "signal_extraction_mode": mode}

        if mode == "llm":
            signals = hvac_signals if isinstance(hvac_signals, dict) and hvac_signals else {}
            facts = facts_from_hvac_signals(
                signals,
                case_id=case_id,
                message_id=message_id,
                observed_at=observed_at,
                source_type="message",
                source_ref=message_id,
                entity_scope="customer",
                metadata={**base_meta, "extraction_path": "llm_intake"},
            )
            if facts:
                return facts, "llm_intake"
            if body_text.strip():
                return (
                    extract_facts_from_text(
                        case_id=case_id,
                        message_id=message_id,
                        document_id="",
                        text=body_text,
                        source_type="message",
                        source_ref=message_id,
                        observed_at=observed_at,
                        entity_scope="customer",
                        metadata={**base_meta, "extraction_path": "signal_extraction_fallback_regex"},
                    ),
                    "signal_extraction_fallback_regex",
                )
            return [], "llm_intake_empty"

        return (
            extract_facts_from_text(
                case_id=case_id,
                message_id=message_id,
                document_id="",
                text=body_text,
                source_type="message",
                source_ref=message_id,
                observed_at=observed_at,
                entity_scope="customer",
                metadata={**base_meta, "extraction_path": "regex"},
            ),
            "regex",
        )

    def ingest_message(
        self,
        *,
        snapshot: dict[str, Any],
        intake_result: dict[str, Any],
        case_link_result: dict[str, Any],
        attachment_fetcher: Callable[[str, str], bytes] | None = None,
        attachment_max_bytes: int = 8_000_000,
        process_attachment_documents: bool = True,
        refresh_document_intelligence: bool = False,
        hvac_signals: dict[str, Any] | None = None,
    ) -> MailboxMemoryIngestResult:
        message = snapshot.get("source_message") or {}
        message_id = str(message.get("message_id") or "").strip()
        thread_id = str(message.get("thread_id") or "").strip()
        case_id = derive_case_id(snapshot=snapshot, intake_result=intake_result, case_link_result=case_link_result)
        if not self.stage_allows(message_id=message_id, thread_id=thread_id, case_id=case_id):
            return MailboxMemoryIngestResult(enabled=False, case_id=case_id, message_id=message_id)

        now_iso = datetime.now().astimezone().isoformat()
        sender = str(message.get("sender") or message.get("from") or "").strip()
        sender_email = _extract_first_email(sender)
        mailbox = str(snapshot.get("mailbox") or "")
        from outbound_receipt import (
            counterparty_email_for_message,
            infer_live_direction,
            source_kind_for_direction,
            try_apply_communication_sent_receipt,
        )

        direction = infer_live_direction(message if isinstance(message, dict) else {}, mailbox=mailbox)
        source_kind = source_kind_for_direction(direction)
        counterparty_email = counterparty_email_for_message(
            message if isinstance(message, dict) else {},
            direction=direction,
            mailbox=mailbox,
        )
        customer_email = counterparty_email or (sender_email if direction != "outbound" else "")
        attachments_preview = build_attachment_records(snapshot)
        routing = route_gmail_message(
            subject=str(message.get("subject") or ""),
            snippet=str(message.get("snippet") or ""),
            sender=sender,
            labels=list(message.get("labels") or []),
            body=str(message.get("body") or message.get("body_text") or ""),
            has_attachment=bool(attachments_preview),
            direction=direction if direction in {"inbound", "outbound"} else "inbound",
            source_kind=source_kind,
        )
        if not routing.upsert_allowed:
            return MailboxMemoryIngestResult(enabled=False, case_id=case_id, message_id=message_id)
        existing_case = None
        try:
            existing_case = self.store.fetch_case(case_id)
        except Exception:  # noqa: BLE001
            existing_case = None
        preserved_customer_email = ""
        if isinstance(existing_case, dict):
            preserved_customer_email = str(existing_case.get("customer_email") or "").strip()
        case_customer_email = preserved_customer_email or customer_email
        case_row = {
            "case_id": case_id,
            "case_key": str(case_link_result.get("selected_case_key") or "").strip(),
            "thread_id": thread_id,
            "case_family": str((intake_result.get("case_assessment") or {}).get("case_family") or "unknown"),
            "mailbox": mailbox,
            "subject": str(message.get("subject") or ""),
            "status": "open",
            "customer_name": _guess_customer_name(
                sender if direction != "outbound" else str((message.get("to") or [""])[0] if isinstance(message.get("to"), list) else message.get("to") or "")
            ),
            "customer_email": case_customer_email,
            "metadata": {
                "case_link_decision": str(case_link_result.get("decision") or ""),
                "source_message_id": message_id,
                "direction": direction,
            },
            "created_at": message.get("date") or now_iso,
            "updated_at": now_iso,
        }
        case_row = apply_routing_to_case_row(case_row, routing)
        self.store.upsert_case(case_row)
        # ── Entity Identity: delegate to correlation_registry (identity + engagement + links) ──
        if self.correlation_registry is not None and case_customer_email:
            self.correlation_registry.sync_mailbox_case(
                case_id=case_id,
                customer_email=case_customer_email,
                thread_id=thread_id,
                message_id=message_id,
                customer_name=str(case_row.get("customer_name") or ""),
            )

        message_row = {
            "message_id": message_id,
            "case_id": case_id,
            "thread_id": thread_id,
            "mailbox": mailbox,
            "sender": sender,
            "sender_email": sender_email,
            "recipients": message.get("to") or [],
            "subject": str(message.get("subject") or ""),
            "snippet": str(message.get("snippet") or ""),
            "body_text": str(message.get("body") or ""),
            "labels": message.get("labels") or [],
            "received_at": message.get("date") or now_iso,
            "raw_snapshot": snapshot,
            "created_at": message.get("date") or now_iso,
            "updated_at": now_iso,
            "direction": direction,
        }
        self.store.upsert_message(message_row)

        events: list[dict[str, Any]] = []
        if direction == "outbound":
            event_type = "communication_sent"
            summary_text = f"Wysłano wiadomość: {str(message.get('subject') or '').strip()}"
        else:
            event_type = "message_received"
            summary_text = f"Odebrano wiadomość: {str(message.get('subject') or '').strip()}"
        events.append(
            self._append_event(
                case_id=case_id,
                message_id=message_id,
                thread_id=thread_id,
                event_type=event_type,
                occurred_at=message.get("date") or now_iso,
                summary_text=summary_text,
                payload={
                    "sender": sender,
                    "subject": str(message.get("subject") or ""),
                    "direction": direction,
                },
                source_refs=[{"type": "message", "message_id": message_id}],
            )
        )
        if direction == "outbound":
            db_url = str(getattr(self.store, "database_url", "") or "").strip()
            try_apply_communication_sent_receipt(
                case_id=case_id,
                thread_id=thread_id,
                message_id=message_id,
                occurred_at=str(message.get("date") or now_iso),
                correlation_registry=self.correlation_registry,
                database_url=db_url,
            )

        attachments = build_attachment_records(snapshot)
        attachment_rows: list[dict[str, Any]] = []
        document_rows: list[dict[str, Any]] = []
        fact_rows: list[dict[str, Any]] = []
        for attachment in attachments:
            attachment_row, docs, doc_facts, doc_events = self._ingest_attachment(
                case_id=case_id,
                message_id=message_id,
                thread_id=thread_id,
                attachment=attachment,
                attachment_fetcher=attachment_fetcher,
                attachment_max_bytes=attachment_max_bytes,
                process_attachment_documents=process_attachment_documents,
                occurred_at=message.get("date") or now_iso,
            )
            attachment_rows.append(attachment_row)
            document_rows.extend(docs)
            fact_rows.extend(doc_facts)
            events.extend(doc_events)

        message_facts, message_extraction_path = self._message_source_facts(
            case_id=case_id,
            message_id=message_id,
            message=message,
            observed_at=str(message.get("date") or now_iso),
            hvac_signals=hvac_signals if isinstance(hvac_signals, dict) else None,
        )
        message_facts.extend(
            extract_reference_facts(
                case_id=case_id,
                message_id=message_id,
                text=str(message.get("subject") or ""),
                observed_at=message.get("date") or now_iso,
                entity_scope="case",
            )
        )
        fact_rows.extend(message_facts)
        self.store.replace_message_facts(message_id=message_id, rows=fact_rows)
        events.append(
            self._append_event(
                case_id=case_id,
                message_id=message_id,
                thread_id=thread_id,
                event_type="facts_extracted",
                occurred_at=message.get("date") or now_iso,
                summary_text=f"Wyciągnięto {len(fact_rows)} faktów dla wiadomości.",
                payload={"fact_count": len(fact_rows), "message_extraction_path": message_extraction_path},
                source_refs=[{"type": "message", "message_id": message_id}],
            )
        )
        events.append(
            self._append_event(
                case_id=case_id,
                message_id=message_id,
                thread_id=thread_id,
                event_type="case_linked",
                occurred_at=message.get("date") or now_iso,
                summary_text=f"Powiązano wiadomość ze sprawą {case_id}.",
                payload={
                    "case_link_decision": str(case_link_result.get("decision") or ""),
                    "selected_case_key": str(case_link_result.get("selected_case_key") or ""),
                },
                source_refs=[{"type": "message", "message_id": message_id}, {"type": "case", "case_id": case_id}],
            )
        )

        snapshot_row = build_case_snapshot(
            case_id=case_id,
            case_record=self.store.fetch_case(case_id) or case_row,
            messages=self.store.fetch_messages_for_case(case_id, limit=10),
            facts=self.store.fetch_facts_for_case(case_id),
            documents=self.store.fetch_documents_for_case(case_id, limit=8),
            events=self.store.fetch_events_for_case(case_id, limit=20),
            next_action=self.store.fetch_next_action(case_id) or {},
            drive_enrichment=collect_drive_case_enrichment(
                store=self.store,
                case_id=case_id,
                query_text=str(message.get("subject") or ""),
                graph_store=self.graph_store,
            ),
        )
        if _legacy_snapshot_write_enabled():
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
        events.append(
            self._append_event(
                case_id=case_id,
                message_id=message_id,
                thread_id=thread_id,
                event_type="case_snapshot_updated",
                occurred_at=now_iso,
                summary_text="Zaktualizowano snapshot sprawy.",
                payload={"status": str(snapshot_row.get("status") or ""), "open_questions": len(snapshot_row.get("open_questions") or [])},
                source_refs=[{"type": "case", "case_id": case_id}],
            )
        )
        refresh_stats: dict[str, Any] = {}
        if refresh_document_intelligence:
            refresh_stats = self.bounded_refresh_document_intelligence_for_case(
                case_id=case_id,
                occurred_at=now_iso,
            )
            events.append(
                self._append_event(
                    case_id=case_id,
                    message_id=message_id,
                    thread_id=thread_id,
                    event_type="document_intelligence_refresh_completed",
                    occurred_at=now_iso,
                    summary_text="Bounded document intelligence refresh (chunk re-embed) completed.",
                    payload={"refresh_document_intelligence": True, **refresh_stats},
                    source_refs=[{"type": "case", "case_id": case_id}],
                )
            )
        context_pack = build_case_context_pack(
            store=self.store,
            case_id=case_id,
            query_text=" ".join(
                part
                for part in (str(message.get("subject") or ""), str(message.get("body") or "")[:1000])
                if part
            ),
            graph_store=self.graph_store,
            retrieval_runtime=self,
        )
        return MailboxMemoryIngestResult(
            enabled=True,
            case_id=case_id,
            message_id=message_id,
            snapshot=snapshot_row,
            context_pack=context_pack,
            events=events,
            facts=fact_rows,
            documents=document_rows,
            attachments=attachment_rows,
        )

    def finalize_case(
        self,
        *,
        case_id: str,
        message_id: str,
        thread_id: str,
        business_result: dict[str, Any] | None,
        reply_result: dict[str, Any] | None,
        action_plan_result: dict[str, Any] | None,
        case_intelligence_result: dict[str, Any] | None,
    ) -> MailboxMemoryIngestResult:
        if not self.enabled or not case_id:
            return MailboxMemoryIngestResult(enabled=False, case_id=case_id, message_id=message_id)

        thread_memory = (case_intelligence_result or {}).get("thread_memory")
        if isinstance(thread_memory, dict) and str(thread_memory.get("thread_id") or "").strip():
            self.persist_thread_memory(
                thread_memory,
                case_id=case_id,
                message_id=message_id,
                source_kind="node_b_generated",
            )

        next_action = derive_next_action_record(
            case_id=case_id,
            business_result=business_result or {},
            reply_result=reply_result or {},
            action_plan_result=action_plan_result or {},
            case_intelligence_result=case_intelligence_result or {},
        )
        self.store.upsert_next_action(case_id, next_action)
        self._append_event(
            case_id=case_id,
            message_id=message_id,
            thread_id=thread_id,
            event_type="next_action_updated",
            occurred_at=next_action.get("updated_at"),
            summary_text="Zapisano rekomendowany następny krok.",
            payload=next_action,
            source_refs=[{"type": "case", "case_id": case_id}],
        )

        inferred = infer_case_status(
            business_result=business_result or {},
            action_plan_result=action_plan_result or {},
            case_intelligence_result=case_intelligence_result or {},
        )
        # RC-05: finalize used to fetch_case() then upsert_case() unlocked — the
        # exact TOCTOU window _stamp_case_runtime_state's mutate_case exists to
        # close (see its comment). Two concurrent finalizes (e.g. a live signal and
        # a Case Intelligence retry landing at the same time) could both read the
        # same prior row and one silently lose the other's status/lifecycle write.
        # mutate_case's SELECT ... FOR UPDATE closes it the same way.
        def _finalize_case_status(row: dict[str, Any]) -> dict[str, Any]:
            case_row = dict(row)
            case_row["status"] = inferred
            case_row["lifecycle_state"] = infer_lifecycle_from_case_status(inferred)
            case_row["updated_at"] = next_action.get("updated_at")
            return case_row

        case_record = self.store.mutate_case(case_id, _finalize_case_status, create_if_missing=True)

        snapshot_row = build_case_snapshot(
            case_id=case_id,
            case_record=case_record,
            messages=self.store.fetch_messages_for_case(case_id, limit=10),
            facts=self.store.fetch_facts_for_case(case_id),
            documents=self.store.fetch_documents_for_case(case_id, limit=8),
            events=self.store.fetch_events_for_case(case_id, limit=20),
            next_action=next_action,
            drive_enrichment=collect_drive_case_enrichment(
                store=self.store,
                case_id=case_id,
                query_text=str(next_action.get("next_action") or ""),
                graph_store=self.graph_store,
            ),
        )
        if _legacy_snapshot_write_enabled():
            self.store.upsert_snapshot(
                case_id,
                {
                    "status": str(snapshot_row.get("status") or "open"),
                    "customer_name": str((snapshot_row.get("customer") or {}).get("name") or ""),
                    "customer_email": str((snapshot_row.get("customer") or {}).get("email") or ""),
                    "recommended_next_action": str(snapshot_row.get("recommended_next_action") or ""),
                    "snapshot_json": snapshot_row,
                    "updated_at": next_action.get("updated_at"),
                },
            )
        context_pack = build_case_context_pack(
            store=self.store,
            case_id=case_id,
            query_text=str(next_action.get("next_action") or ""),
            graph_store=self.graph_store,
            retrieval_runtime=self,
        )
        return MailboxMemoryIngestResult(
            enabled=True,
            case_id=case_id,
            message_id=message_id,
            snapshot=snapshot_row,
            context_pack=context_pack,
            next_action=next_action,
        )

    def get_context_pack(self, *, case_id: str = "", message_id: str = "", query_text: str = "") -> CaseContextPack:
        resolved_case_id = case_id.strip()
        if not resolved_case_id and message_id.strip():
            case = self.store.fetch_case_by_message_id(message_id.strip()) or {}
            resolved_case_id = str(case.get("case_id") or "").strip()
        if not resolved_case_id:
            return CaseContextPack(case_id="", next_action={})
        return build_case_context_pack(
            store=self.store,
            case_id=resolved_case_id,
            query_text=query_text,
            graph_store=self.graph_store,
            retrieval_runtime=self,
        )

    def _ingest_attachment(
        self,
        *,
        case_id: str,
        message_id: str,
        thread_id: str,
        attachment: dict[str, Any],
        attachment_fetcher: Callable[[str, str], bytes] | None,
        attachment_max_bytes: int,
        process_attachment_documents: bool,
        occurred_at: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        attachment_id = str(attachment.get("attachment_id") or "").strip()
        file_name = str(attachment.get("file_name") or "").strip()
        mime_type = str(attachment.get("mime_type") or "").strip()
        gmail_attachment_id = str(attachment.get("storage_ref") or "").strip()
        raw_bytes = b""
        warnings: list[str] = []
        if callable(attachment_fetcher) and gmail_attachment_id:
            try:
                raw_bytes = attachment_fetcher(message_id, gmail_attachment_id)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"fetch_failed:{type(exc).__name__}")
        if raw_bytes and len(raw_bytes) > attachment_max_bytes:
            warnings.append("attachment_too_large_for_memory")
            raw_bytes = b""

        blob_path = ""
        content_sha = ""
        if raw_bytes:
            content_sha = hashlib.sha256(raw_bytes).hexdigest()
            blob_path = self._write_blob(content_sha, raw_bytes)

        attachment_row = {
            "attachment_id": attachment_id,
            "case_id": case_id,
            "message_id": message_id,
            "thread_id": thread_id,
            "file_name": file_name,
            "mime_type": mime_type,
            "size_bytes": int(attachment.get("size_bytes") or len(raw_bytes)),
            "gmail_attachment_id": gmail_attachment_id,
            "content_sha256": content_sha,
            "blob_path": blob_path,
            "metadata": {
                "warnings": warnings,
                "attachment_business_type": str(attachment.get("attachment_business_type") or ""),
            },
            "created_at": occurred_at,
            "updated_at": occurred_at,
        }
        if not process_attachment_documents:
            attachment_row["metadata"]["warnings"] = [*warnings, "document_processing_skipped_metadata_only"]
            self.store.upsert_attachment(attachment_row)
            return attachment_row, [], [], []
        self.store.upsert_attachment(attachment_row)
        documents, facts, events = self._build_documents_for_attachment(
            case_id=case_id,
            message_id=message_id,
            thread_id=thread_id,
            attachment_row=attachment_row,
            raw_bytes=raw_bytes,
            occurred_at=occurred_at,
        )
        return attachment_row, documents, facts, events

    def _build_documents_for_attachment(
        self,
        *,
        case_id: str,
        message_id: str,
        thread_id: str,
        attachment_row: dict[str, Any],
        raw_bytes: bytes,
        occurred_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        file_name = str(attachment_row.get("file_name") or "")
        mime_type = str(attachment_row.get("mime_type") or "")
        attachment_id = str(attachment_row.get("attachment_id") or "")
        root_document_id = stable_id("doc", attachment_id, file_name or mime_type)
        document_specs = [{
            "document_id": root_document_id,
            "parent_document_id": "",
            "file_name": file_name,
            "mime_type": mime_type,
            "raw_bytes": raw_bytes,
            "source_type": "attachment",
            "content_sha256": str(attachment_row.get("content_sha256") or ""),
            "blob_path": str(attachment_row.get("blob_path") or ""),
            "metadata_warnings": [],
        }]
        if raw_bytes and _looks_like_zip(file_name=file_name, mime_type=mime_type):
            children, zip_warnings = extract_zip_children(raw_bytes, parent_anchor=attachment_id)
            document_specs[0]["source_type"] = "archive"
            document_specs[0]["metadata_warnings"] = zip_warnings
            for child in children:
                child_sha = hashlib.sha256(child["raw_bytes"]).hexdigest()
                child_blob = self._write_blob(child_sha, child["raw_bytes"])
                document_specs.append({
                    "document_id": child["document_id"],
                    "parent_document_id": root_document_id,
                    "file_name": child["file_name"],
                    "mime_type": child["mime_type"],
                    "raw_bytes": child["raw_bytes"],
                    "source_type": "archive_child",
                    "content_sha256": child_sha,
                    "blob_path": child_blob,
                    "metadata_warnings": [],
                })

        document_rows: list[dict[str, Any]] = []
        fact_rows: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        for spec in document_specs:
            parse_config = build_parse_config_from_runtime(self)
            parse_result = parse_attachment_document(
                spec.get("raw_bytes") or b"",
                mime_type=str(spec.get("mime_type") or ""),
                file_name=str(spec.get("file_name") or ""),
                docling_enabled=parse_config.docling_enabled,
                unstructured_enabled=parse_config.unstructured_enabled,
                parser_chain=parse_config.resolved_chain(),
                docling_options=dict(parse_config.docling_options),
                structured_facts_enabled=parse_config.structured_facts_enabled,
            )
            extraction = parse_result.to_extraction_dict()
            extraction_metadata = dict(extraction.get("metadata") or {})
            parser_warnings = list(extraction_metadata.get("warnings") or [])
            parser_id = str(extraction_metadata.get("parser_id") or parse_result.parser_id or "")
            docling_used = parser_id == "docling" or str(extraction.get("parser_provenance") or "") == "docling"
            parser_stack = list(parse_config.resolved_chain())
            document_row = {
                "document_id": str(spec.get("document_id") or ""),
                "case_id": case_id,
                "message_id": message_id,
                "attachment_id": attachment_id,
                "parent_document_id": str(spec.get("parent_document_id") or ""),
                "file_name": str(spec.get("file_name") or ""),
                "mime_type": str(spec.get("mime_type") or ""),
                "source_type": str(spec.get("source_type") or "attachment"),
                "document_kind": infer_document_kind(str(spec.get("file_name") or ""), str(spec.get("mime_type") or "")),
                "extraction_status": str(extraction.get("extraction_status") or "pending"),
                "parser_name": str(extraction.get("parser_provenance") or extraction.get("extraction_method") or ""),
                "content_sha256": str(spec.get("content_sha256") or ""),
                "blob_path": str(spec.get("blob_path") or ""),
                "text_content": str(extraction.get("extracted_text") or ""),
                "summary_text": summarize_document_text(str(extraction.get("extracted_text") or ""), file_name=str(spec.get("file_name") or "")),
                "metadata": {
                    "extraction_method": str(extraction.get("extraction_method") or ""),
                    "extraction_confidence": float(extraction.get("extraction_confidence") or 0.0),
                    "content_sha256_prefix": str(extraction.get("content_sha256_prefix") or ""),
                    "warnings": list(spec.get("metadata_warnings") or []) + parser_warnings,
                    "parser_stack": parser_stack,
                    "parser_id": parser_id,
                    "structured_parse": bool(parse_result.structured),
                    "element_count": int(extraction_metadata.get("element_count") or len(parse_result.elements)),
                    "docling_used": docling_used,
                    "ocr_used": "ocr" in str(extraction.get("extraction_method") or "").lower(),
                    "page_count": int(extraction_metadata.get("page_count") or 0),
                    "table_count": int(extraction_metadata.get("table_count") or 0),
                    "fallback_reason": ""
                    if parser_id in {"docling", "unstructured", "legacy_structured"}
                    else ("; ".join(parser_warnings[:2]) if parser_warnings else ""),
                },
                "created_at": occurred_at,
                "updated_at": occurred_at,
            }
            self.store.upsert_document(document_row)
            try:
                from document_intelligence_runtime import build_document_intelligence_result, document_fields_to_fact_rows

                structured_fields = extract_structured_fields(
                    parse_result,
                    source_id=document_row["document_id"],
                )
                docintel = build_document_intelligence_result(
                    source_type="gmail_attachment",
                    source_id=document_row["document_id"],
                    case_id=case_id,
                    filename=document_row["file_name"],
                    mime_type=document_row["mime_type"],
                    text=document_row["text_content"],
                    parser=document_row["parser_name"] or parser_id or "fallback",
                    parser_confidence=float((document_row.get("metadata") or {}).get("extraction_confidence") or 0.0),
                    pre_extracted_fields=structured_fields or None,
                )
                docintel_row = docintel.to_dict()
                self.store.upsert_document_intelligence_result(docintel_row)
                if structured_fields:
                    structured_fact_rows = structured_fields_to_fact_rows(
                        structured_fields,
                        case_id=case_id,
                        document_id=document_row["document_id"],
                        message_id=message_id,
                        observed_at=occurred_at,
                        parser_id=parser_id,
                    )
                    fact_rows.extend(structured_fact_rows)
                elif document_intelligence_promote_facts_enabled():
                    fact_rows.extend(document_fields_to_fact_rows(docintel_row))
            except Exception as exc:  # noqa: BLE001
                events.append(
                    self._append_event(
                        case_id=case_id,
                        message_id=message_id,
                        thread_id=thread_id,
                        event_type="document_intelligence_failed",
                        occurred_at=occurred_at,
                        summary_text=f"Document intelligence failed: {document_row['file_name']}",
                        payload={"document_id": document_row["document_id"], "error": str(exc)[:500]},
                        source_refs=[{"type": "document", "document_id": document_row["document_id"]}],
                    )
                )
            chunks = self._build_chunk_rows(
                case_id=case_id,
                document_id=document_row["document_id"],
                file_name=document_row["file_name"],
                text=document_row["text_content"],
                created_at=occurred_at,
                source_type="mailbox_document_chunk",
                updated_at=occurred_at,
            )
            self.store.replace_document_chunks(document_row["document_id"], chunks)
            document_rows.append(document_row)
            if not should_skip_regex_document_facts(
                parse_result,
                structured_facts_enabled=bool(self.document_structured_facts_enabled),
            ):
                fact_rows.extend(
                    extract_facts_from_text(
                        case_id=case_id,
                        message_id=message_id,
                        document_id=document_row["document_id"],
                        text=document_row["text_content"],
                        source_type="document",
                        source_ref=document_row["document_id"],
                        observed_at=occurred_at,
                        entity_scope="document",
                        metadata={
                            "file_name": document_row["file_name"],
                            "document_kind": document_row["document_kind"],
                            "parser_id": parser_id,
                        },
                    )
                )
            events.append(
                self._append_event(
                    case_id=case_id,
                    message_id=message_id,
                    thread_id=thread_id,
                    event_type="attachment_parsed",
                    occurred_at=occurred_at,
                    summary_text=f"Przetworzono dokument: {document_row['file_name']}",
                    payload={
                        "document_id": document_row["document_id"],
                        "extraction_status": document_row["extraction_status"],
                        "parser_name": document_row["parser_name"],
                    },
                    source_refs=[{"type": "document", "document_id": document_row["document_id"]}],
                )
            )
        return document_rows, fact_rows, events

    def _build_chunk_rows(
        self,
        *,
        case_id: str,
        document_id: str,
        file_name: str,
        text: str,
        created_at: str,
        source_type: str,
        updated_at: str = "",
    ) -> list[dict[str, Any]]:
        rows = build_document_chunks(
            case_id=case_id,
            document_id=document_id,
            file_name=file_name,
            text=text,
            created_at=created_at,
            source_type=source_type,
            updated_at=updated_at,
        )
        if not rows:
            return []
        return apply_embeddings_to_chunk_rows(
            rows,
            vector_enabled=bool(self.vector_enabled),
            embedding_model=str(self.embedding_model or ""),
            embedding_runtime=self.embedding_runtime,
            updated_at=str(updated_at or created_at),
            created_at_fallback=str(created_at),
        )

    def bounded_refresh_document_intelligence_for_case(
        self,
        *,
        case_id: str,
        occurred_at: str,
        max_mailbox_documents: int = 4,
        max_drive_documents: int = 4,
        max_chunks_per_document: int = 16,
    ) -> dict[str, Any]:
        """Re-apply bounded embeddings to already stored chunk rows (no Gmail/Drive re-download)."""
        stats = {
            "mailbox_documents_touched": 0,
            "mailbox_chunks_reembedded": 0,
            "drive_documents_touched": 0,
            "drive_chunks_reembedded": 0,
            "drive_chunks_materialized": 0,
            "vector_enabled": bool(self.vector_enabled),
        }
        if not case_id.strip():
            return stats

        mailbox_docs = self.store.fetch_documents_for_case(case_id, limit=max_mailbox_documents)
        for doc in mailbox_docs:
            doc_id = str(doc.get("document_id") or "").strip()
            if not doc_id:
                continue
            pool = [dict(c) for c in (self.store.fetch_chunks_for_case(case_id, limit=400) or []) if str(c.get("document_id") or "") == doc_id]
            if not pool:
                continue
            pool.sort(key=lambda row: int(row.get("ordinal") or 0))
            rows = pool[:max_chunks_per_document]
            apply_embeddings_to_chunk_rows(
                rows,
                vector_enabled=bool(self.vector_enabled),
                embedding_model=str(self.embedding_model or ""),
                embedding_runtime=self.embedding_runtime,
                updated_at=str(occurred_at),
                created_at_fallback=str(rows[0].get("created_at") or occurred_at),
            )
            self.store.replace_document_chunks(doc_id, rows)
            stats["mailbox_documents_touched"] += 1
            stats["mailbox_chunks_reembedded"] += len(rows)

        replace_drive = getattr(self.store, "replace_drive_document_chunks", None)
        fetch_drive_docs = getattr(self.store, "fetch_drive_documents_for_case", None)
        fetch_drive_chunks = getattr(self.store, "fetch_drive_chunks_for_case", None)
        if callable(replace_drive) and callable(fetch_drive_docs) and callable(fetch_drive_chunks):
            drive_docs = fetch_drive_docs(case_id, limit=max_drive_documents)
            for doc in drive_docs or []:
                doc_id = str(doc.get("document_id") or "").strip()
                if not doc_id:
                    continue
                pool = [dict(c) for c in (fetch_drive_chunks(case_id, limit=400) or []) if str(c.get("document_id") or "") == doc_id]
                if not pool:
                    text_content = str(doc.get("text_content") or "").strip()
                    if not text_content:
                        continue
                    raw_rows = build_document_chunks(
                        case_id=case_id,
                        document_id=doc_id,
                        file_name=str(doc.get("file_name") or "drive_document.txt"),
                        text=text_content,
                        created_at=str(occurred_at),
                        source_type="drive_document_chunk",
                        updated_at=str(occurred_at),
                    )
                    if not raw_rows:
                        continue
                    rows = raw_rows[:max_chunks_per_document]
                    apply_embeddings_to_chunk_rows(
                        rows,
                        vector_enabled=bool(self.vector_enabled),
                        embedding_model=str(self.embedding_model or ""),
                        embedding_runtime=self.embedding_runtime,
                        updated_at=str(occurred_at),
                        created_at_fallback=str(rows[0].get("created_at") or occurred_at),
                    )
                    replace_drive(document_id=doc_id, rows=rows)
                    stats["drive_documents_touched"] += 1
                    stats["drive_chunks_materialized"] += len(rows)
                    continue
                pool.sort(key=lambda row: int(row.get("ordinal") or 0))
                rows = pool[:max_chunks_per_document]
                apply_embeddings_to_chunk_rows(
                    rows,
                    vector_enabled=bool(self.vector_enabled),
                    embedding_model=str(self.embedding_model or ""),
                    embedding_runtime=self.embedding_runtime,
                    updated_at=str(occurred_at),
                    created_at_fallback=str(rows[0].get("created_at") or occurred_at),
                )
                replace_drive(document_id=doc_id, rows=rows)
                stats["drive_documents_touched"] += 1
                stats["drive_chunks_reembedded"] += len(rows)

        return stats

    def _append_event(
        self,
        *,
        case_id: str,
        message_id: str,
        thread_id: str,
        event_type: str,
        occurred_at: Any,
        summary_text: str,
        payload: dict[str, Any],
        source_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        event = {
            "event_id": stable_id("mmevt", case_id, message_id, thread_id, event_type, summary_text[:48]),
            "case_id": case_id,
            "message_id": message_id,
            "thread_id": thread_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "summary_text": summary_text,
            "payload": payload,
            "source_refs": source_refs,
        }
        self.store.append_event(event)
        return event

    def _write_blob(self, content_sha256: str, data: bytes) -> str:
        prefix = content_sha256[:2]
        target = self.blob_root / prefix / content_sha256
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        return str(target)


def build_mailbox_memory_runtime(settings: Any, *, allow_in_memory: bool = False) -> MailboxMemoryRuntime | None:
    stage_mode = str(getattr(settings, "mailbox_memory_stage_mode", "disabled") or "disabled").strip().lower()
    db_url = str(getattr(settings, "mailbox_memory_database_url", "") or "").strip()
    blob_root = Path(str(getattr(settings, "mailbox_memory_blob_root", "") or "")).resolve()
    allowlist = tuple(
        str(item).strip()
        for item in getattr(settings, "mailbox_memory_stage_allowlist", ()) or ()
        if str(item).strip()
    )
    if stage_mode not in {"shadow", "live"}:
        return None
    graph_store = None
    if bool(getattr(settings, "google_drive_graph_enabled", False)) and db_url:
        try:
            from graph_store import PostgresGraphStore

            graph_store = PostgresGraphStore(db_url)
        except Exception:  # pragma: no cover - runtime fallback
            graph_store = None
    if db_url:
        correlation = build_correlation_registry_service(db_url)
        return MailboxMemoryRuntime(
            store=PostgresMailboxMemoryStore(
                db_url,
                vector_enabled=bool(getattr(settings, "mailbox_memory_vector_enabled", False)),
                embedding_dimensions=int(getattr(settings, "openai_compat_embedding_dimensions", 0) or 0),
            ),
            blob_root=blob_root,
            stage_mode=stage_mode,
            stage_allowlist=allowlist,
            correlation_registry=correlation,
            graph_store=graph_store,
            docling_enabled=bool(getattr(settings, "docling_enabled", False)),
            docling_options={
                "max_pages": int(getattr(settings, "docling_max_pages", 0) or 0),
                "timeout_sec": int(getattr(settings, "docling_timeout_sec", 0) or 0),
            },
            attachment_parser_chain=tuple(getattr(settings, "attachment_parser_chain", ()) or ()),
            unstructured_enabled=bool(getattr(settings, "unstructured_enabled", False)),
            document_structured_facts_enabled=bool(
                getattr(settings, "document_structured_facts_enabled", True)
            ),
            vector_enabled=bool(getattr(settings, "mailbox_memory_vector_enabled", False)),
            embedding_model=str(getattr(settings, "openai_compat_embedding_model", "") or ""),
            embedding_runtime=build_embedding_runtime(settings),
            signal_extraction_mode=str(
                getattr(settings, "signal_extraction_mode", "llm") or "llm"
            ).strip().lower(),
        )
    if allow_in_memory:
        correlation = build_correlation_registry_service("", in_memory=True)
        return MailboxMemoryRuntime(
            store=InMemoryMailboxMemoryStore(),
            blob_root=blob_root,
            stage_mode=stage_mode,
            stage_allowlist=allowlist,
            correlation_registry=correlation,
            graph_store=graph_store,
            docling_enabled=bool(getattr(settings, "docling_enabled", False)),
            docling_options={
                "max_pages": int(getattr(settings, "docling_max_pages", 0) or 0),
                "timeout_sec": int(getattr(settings, "docling_timeout_sec", 0) or 0),
            },
            attachment_parser_chain=tuple(getattr(settings, "attachment_parser_chain", ()) or ()),
            unstructured_enabled=bool(getattr(settings, "unstructured_enabled", False)),
            document_structured_facts_enabled=bool(
                getattr(settings, "document_structured_facts_enabled", True)
            ),
            vector_enabled=bool(getattr(settings, "mailbox_memory_vector_enabled", False)),
            embedding_model=str(getattr(settings, "openai_compat_embedding_model", "") or ""),
            embedding_runtime=build_embedding_runtime(settings),
            signal_extraction_mode=str(
                getattr(settings, "signal_extraction_mode", "llm") or "llm"
            ).strip().lower(),
        )
    return None


def derive_case_id(*, snapshot: dict[str, Any], intake_result: dict[str, Any], case_link_result: dict[str, Any]) -> str:
    source_message = snapshot.get("source_message") or {}
    case_family = str((intake_result.get("case_assessment") or {}).get("case_family") or "unknown").strip() or "unknown"
    thread_id = str(source_message.get("thread_id") or "").strip()
    message_id = str(source_message.get("message_id") or "").strip()
    return derive_canonical_case_id(
        case_family=case_family,
        selected_case_key=str(case_link_result.get("selected_case_key") or ""),
        reference_tokens=source_message.get("reference_tokens") or {},
        thread_id=thread_id,
        message_id=message_id,
    )


def infer_document_kind(file_name: str, mime_type: str) -> str:
    name = str(file_name or "").lower()
    mime = str(mime_type or "").lower()
    if name.endswith(".zip") or "zip" in mime:
        return "archive"
    if name.endswith(".pdf") or mime == "application/pdf":
        return "pdf"
    if name.endswith(".docx"):
        return "docx"
    if name.endswith(".xlsx") or name.endswith(".xls"):
        return "spreadsheet"
    if mime.startswith("image/") or any(name.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".heic")):
        return "image"
    return "generic"


def summarize_document_text(text: str, *, file_name: str) -> str:
    cleaned = " ".join(str(text or "").strip().split())
    if not cleaned:
        return f"Brak odczytanego tekstu dla dokumentu {file_name or 'bez nazwy'}."
    if len(cleaned) <= 220:
        return cleaned
    return cleaned[:219].rstrip() + "…"


def build_document_chunks(
    *,
    case_id: str,
    document_id: str,
    file_name: str,
    text: str,
    created_at: str,
    source_type: str = "mailbox_document_chunk",
    updated_at: str = "",
) -> list[dict[str, Any]]:
    cleaned = "\n".join(line.strip() for line in str(text or "").splitlines())
    if not cleaned.strip():
        return []
    pieces: list[str] = []
    current = ""
    for block in re.split(r"\n{2,}", cleaned):
        block = block.strip()
        if not block:
            continue
        if len(block) > CHUNK_TARGET_CHARS:
            if current:
                pieces.append(current)
                current = ""
            start = 0
            while start < len(block):
                pieces.append(block[start : start + CHUNK_TARGET_CHARS])
                start += CHUNK_TARGET_CHARS
            continue
        if len(current) + len(block) + 2 <= CHUNK_TARGET_CHARS:
            current = f"{current}\n\n{block}".strip()
            continue
        if current:
            pieces.append(current)
        current = block
    if current:
        pieces.append(current)
    rows: list[dict[str, Any]] = []
    for ordinal, piece in enumerate(pieces):
        rows.append(
            {
                "chunk_id": stable_id("chunk", document_id, str(ordinal)),
                "document_id": document_id,
                "case_id": case_id,
                "ordinal": ordinal,
                "chunk_text": piece,
                "token_estimate": max(1, len(piece.split())),
                "embedding_model": "",
                "embedding_status": "missing",
                "embedding_updated_at": None,
                "embedding_error": "",
                "metadata": {"file_name": file_name, "source_type": source_type},
                "created_at": created_at,
                **({"updated_at": updated_at or created_at} if updated_at else {}),
            }
        )
    return rows


def build_case_snapshot(
    *,
    case_id: str,
    case_record: dict[str, Any],
    messages: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    events: list[dict[str, Any]],
    next_action: dict[str, Any],
    drive_enrichment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    drive_enrichment = drive_enrichment if isinstance(drive_enrichment, dict) else {}
    drive_facts = list(drive_enrichment.get("drive_facts") or [])
    drive_documents = list(drive_enrichment.get("drive_documents") or [])
    completeness_gaps = list(drive_enrichment.get("completeness_gaps") or [])
    graph_hints = list(drive_enrichment.get("graph_hints") or [])
    reference_documents = list(drive_enrichment.get("reference_documents") or [])
    combined_facts = list(facts) + drive_facts
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for fact in combined_facts:
        key = (str(fact.get("entity_scope") or "case"), str(fact.get("fact_key") or ""))
        grouped.setdefault(key, []).append(fact)

    key_facts: list[dict[str, Any]] = []
    conflicting_facts: list[dict[str, Any]] = []
    open_questions: list[str] = []
    for (entity_scope, fact_key), items in grouped.items():
        unique_values = {
            str(item.get("normalized_value") or "").strip()
            for item in items
            if str(item.get("normalized_value") or "").strip()
        }
        ranked = sorted(items, key=lambda item: (-float(item.get("confidence") or 0.0), str(item.get("observed_at") or "")))
        preferred = ranked[0] if ranked else {}
        if preferred and str(preferred.get("normalized_value") or "").strip():
            key_facts.append(
                {
                    "entity_scope": entity_scope,
                    "fact_key": fact_key,
                    "value": str(preferred.get("normalized_value") or ""),
                    "confidence": float(preferred.get("confidence") or 0.0),
                    "source_ref": str(preferred.get("source_ref") or ""),
                }
            )
        if len(unique_values) > 1:
            conflict_entry = {"entity_scope": entity_scope, "fact_key": fact_key, "values": sorted(unique_values)}
            conflicting_facts.append(conflict_entry)
            open_questions.append(f"Konflikt danych dla {fact_key}: {', '.join(conflict_entry['values'])}.")

    cross_scope_values: dict[str, set[str]] = {}
    for fact in combined_facts:
        key = str(fact.get("fact_key") or "").strip()
        value = str(fact.get("normalized_value") or "").strip()
        if key and value:
            cross_scope_values.setdefault(key, set()).add(value)
    existing_conflict_keys = {str(item.get("fact_key") or "") for item in conflicting_facts}
    for fact_key, values in cross_scope_values.items():
        if len(values) <= 1 or fact_key in existing_conflict_keys:
            continue
        conflict_entry = {"entity_scope": "mixed", "fact_key": fact_key, "values": sorted(values)}
        conflicting_facts.append(conflict_entry)
        open_questions.append(f"Konflikt danych dla {fact_key}: {', '.join(conflict_entry['values'])}.")

    customer_name = str(case_record.get("customer_name") or "")
    customer_email = str(case_record.get("customer_email") or "")
    if not customer_email:
        for item in key_facts:
            if item["fact_key"] == "customer_email":
                customer_email = item["value"]
                break
    if not customer_name:
        for item in key_facts:
            if item["fact_key"] == "customer_name":
                customer_name = item["value"]
                break

    latest_documents = [
        {
            "document_id": str(item.get("document_id") or ""),
            "file_name": str(item.get("file_name") or ""),
            "document_kind": str(item.get("document_kind") or ""),
            "summary_text": str(item.get("summary_text") or ""),
            "updated_at": str(item.get("updated_at") or item.get("created_at") or ""),
        }
        for item in documents[:5]
    ]
    if not next_action.get("next_action") and open_questions:
        next_action = dict(next_action)
        next_action["next_action"] = "review_required"
        next_action["rationale"] = open_questions[0]

    status = str(case_record.get("status") or "open")
    if open_questions and status == "open":
        status = "awaiting_review"

    return {
        "case_id": case_id,
        "status": status,
        "customer": {"name": customer_name, "email": customer_email},
        "latest_signal_id": str(case_record.get("latest_signal_id") or ""),
        "latest_signal_at": str(case_record.get("latest_signal_at") or ""),
        "last_rebuild_at": str(case_record.get("last_rebuild_at") or ""),
        "last_projection_refresh_at": str(case_record.get("last_projection_refresh_at") or ""),
        "last_source_kinds_seen": list(case_record.get("last_source_kinds_seen") or []),
        "key_facts": key_facts[:8],
        "open_questions": _dedupe_texts(open_questions)[:8],
        "latest_documents": latest_documents,
        "drive_documents_summary": [
            {
                "document_id": str(item.get("document_id") or ""),
                "file_name": str(item.get("file_name") or item.get("title") or ""),
                "lane": str(item.get("lane") or ""),
                "document_kind": str(item.get("document_kind") or ""),
                "scope": str(item.get("scope") or ""),
                "source_ref": str(item.get("source_ref") or ""),
                "summary_text": str(item.get("summary_text") or ""),
            }
            for item in drive_documents[:8]
        ],
        "completeness_gaps": _dedupe_texts(completeness_gaps)[:8],
        "graph_hints": graph_hints[:10],
        "reference_documents": reference_documents[:8],
        "conflicting_facts": conflicting_facts[:8],
        "recommended_next_action": str(next_action.get("next_action") or ""),
        "recommended_next_action_reason": str(next_action.get("rationale") or ""),
        "recent_message_ids": [str(item.get("message_id") or "") for item in messages[:5]],
        "recent_event_types": [str(item.get("event_type") or "") for item in events[:8]],
        "updated_at": datetime.now().astimezone().isoformat(),
    }


def _hot_state_open_questions(hot_state: dict[str, Any]) -> list[str]:
    questions: list[str] = []
    for item in list(hot_state.get("open_loops") or []):
        if isinstance(item, dict):
            text = str(item.get("description") or item.get("summary") or item.get("loop_id") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            questions.append(text)
    return questions


def _fetch_current_hot_state(
    *,
    store: MailboxMemoryStore,
    case_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    fetcher = getattr(store, "fetch_latest_case_snapshot_version", None)
    if not callable(fetcher):
        return {}, {}
    row = fetcher(case_id)
    if not isinstance(row, dict):
        return {}, {}
    hot_state = row.get("snapshot_json")
    if not isinstance(hot_state, dict) or not hot_state:
        return {}, {}
    case_block = hot_state.get("case") if isinstance(hot_state.get("case"), dict) else {}
    hot_case_id = str(case_block.get("case_id") or hot_state.get("case_id") or "").strip()
    if hot_case_id and hot_case_id != case_id:
        return {}, {}
    return dict(hot_state), dict(row)


def build_current_case_context_snapshot(
    *,
    store: MailboxMemoryStore,
    case_id: str,
    case_record: dict[str, Any],
    messages: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    events: list[dict[str, Any]],
    next_action: dict[str, Any],
    drive_enrichment: dict[str, Any],
) -> dict[str, Any]:
    snapshot = build_case_snapshot(
        case_id=case_id,
        case_record=case_record,
        messages=messages,
        facts=facts,
        documents=documents,
        events=events,
        next_action=next_action,
        drive_enrichment=drive_enrichment,
    )
    snapshot["context_snapshot_status"] = "current"
    snapshot["context_snapshot_source"] = "mailbox_memory_live_projection"
    snapshot["context_snapshot_generated_at"] = datetime.now().astimezone().isoformat()
    for field_name in ("case_key", "case_family"):
        value = str(case_record.get(field_name) or "").strip()
        if value:
            snapshot[field_name] = value

    hot_state, hot_row = _fetch_current_hot_state(store=store, case_id=case_id)
    if not hot_state:
        return snapshot

    case_block = hot_state.get("case") if isinstance(hot_state.get("case"), dict) else {}
    for source_name, target_name in (
        ("case_key", "case_key"),
        ("case_family", "case_family"),
        ("lifecycle_status", "status"),
        ("operational_status", "operational_status"),
        ("waiting_for", "waiting_for"),
        ("priority", "priority"),
        ("summary_text", "summary_text"),
    ):
        value = str(case_block.get(source_name) or "").strip()
        if value:
            snapshot[target_name] = value

    snapshot["open_questions"] = _hot_state_open_questions(hot_state)
    if isinstance(hot_state.get("key_facts"), list):
        snapshot["key_facts"] = list(hot_state.get("key_facts") or [])
    if isinstance(hot_state.get("active_conflicts"), list):
        snapshot["conflicting_facts"] = list(hot_state.get("active_conflicts") or [])
    if isinstance(hot_state.get("documents_summary"), list):
        snapshot["latest_documents"] = list(hot_state.get("documents_summary") or [])

    recommended_next_step = str(hot_state.get("recommended_next_step") or "").strip()
    if recommended_next_step:
        snapshot["recommended_next_action"] = recommended_next_step

    snapshot_meta = hot_state.get("snapshot_meta") if isinstance(hot_state.get("snapshot_meta"), dict) else {}
    source_signal_id = str(
        snapshot_meta.get("source_signal_id")
        or hot_state.get("source_signal_id")
        or hot_row.get("source_signal_id")
        or ""
    ).strip()
    if source_signal_id:
        snapshot["latest_signal_id"] = source_signal_id

    version = hot_row.get("version") or snapshot_meta.get("version")
    if version is not None:
        snapshot["context_snapshot_version"] = int(version)
    snapshot["context_snapshot_status"] = "current"
    snapshot["context_snapshot_source"] = "case_snapshot_hot_state"
    snapshot["context_snapshot_generated_at"] = str(
        hot_row.get("created_at")
        or snapshot_meta.get("created_at")
        or snapshot["context_snapshot_generated_at"]
    )
    return snapshot


def build_case_context_pack(
    *,
    store: MailboxMemoryStore,
    case_id: str,
    query_text: str = "",
    graph_store: Any | None = None,
    retrieval_runtime: Any | None = None,
) -> CaseContextPack:
    facts = store.fetch_facts_for_case(case_id)
    active_facts, conflicting_facts = split_conflicting_facts(facts)
    documents = store.fetch_documents_for_case(case_id, limit=8)
    messages = store.fetch_messages_for_case(case_id, limit=10)
    events = store.fetch_events_for_case(case_id, limit=12)
    next_action = store.fetch_next_action(case_id) or {}
    case_row = store.fetch_case(case_id) or {}
    drive_enrichment = collect_drive_case_enrichment(
        store=store,
        case_id=case_id,
        query_text=query_text,
        graph_store=graph_store,
    )
    snapshot = build_current_case_context_snapshot(
        store=store,
        case_id=case_id,
        case_record=case_row,
        messages=messages,
        facts=facts,
        documents=documents,
        events=events,
        next_action=next_action,
        drive_enrichment=drive_enrichment,
    )
    drive_documents = list(drive_enrichment.get("drive_documents") or [])
    drive_facts = list(drive_enrichment.get("drive_facts") or [])
    drive_active_facts, drive_conflicts = split_conflicting_facts(drive_facts)
    vector_scores: dict[str, float] | None = None
    semantic_rows: list[dict[str, Any]] = []
    rt = retrieval_runtime
    qtext = str(query_text or "").strip()
    vr_summary: dict[str, Any] = {
        "vector_path_status": VECTOR_PATH_DISABLED,
        "detail": "vector_path_disabled",
        "retrieval_mode": "lexical_freshness_fallback",
        "fallback_reason": "vector_path_disabled",
        "semantic_candidate_count": 0,
        "embedding_error": "",
        "semantic_error": "",
    }
    vec_enabled = bool(getattr(rt, "vector_enabled", False)) if rt is not None else False
    if not vec_enabled:
        vr_summary["detail"] = "MAILBOX_MEMORY_VECTOR_ENABLED=0_or_no_runtime"
        vr_summary["fallback_reason"] = "MAILBOX_MEMORY_VECTOR_ENABLED=0_or_no_runtime"
    elif not qtext:
        vr_summary["vector_path_status"] = VECTOR_PATH_UNAVAILABLE
        vr_summary["detail"] = "empty_query_text"
        vr_summary["fallback_reason"] = "empty_query_text"
    else:
        emb_runtime = getattr(rt, "embedding_runtime", None)
        if emb_runtime is None:
            vr_summary["vector_path_status"] = VECTOR_PATH_UNAVAILABLE
            vr_summary["detail"] = "embedding_runtime_missing"
            vr_summary["fallback_reason"] = "embedding_runtime_missing"
        else:
            qvecs: list[Any] = []
            embed_ok = False
            try:
                qvecs = list(emb_runtime.embed_texts([qtext]))
                embed_ok = True
            except Exception as exc:  # noqa: BLE001
                vr_summary["vector_path_status"] = VECTOR_PATH_FAILED
                vr_summary["detail"] = "embedding_exception"
                vr_summary["fallback_reason"] = "embedding_exception"
                vr_summary["embedding_error"] = str(exc)[:2000]
            if embed_ok:
                qvec = qvecs[0] if qvecs else None
                lit = _vector_literal(qvec) if qvec else None
                if not lit:
                    vr_summary["vector_path_status"] = VECTOR_PATH_UNAVAILABLE
                    vr_summary["detail"] = "empty_embedding_vector"
                    vr_summary["fallback_reason"] = "empty_embedding_vector"
                else:
                    fetch_sem = getattr(store, "fetch_semantic_chunk_candidates_for_case", None)
                    if not callable(fetch_sem):
                        vr_summary["vector_path_status"] = VECTOR_PATH_UNAVAILABLE
                        vr_summary["detail"] = "semantic_fetch_not_supported"
                        vr_summary["fallback_reason"] = "semantic_fetch_not_supported"
                    else:
                        try:
                            semantic_rows = list(
                                fetch_sem(case_id, lit, limit_mailbox=50, limit_drive=50) or []
                            )
                        except Exception as exc:  # noqa: BLE001
                            vr_summary["vector_path_status"] = VECTOR_PATH_FAILED
                            vr_summary["detail"] = "semantic_fetch_exception"
                            vr_summary["fallback_reason"] = "semantic_fetch_exception"
                            vr_summary["semantic_error"] = str(exc)[:2000]
                            semantic_rows = []
                        else:
                            vr_summary["vector_path_status"] = VECTOR_PATH_USED
                            vr_summary["semantic_candidate_count"] = len(semantic_rows)
                            vr_summary["detail"] = f"semantic_fetch_ok_candidates={len(semantic_rows)}"
                            vr_summary["fallback_reason"] = "" if semantic_rows else "semantic_fetch_no_candidates"
                            vr_summary["retrieval_mode"] = (
                                "hybrid_vector_lexical" if semantic_rows else "lexical_freshness_fallback"
                            )
                            if semantic_rows:
                                vector_scores = {}
                                for row in semantic_rows:
                                    cid = str(row.get("chunk_id") or "")
                                    if not cid:
                                        continue
                                    sim = float(row.get("vector_similarity") or 0.0)
                                    vector_scores[cid] = max(0.0, min(1.0, sim))
    base_chunks = _collect_case_chunks(store, case_id=case_id)
    if semantic_rows:
        seen_ids = {str(c.get("chunk_id") or "") for c in base_chunks}
        for row in semantic_rows:
            cid = str(row.get("chunk_id") or "")
            if cid and cid not in seen_ids:
                base_chunks.append(_prepare_chunk_for_context(row, fallback_source_type="drive_document_chunk"))
                seen_ids.add(cid)
    chunks = rank_chunks(
        base_chunks,
        query_text=query_text,
        limit=6,
        vector_scores=vector_scores,
        vector_path_status=str(vr_summary.get("vector_path_status") or ""),
        vector_path_detail=str(vr_summary.get("detail") or ""),
    )
    action_proposals = store.fetch_action_proposals(case_id=case_id, limit=20) if hasattr(store, "fetch_action_proposals") else []
    execution_results = store.fetch_execution_results(case_id=case_id, limit=20) if hasattr(store, "fetch_execution_results") else []
    raw_calendar_events = store.fetch_calendar_events_for_case(case_id, limit=10) if hasattr(store, "fetch_calendar_events_for_case") else []
    from calendar_models import active_calendar_events, infer_calendar_risk

    calendar_events = active_calendar_events(raw_calendar_events)
    document_intelligence_rows = (
        store.fetch_document_intelligence_for_case(case_id, limit=20)
        if hasattr(store, "fetch_document_intelligence_for_case")
        else []
    )
    calendar_risk = infer_calendar_risk(events=raw_calendar_events, facts=active_facts + drive_active_facts)
    doc_conflicts = [conf for row in document_intelligence_rows for conf in list(row.get("conflicts") or [])]
    source_refs = build_source_refs(
        snapshot=snapshot,
        facts=active_facts + drive_active_facts,
        documents=documents + drive_documents,
        chunks=chunks,
        events=events,
    )
    source_refs.extend(
        {"type": "graph_hint", "relation_type": str(item.get("relation_type") or ""), "source_ref": str(item.get("source_ref") or "")}
        for item in (drive_enrichment.get("graph_hints") or [])[:6]
    )
    from similar_cases_precedent import fetch_similar_case_precedent_refs, fetch_similar_case_precedent_refs_v1

    connect = getattr(store, "_connect", None)
    precedent_refs: list[dict[str, Any]] = []
    if callable(connect):
        try:
            with connect() as conn:
                precedent_refs = fetch_similar_case_precedent_refs_v1(store, conn, case_id=case_id, limit=8)
        except Exception:
            precedent_refs = fetch_similar_case_precedent_refs(store, case_id=case_id, limit=5)
    else:
        precedent_refs = fetch_similar_case_precedent_refs(store, case_id=case_id, limit=5)

    # I2: Cross-fact coherence validator — flaguj niespójności, nie blokuj
    coherence_warnings: list[str] = []
    try:
        from case_coherence import CaseCoherenceValidator
        all_facts = active_facts + drive_active_facts
        validator = CaseCoherenceValidator()
        # Sprawdź każdą parę faktów o tym samym kluczu
        seen_keys: dict[str, list[dict]] = {}
        for fact in all_facts:
            key = str(fact.get("key", fact.get("fact_key", fact.get("name", "")))).strip().lower()
            if key:
                seen_keys.setdefault(key, []).append(fact)
        for key, same_key_facts in seen_keys.items():
            if len(same_key_facts) > 1:
                for i in range(len(same_key_facts)):
                    for j in range(i + 1, len(same_key_facts)):
                        f1 = same_key_facts[i]
                        f2 = same_key_facts[j]
                        result = validator.validate_fact_consistency(
                            existing_facts=[f1],
                            new_fact_key=str(f1.get("key", f1.get("fact_key", f1.get("name", "")))),
                            new_fact_value=f2.get("value", f2.get("fact_value", "")),
                        )
                        coherence_warnings.extend(result.warnings)
    except ImportError:
        pass
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Coherence validator error (non-blocking): %s", exc)

    return CaseContextPack(
        case_id=case_id,
        snapshot=snapshot,
        recent_events=events,
        active_facts=active_facts + drive_active_facts,
        conflicting_facts=conflicting_facts + drive_conflicts,
        latest_documents=documents,
        drive_documents_summary=drive_documents,
        completeness_gaps=list(drive_enrichment.get("completeness_gaps") or []),
        graph_hints=list(drive_enrichment.get("graph_hints") or []),
        reference_documents=list(drive_enrichment.get("reference_documents") or []),
        relevant_chunks=chunks,
        source_refs=source_refs[:24],
        next_action=next_action,
        action_proposals=action_proposals,
        execution_results=execution_results,
        calendar={
            "case_id": case_id,
            "events": calendar_events,
            "observed_events": raw_calendar_events,
            "next_event": calendar_events[0] if calendar_events else {},
            "has_calendar_event": bool(calendar_events),
            "calendar_risk": calendar_risk,
            "visit_lifecycle": "scheduled_visit" if calendar_events else ("proposed_visit" if calendar_risk == "customer_proposed_date" else "no_calendar_event"),
        },
        document_intelligence={
            "important_documents": document_intelligence_rows[:8],
            "extracted_fields": [field for row in document_intelligence_rows for field in list(row.get("extracted_fields") or [])][:40],
            "document_conflicts": doc_conflicts[:20],
            "fields_requiring_review": [
                field
                for row in document_intelligence_rows
                if bool(row.get("requires_human_review"))
                for field in list(row.get("extracted_fields") or [])
            ][:40],
        },
        runtime_state={
            "latest_signal_id": str(case_row.get("latest_signal_id") or ""),
            "latest_signal_at": str(case_row.get("latest_signal_at") or ""),
            "last_rebuild_at": str(case_row.get("last_rebuild_at") or ""),
            "last_projection_refresh_at": str(case_row.get("last_projection_refresh_at") or ""),
            "last_source_kinds_seen": list(case_row.get("last_source_kinds_seen") or []),
        },
        vector_retrieval=vr_summary,
        precedent_evidence_refs=precedent_refs,
        coherence_warnings=coherence_warnings,
    )


def collect_drive_case_enrichment(
    *,
    store: MailboxMemoryStore,
    case_id: str,
    query_text: str = "",
    graph_store: Any | None = None,
) -> dict[str, Any]:
    fetch_drive_documents_for_case = getattr(store, "fetch_drive_documents_for_case", None)
    fetch_drive_facts_for_case = getattr(store, "fetch_drive_facts_for_case", None)
    fetch_drive_documents = getattr(store, "fetch_drive_documents", None)
    if not callable(fetch_drive_documents_for_case) or not callable(fetch_drive_facts_for_case):
        return {
            "drive_documents": [],
            "drive_facts": [],
            "completeness_gaps": [],
            "graph_hints": [],
            "reference_documents": [],
        }

    drive_documents = list(fetch_drive_documents_for_case(case_id, limit=12) or [])
    drive_facts = list(fetch_drive_facts_for_case(case_id) or [])
    graph_hints = []
    if graph_store is not None:
        try:
            graph_hints = list(graph_store.fetch_case_hints(case_id, limit=12) or [])
        except Exception:  # pragma: no cover - defensive
            graph_hints = []

    hot_state, _hot_row = _fetch_current_hot_state(store=store, case_id=case_id)
    reference_key_facts = list(hot_state.get("key_facts") or [])
    for fact in list(store.fetch_facts_for_case(case_id) or []):
        value = str(fact.get("normalized_value") or "").strip()
        if value:
            reference_key_facts.append({"value": value})
    scope_terms = collect_reference_terms(
        query_text=query_text,
        drive_facts=drive_facts,
        case_snapshot={"key_facts": reference_key_facts},
    )
    reference_pool = []
    if callable(fetch_drive_documents):
        reference_pool = list(
            fetch_drive_documents(
                limit=80,
                scopes=("reference_template", "company_reference"),
            )
            or []
        )
    reference_documents = select_reference_documents(reference_pool, terms=scope_terms)
    completeness_gaps = infer_completeness_gaps(drive_documents, drive_facts)
    return {
        "drive_documents": summarize_drive_documents(drive_documents, limit=8),
        "drive_facts": drive_facts,
        "completeness_gaps": completeness_gaps,
        "graph_hints": summarize_graph_hints(graph_hints, limit=10),
        "reference_documents": summarize_reference_documents(reference_documents, limit=8),
    }


def summarize_drive_documents(drive_documents: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    return [
        {
            "document_id": str(item.get("document_id") or ""),
            "file_name": str(item.get("file_name") or ""),
            "lane": str(item.get("lane") or ""),
            "document_kind": str(item.get("document_kind") or ""),
            "scope": str(item.get("scope") or ""),
            "source_ref": str(item.get("source_ref") or ""),
            "summary_text": str(item.get("summary_text") or ""),
            "linkage_status": str(item.get("linkage_status") or ""),
        }
        for item in drive_documents[:limit]
    ]


def summarize_graph_hints(graph_hints: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    return [
        {
            "relation_type": str(item.get("relation_type") or ""),
            "related_node_type": str(item.get("related_node_type") or ""),
            "related_title": str(item.get("related_title") or ""),
            "confidence": float(item.get("confidence") or 0.0),
            "source_ref": str(item.get("source_ref") or ""),
        }
        for item in graph_hints[:limit]
    ]


def summarize_reference_documents(reference_documents: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    return [
        {
            "document_id": str(item.get("document_id") or ""),
            "file_name": str(item.get("file_name") or ""),
            "lane": str(item.get("lane") or ""),
            "document_kind": str(item.get("document_kind") or ""),
            "scope": str(item.get("scope") or ""),
            "source_ref": str(item.get("source_ref") or ""),
            "summary_text": str(item.get("summary_text") or ""),
        }
        for item in reference_documents[:limit]
    ]


def collect_reference_terms(*, query_text: str, drive_facts: list[dict[str, Any]], case_snapshot: dict[str, Any]) -> set[str]:
    terms = {
        token.lower()
        for token in re.findall(r"[A-Za-zÀ-ÿ0-9_/.-]{3,}", str(query_text or ""))
    }
    snapshot_payload = case_snapshot.get("snapshot_json", case_snapshot) if isinstance(case_snapshot, dict) else {}
    if isinstance(snapshot_payload, dict):
        for fact in snapshot_payload.get("key_facts") or []:
            value = str((fact or {}).get("value") or "").strip()
            if value:
                terms.update(token.lower() for token in re.findall(r"[A-Za-zÀ-ÿ0-9_/.-]{3,}", value))
    for fact in drive_facts:
        if str(fact.get("fact_key") or "") in {"device_model", "device_model_bundle", "model_bundle", "offer_family", "manufacturer"}:
            value = str(fact.get("normalized_value") or "").strip()
            if value:
                terms.update(token.lower() for token in re.findall(r"[A-Za-zÀ-ÿ0-9_/.-]{3,}", value))
    return {term for term in terms if len(term) >= 3}


def select_reference_documents(reference_pool: list[dict[str, Any]], *, terms: set[str]) -> list[dict[str, Any]]:
    if not reference_pool:
        return []
    if not terms:
        return reference_pool[:5]
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in reference_pool:
        blob = " ".join(
            [
                str(item.get("file_name") or ""),
                str(item.get("summary_text") or ""),
                str(item.get("text_content") or "")[:2000],
                str(item.get("lane") or ""),
                str(item.get("document_kind") or ""),
            ]
        ).lower()
        score = sum(1 for term in terms if term in blob)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [dict(item[1]) for item in scored[:8]]


def infer_completeness_gaps(drive_documents: list[dict[str, Any]], drive_facts: list[dict[str, Any]]) -> list[str]:
    kinds = {str(item.get("document_kind") or "") for item in drive_documents}
    lanes = {str(item.get("lane") or "") for item in drive_documents}
    gaps: list[str] = []
    if "warranty_card" in kinds and not kinds.intersection({"contract", "order", "invoice", "deposit_invoice"}):
        gaps.append("Warranty present without canonical sales documents.")
    if {"order", "deposit_invoice", "invoice"}.intersection(kinds) and "contract" not in kinds:
        gaps.append("Commercial transactions present but signed contract is missing.")
    if "service_protocol" not in kinds and any(str(item.get("fact_key") or "") == "service_frequency" for item in drive_facts):
        gaps.append("Service requirement detected without linked service protocol.")
    if "case_folder" in lanes and not kinds.intersection({"contract", "order", "invoice", "warranty_card", "service_protocol"}):
        gaps.append("Media bundle exists without canonical case documents.")
    if "commercial_pricing" in lanes and not any(str(item.get("fact_family") or "") == "pricing" for item in drive_facts):
        gaps.append("Pricing workbook/list ingested without normalized pricing facts.")
    return _dedupe_texts(gaps)


def split_conflicting_facts(facts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rank facts per (entity_scope, fact_key) into one active value plus any
    genuine conflicts.

    `append_facts_with_supersession` (RP-29) already resolves a
    (entity_scope, fact_key) update by writing the prior row's `status` as
    `"superseded"` instead of leaving two competing `"active"` rows. A
    superseded row is a settled fact, not a live disagreement -- it must not
    re-enter ranking here, or a stale row with incidentally higher confidence
    or a newer `observed_at` could silently outrank the row the write path
    already declared current. Rows with no `status` at all (older producers,
    most existing tests) are treated as active, matching the schema default.
    """
    live_facts = [fact for fact in facts if str(fact.get("status") or "active") != "superseded"]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for fact in live_facts:
        grouped.setdefault((str(fact.get("entity_scope") or "case"), str(fact.get("fact_key") or "")), []).append(fact)
    active: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for (entity_scope, fact_key), items in grouped.items():
        # Two stable passes: newest-first, then confidence-descending. A stable
        # sort preserves the newest-first order among ties, so a confidence tie
        # keeps the most recently observed fact active instead of the oldest.
        ranked = sorted(items, key=lambda item: str(item.get("observed_at") or ""), reverse=True)
        ranked = sorted(ranked, key=lambda item: float(item.get("confidence") or 0.0), reverse=True)
        if ranked:
            active.append(ranked[0])
        values = {str(item.get("normalized_value") or "").strip() for item in items if str(item.get("normalized_value") or "").strip()}
        if len(values) > 1:
            conflicts.append({"entity_scope": entity_scope, "fact_key": fact_key, "values": sorted(values)})
    return active, conflicts


def rank_chunks(
    chunks: list[dict[str, Any]],
    *,
    query_text: str,
    limit: int,
    vector_scores: dict[str, float] | None = None,
    vector_path_status: str = "",
    vector_path_detail: str = "",
) -> list[dict[str, Any]]:
    query_tokens = {token for token in re.findall(r"[A-Za-zÀ-ÿ0-9_/-]{3,}", str(query_text or "").lower())}
    prepared = [_prepare_chunk_for_context(chunk) for chunk in chunks]
    scored: list[tuple[float, int, str, dict[str, Any]]] = []
    for chunk in prepared:
        text = str(chunk.get("chunk_text") or "").lower()
        lexical_hits = sorted(token for token in query_tokens if token in text)
        lexical_score = len(lexical_hits) / max(1, len(query_tokens)) if query_tokens else 0.0
        freshness_hint = 1.0 if str(chunk.get("updated_at") or chunk.get("created_at") or "").strip() else 0.5
        cid = str(chunk.get("chunk_id") or "")
        vector_score_error = ""
        try:
            raw_vec = float(vector_scores.get(cid, 0.0) or 0.0) if vector_scores else 0.0
        except (TypeError, ValueError):
            raw_vec = 0.0
            vector_score_error = "invalid_vector_score"
        vector_score = max(0.0, min(1.0, raw_vec))
        used_vector = bool(vector_scores is not None and vector_score > 1e-9)
        retrieval_score = round((lexical_score * 0.50) + (vector_score * 0.35) + (freshness_hint * 0.15), 6)
        fallback_reason = ""
        if not used_vector:
            if vector_score_error:
                fallback_reason = vector_score_error
            elif vector_scores is None:
                fallback_reason = vector_path_detail or vector_path_status or "vector_scores_missing"
            else:
                fallback_reason = "vector_score_missing_or_zero"
        sig: dict[str, Any] = {
            "lexical_overlap_tokens": lexical_hits,
            "lexical_score": round(lexical_score, 6),
            "vector_score": round(vector_score, 6),
            "freshness_hint": round(freshness_hint, 6),
            "used_vector": used_vector,
            "retrieval_mode": "hybrid_vector_lexical" if used_vector else "lexical_freshness_fallback",
            "ranking_reason": (
                "hybrid_score=lexical_0.50+vector_0.35+freshness_0.15"
                if used_vector
                else "fallback_score=lexical_0.50+freshness_0.15"
            ),
        }
        if fallback_reason:
            sig["fallback_reason"] = fallback_reason
        if vector_path_status:
            sig["vector_path_status"] = vector_path_status
        if vector_path_detail:
            sig["vector_path_detail"] = vector_path_detail
        chunk["retrieval_signals"] = sig
        chunk["retrieval_score"] = retrieval_score
        scored.append(
            (
                retrieval_score,
                len(lexical_hits),
                str(chunk.get("updated_at") or chunk.get("created_at") or ""),
                chunk,
            )
        )
    scored.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2],
            -int(item[3].get("ordinal") or 0),
        ),
        reverse=True,
    )
    ranked = [dict(item[3]) for item in scored if item[0] > 0][:limit]
    if ranked:
        return ranked
    return [dict(item[3]) for item in scored[:limit]]


def _collect_case_chunks(store: MailboxMemoryStore, *, case_id: str) -> list[dict[str, Any]]:
    mailbox_chunks = [
        _prepare_chunk_for_context(chunk, fallback_source_type="mailbox_document_chunk")
        for chunk in (store.fetch_chunks_for_case(case_id, limit=200) or [])
    ]
    fetch_drive_chunks_for_case = getattr(store, "fetch_drive_chunks_for_case", None)
    if not callable(fetch_drive_chunks_for_case):
        return mailbox_chunks
    drive_chunks = [
        _prepare_chunk_for_context(chunk, fallback_source_type="drive_document_chunk")
        for chunk in (fetch_drive_chunks_for_case(case_id, limit=200) or [])
    ]
    return mailbox_chunks + drive_chunks


def _prepare_chunk_for_context(chunk: dict[str, Any], *, fallback_source_type: str = "mailbox_document_chunk") -> dict[str, Any]:
    payload = dict(chunk)
    metadata = dict(payload.get("metadata") or {})
    payload["metadata"] = metadata
    payload["source_type"] = str(payload.get("source_type") or metadata.get("source_type") or fallback_source_type)
    payload["embedding_status"] = str(payload.get("embedding_status") or metadata.get("embedding_status") or "missing")
    payload["retrieval_signals"] = dict(payload.get("retrieval_signals") or {})
    payload["retrieval_score"] = float(payload.get("retrieval_score") or 0.0)
    return payload


def build_source_refs(
    *,
    snapshot: dict[str, Any],
    facts: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for message_id in snapshot.get("recent_message_ids") or []:
        refs.append({"type": "message", "id": message_id})
    for fact in facts[:5]:
        refs.append({"type": "fact", "id": str(fact.get("fact_id") or ""), "source_ref": str(fact.get("source_ref") or "")})
    for document in documents[:5]:
        refs.append({"type": "document", "id": str(document.get("document_id") or ""), "file_name": str(document.get("file_name") or "")})
    for chunk in chunks[:4]:
        refs.append({"type": "chunk", "id": str(chunk.get("chunk_id") or ""), "document_id": str(chunk.get("document_id") or "")})
    for event in events[:4]:
        refs.append({"type": "event", "id": str(event.get("event_id") or ""), "event_type": str(event.get("event_type") or "")})
    return refs


def infer_case_status(*, business_result: dict[str, Any], action_plan_result: dict[str, Any], case_intelligence_result: dict[str, Any]) -> str:
    review_mode = str((case_intelligence_result.get("review_routing") or {}).get("review_mode") or "")
    if review_mode and review_mode != "auto_safe":
        return "awaiting_review"
    primary_action = str(action_plan_result.get("primary_action") or "")
    if primary_action == "prepare_reply":
        return "awaiting_reply"
    if primary_action in {"update_case", "create_task"}:
        return "active"
    if str(business_result.get("recommended_next_action") or "") == "wait":
        return "waiting"
    return "open"


def infer_lifecycle_from_case_status(status: str) -> str:
    """Map legacy case_status to lifecycle_state (2026-06-25)."""
    mapping = {
        "awaiting_review": "qualification",
        "awaiting_reply": "qualification",
        "active": "qualification",
        "waiting": "waiting_for_client",
        "open": "new_lead",
    }
    return mapping.get(status, "qualification")


def derive_next_action_record(
    *,
    case_id: str,
    business_result: dict[str, Any],
    reply_result: dict[str, Any],
    action_plan_result: dict[str, Any],
    case_intelligence_result: dict[str, Any],
) -> dict[str, Any]:
    next_best_action = (case_intelligence_result.get("next_best_action") or {}).get("primary_next_action") or {}
    action_type = str(
        next_best_action.get("action_type")
        or action_plan_result.get("primary_action")
        or business_result.get("recommended_next_action")
        or ""
    ).strip()
    rationale = str(
        next_best_action.get("reason_pl")
        or action_plan_result.get("why_this_action")
        or business_result.get("recommended_action_reason")
        or ""
    ).strip()
    payload = {
        "business_result": business_result,
        "reply_result": {"draft_enabled": bool(reply_result.get("draft_enabled")), "draft_count": len(reply_result.get("drafts") or [])},
        "action_plan_result": action_plan_result,
        "case_intelligence_result": {
            "review_routing": case_intelligence_result.get("review_routing") or {},
            "next_best_action": case_intelligence_result.get("next_best_action") or {},
        },
    }
    return {
        "case_id": case_id,
        "next_action": action_type,
        "rationale": rationale,
        "source_stage": "case_intelligence",
        "payload": payload,
        "updated_at": datetime.now().astimezone().isoformat(),
    }


def facts_from_hvac_signals(
    hvac_signals: dict[str, Any],
    *,
    case_id: str,
    message_id: str,
    observed_at: str,
    source_type: str,
    source_ref: str,
    entity_scope: str,
    metadata: dict[str, Any],
    document_id: str = "",
) -> list[dict[str, Any]]:
    """Map intake LLM SignalExtractionResult dict to mailbox fact rows (no extra LLM call)."""
    if not isinstance(hvac_signals, dict) or not hvac_signals:
        return []
    facts: list[dict[str, Any]] = []
    building = str(hvac_signals.get("building_type") or "").strip()
    if building:
        facts.append(
            _build_fact(
                case_id,
                message_id,
                document_id,
                entity_scope,
                "building_type",
                building,
                building,
                0.85,
                observed_at,
                source_type,
                source_ref,
                metadata,
            )
        )
    area = hvac_signals.get("heated_area_m2")
    if area is not None:
        try:
            area_val = float(area)
        except (TypeError, ValueError):
            area_val = None
        if area_val is not None and area_val > 0:
            normalized = f"{area_val:g}"
            facts.append(
                _build_fact(
                    case_id,
                    message_id,
                    document_id,
                    entity_scope,
                    "heated_area_m2",
                    normalized,
                    str(area),
                    0.8,
                    observed_at,
                    source_type,
                    source_ref,
                    metadata,
                )
            )
    geo = str(hvac_signals.get("raw_geographic_signal") or "").strip()
    if len(geo) >= 3:
        facts.append(
            _build_fact(
                case_id,
                message_id,
                document_id,
                entity_scope,
                "city",
                geo,
                geo,
                0.7,
                observed_at,
                source_type,
                source_ref,
                metadata,
            )
        )
    heating = str(hvac_signals.get("current_heating_source") or "").strip()
    if heating:
        facts.append(
            _build_fact(
                case_id,
                message_id,
                document_id,
                entity_scope,
                "current_heating_source",
                heating,
                heating,
                0.75,
                observed_at,
                source_type,
                source_ref,
                metadata,
            )
        )
    return dedupe_fact_rows(facts)


def _regon_9_check_digit(digits: str) -> int:
    total = sum(int(digits[i]) * _REGON_9_WEIGHTS[i] for i in range(8))
    check = total % 11
    return 0 if check == 10 else check


def _is_valid_regon_9(digits: str) -> bool:
    if len(digits) != 9 or not digits.isdigit():
        return False
    return _regon_9_check_digit(digits) == int(digits[8])


def _nip_check_digit(digits: str) -> int:
    total = sum(int(digits[i]) * _NIP_WEIGHTS[i] for i in range(9))
    check = total % 11
    return 0 if check == 10 else check


def _is_valid_nip_10(digits: str) -> bool:
    if len(digits) != 10 or not digits.isdigit():
        return False
    return _nip_check_digit(digits) == int(digits[9])


def _has_registry_label_before(body: str, start: int) -> bool:
    window = body[max(0, start - 120):start]
    return bool(_REGISTRY_LABEL_CONTEXT_RE.search(window))


def _looks_like_reference_token(body: str, match: re.Match[str]) -> bool:
    """Skip hyphenated offer/reference tokens (e.g. 35631341-001) mistaken for phones."""
    if _PHONE_LABEL_CONTEXT_RE.search(body[max(0, match.start() - 80):match.start()]):
        return False
    context = body[max(0, match.start() - 16):min(len(body), match.end() + 16)]
    if re.search(r"\d{5,}[-/]\d{2,}", context):
        return True
    prefix = body[max(0, match.start() - 12):match.start()]
    return bool(re.search(r"[-/]\s*$", prefix))


def _should_extract_customer_phone(body: str, match: re.Match[str]) -> bool:
    digits = re.sub(r"\D+", "", match.group(1))
    if len(digits) < 9:
        return False
    if _has_registry_label_before(body, match.start()):
        return False
    if _looks_like_reference_token(body, match):
        return False
    if len(digits) == 9 and _is_valid_regon_9(digits):
        return False
    if digits.startswith("000") and len(digits) in {9, 10}:
        return False
    if len(digits) == 10 and _is_valid_nip_10(digits):
        return False
    return True


def extract_facts_from_text(
    *,
    case_id: str,
    message_id: str,
    document_id: str,
    text: str,
    source_type: str,
    source_ref: str,
    observed_at: str,
    entity_scope: str,
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    body = str(text or "")
    if not body.strip():
        return []
    facts: list[dict[str, Any]] = []
    for email in sorted({match.group("email") for match in EMAIL_RE.finditer(body) if _is_real_email(match.group("email"))}):
        facts.append(_build_fact(case_id, message_id, document_id, entity_scope, "customer_email", email, email, 0.95, observed_at, source_type, source_ref, metadata))
    for match in PHONE_RE.finditer(body):
        if not _should_extract_customer_phone(body, match):
            continue
        raw = re.sub(r"\D+", "", match.group(1))
        if len(raw) >= 9:
            facts.append(_build_fact(case_id, message_id, document_id, entity_scope, "customer_phone", raw, match.group(1), 0.82, observed_at, source_type, source_ref, metadata))
    for match in AREA_RE.finditer(body):
        normalized = match.group(1).replace(",", ".")
        facts.append(_build_fact(case_id, message_id, document_id, entity_scope, "heated_area_m2", normalized, match.group(0), 0.74, observed_at, source_type, source_ref, metadata))
    for match in CITY_HINT_RE.finditer(body):
        city = match.group(1).strip()
        if len(city) >= 3:
            facts.append(_build_fact(case_id, message_id, document_id, entity_scope, "city", city, city, 0.58, observed_at, source_type, source_ref, metadata))
    building = _extract_building_type_fact(
        body,
        case_id=case_id,
        message_id=message_id,
        document_id=document_id,
        entity_scope=entity_scope,
        observed_at=observed_at,
        source_type=source_type,
        source_ref=source_ref,
        metadata=metadata,
    )
    if building is not None:
        facts.append(building)
    power = _extract_power_kw_fact(
        body,
        case_id=case_id,
        message_id=message_id,
        document_id=document_id,
        entity_scope=entity_scope,
        observed_at=observed_at,
        source_type=source_type,
        source_ref=source_ref,
        metadata=metadata,
    )
    if power is not None:
        facts.append(power)
    return dedupe_fact_rows(facts)


def _extract_building_type_fact(
    body: str,
    *,
    case_id: str,
    message_id: str,
    document_id: str,
    entity_scope: str,
    observed_at: str,
    source_type: str,
    source_ref: str,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    best: tuple[float, str, str] | None = None
    for pattern, normalized, confidence in _BUILDING_TYPE_RULES:
        match = pattern.search(body)
        if not match:
            continue
        raw = match.group(0).strip()
        if best is None or confidence > best[0]:
            best = (confidence, normalized, raw)
    if best is None:
        return None
    confidence, normalized, raw = best
    return _build_fact(
        case_id,
        message_id,
        document_id,
        entity_scope,
        "building_type",
        normalized,
        raw,
        confidence,
        observed_at,
        source_type,
        source_ref,
        metadata,
    )


def _parse_power_kw_number(raw: str) -> float | None:
    try:
        value = float(str(raw).replace(",", ".").strip())
    except ValueError:
        return None
    if value < _POWER_KW_MIN or value > _POWER_KW_MAX:
        return None
    return value


def _format_power_kw(value: float) -> str:
    return f"{value:.1f}"


def _extract_power_kw_fact(
    body: str,
    *,
    case_id: str,
    message_id: str,
    document_id: str,
    entity_scope: str,
    observed_at: str,
    source_type: str,
    source_ref: str,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    best: tuple[float, str, str] | None = None
    for pattern, confidence in (
        (_POWER_KW_CONTEXTUAL_RE, 0.8),
        (_POWER_KW_POMPA_GLUED_RE, 0.8),
        (_POWER_KW_BARE_RE, 0.6),
    ):
        for match in pattern.finditer(body):
            parsed = _parse_power_kw_number(match.group(1))
            if parsed is None:
                continue
            normalized = _format_power_kw(parsed)
            raw = match.group(0).strip()
            if best is None or confidence > best[0]:
                best = (confidence, normalized, raw)
    if best is None:
        return None
    confidence, normalized, raw = best
    return _build_fact(
        case_id,
        message_id,
        document_id,
        entity_scope,
        "power_kw",
        normalized,
        raw,
        confidence,
        observed_at,
        source_type,
        source_ref,
        metadata,
    )


def extract_reference_facts(*, case_id: str, message_id: str, text: str, observed_at: str, entity_scope: str) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for token in sorted({match.group(1) for match in CASE_TOKEN_RE.finditer(str(text or ""))}):
        facts.append(_build_fact(case_id, message_id, "", entity_scope, "reference_token", token, token, 0.66, observed_at, "message", message_id, {"origin": "subject_reference"}))
    return dedupe_fact_rows(facts)


def dedupe_fact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        key = "::".join(
            (
                str(row.get("message_id") or ""),
                str(row.get("document_id") or ""),
                str(row.get("entity_scope") or ""),
                str(row.get("fact_key") or ""),
                str(row.get("normalized_value") or ""),
            )
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def stable_id(prefix: str, *parts: str) -> str:
    seed = "::".join(str(part or "").strip() for part in parts if str(part or "").strip()) or prefix
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _build_fact(
    case_id: str,
    message_id: str,
    document_id: str,
    entity_scope: str,
    fact_key: str,
    normalized_value: str,
    raw_value: str,
    confidence: float,
    observed_at: str,
    source_type: str,
    source_ref: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "fact_id": stable_id("fact", case_id, message_id, document_id, entity_scope, fact_key, normalized_value),
        "case_id": case_id,
        "message_id": message_id,
        "document_id": document_id,
        "entity_scope": entity_scope,
        "fact_key": fact_key,
        "normalized_value": normalized_value,
        "raw_value": raw_value,
        "confidence": round(max(0.0, min(1.0, float(confidence))), 4),
        "observed_at": observed_at,
        "source_type": source_type,
        "source_ref": source_ref,
        "status": "active",
        "metadata": metadata,
    }


def _extract_first_email(text: str) -> str:
    match = EMAIL_RE.search(str(text or ""))
    return match.group("email") if match else ""


def _guess_customer_name(sender: str) -> str:
    text = str(sender or "").strip()
    if "<" in text:
        text = text.split("<", 1)[0].strip().strip('"')
    if "@" in text and not text.replace("@", "").strip():
        return ""
    return text


def _looks_like_zip(*, file_name: str, mime_type: str) -> bool:
    name = str(file_name or "").lower()
    mime = str(mime_type or "").lower()
    return name.endswith(".zip") or "zip" in mime


def extract_zip_children(data: bytes, *, parent_anchor: str) -> tuple[list[dict[str, Any]], list[str]]:
    children: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            for index, info in enumerate(archive.infolist()):
                if index >= MAX_ZIP_CHILDREN:
                    warnings.append("zip_child_limit_reached")
                    break
                if info.is_dir():
                    continue
                if info.file_size > MAX_ZIP_CHILD_BYTES:
                    warnings.append(f"child_too_large:{info.filename}")
                    continue
                raw = archive.read(info)
                if not raw:
                    continue
                file_name = Path(info.filename).name
                children.append(
                    {
                        "document_id": stable_id("doc", parent_anchor, info.filename),
                        "file_name": file_name,
                        "mime_type": infer_child_mime(file_name),
                        "raw_bytes": raw,
                    }
                )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"zip_parse_failed:{type(exc).__name__}")
    return children, warnings


def infer_child_mime(file_name: str) -> str:
    name = str(file_name or "").lower()
    if name.endswith(".pdf"):
        return "application/pdf"
    if name.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if name.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if name.endswith(".xls"):
        return "application/vnd.ms-excel"
    if name.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if name.endswith(".png"):
        return "image/png"
    return ""


def _dedupe_texts(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


__all__ = [
    "MailboxMemoryRuntime",
    "apply_embeddings_to_chunk_rows",
    "build_case_context_pack",
    "build_case_snapshot",
    "build_document_chunks",
    "build_mailbox_memory_runtime",
    "derive_case_id",
    "derive_next_action_record",
    "extract_facts_from_text",
    "facts_from_hvac_signals",
    "extract_reference_facts",
    "rank_chunks",
    "split_conflicting_facts",
]
