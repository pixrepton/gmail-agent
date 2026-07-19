"""In-memory implementation of MailboxMemoryStore for unit tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from .protocol import MailboxMemoryStore
from .schema import _case_payload_with_defaults, _cosine_similarity, _parse_vector_literal_coords


@dataclass(slots=True)
class InMemoryMailboxMemoryStore:
    """Deterministic store for tests and dry-run memory trials."""

    cases: dict[str, dict[str, Any]] | None = None
    messages: dict[str, dict[str, Any]] | None = None
    attachments: dict[str, dict[str, Any]] | None = None
    documents: dict[str, dict[str, Any]] | None = None
    chunks: dict[str, list[dict[str, Any]]] | None = None
    events: list[dict[str, Any]] | None = None
    facts: dict[str, list[dict[str, Any]]] | None = None
    snapshots: dict[str, dict[str, Any]] | None = None
    case_snapshot_versions: dict[str, list[dict[str, Any]]] | None = None
    next_actions: dict[str, dict[str, Any]] | None = None
    drive_documents: dict[str, dict[str, Any]] | None = None
    drive_chunks: dict[str, list[dict[str, Any]]] | None = None
    drive_facts: dict[str, list[dict[str, Any]]] | None = None
    drive_runs: dict[str, dict[str, Any]] | None = None
    raw_observations: dict[str, dict[str, Any]] | None = None
    raw_observation_fingerprints: dict[str, str] | None = None
    signals: dict[str, dict[str, Any]] | None = None
    signal_idempotency: dict[str, str] | None = None
    signal_attempts: list[dict[str, Any]] | None = None
    source_cursors: dict[str, dict[str, Any]] | None = None
    action_proposals: dict[str, dict[str, Any]] | None = None
    execution_results: dict[str, dict[str, Any]] | None = None
    calendar_events: dict[str, dict[str, Any]] | None = None
    calendar_case_links: dict[str, dict[str, Any]] | None = None
    document_intelligence_results: dict[str, dict[str, Any]] | None = None
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.cases = self.cases or {}
        self.messages = self.messages or {}
        self.attachments = self.attachments or {}
        self.documents = self.documents or {}
        self.chunks = self.chunks or {}
        self.events = self.events or []
        self.facts = self.facts or {}
        self.snapshots = self.snapshots or {}
        self.case_snapshot_versions = self.case_snapshot_versions or {}
        self.next_actions = self.next_actions or {}
        self.drive_documents = self.drive_documents or {}
        self.drive_chunks = self.drive_chunks or {}
        self.drive_facts = self.drive_facts or {}
        self.drive_runs = self.drive_runs or {}
        self.raw_observations = self.raw_observations or {}
        self.raw_observation_fingerprints = self.raw_observation_fingerprints or {}
        self.signals = self.signals or {}
        self.signal_idempotency = self.signal_idempotency or {}
        self.signal_attempts = self.signal_attempts or []
        self.source_cursors = self.source_cursors or {}
        self.action_proposals = self.action_proposals or {}
        self.execution_results = self.execution_results or {}
        self.calendar_events = self.calendar_events or {}
        self.calendar_case_links = self.calendar_case_links or {}
        self.document_intelligence_results = self.document_intelligence_results or {}

    def bootstrap(self) -> None:
        return None

    def upsert_case(self, row: dict[str, Any]) -> None:
        case_id = str(row.get("case_id") or "").strip()
        if case_id:
            payload = _case_payload_with_defaults(row)
            payload["case_id"] = case_id
            with self._lock:
                self.cases[case_id] = payload

    def mutate_case(
        self,
        case_id: str,
        mutator,
        *,
        create_if_missing: bool = False,
    ) -> dict[str, Any]:
        cid = str(case_id or "").strip()
        if not cid:
            raise LookupError("case_id is required")
        with self._lock:
            current = self.cases.get(cid)
            if current is None and not create_if_missing:
                raise LookupError(f"case not found: {cid}")
            base = dict(current) if current is not None else {"case_id": cid, "metadata": {}}
            updated = mutator(dict(base))
            if not isinstance(updated, dict):
                raise RuntimeError("case mutator must return dict row")
            payload = _case_payload_with_defaults(updated)
            payload["case_id"] = cid
            self.cases[cid] = payload
            return dict(payload)

    def upsert_message(self, row: dict[str, Any]) -> None:
        message_id = str(row.get("message_id") or "").strip()
        if message_id:
            self.messages[message_id] = dict(row)

    def upsert_attachment(self, row: dict[str, Any]) -> None:
        attachment_id = str(row.get("attachment_id") or "").strip()
        if attachment_id:
            self.attachments[attachment_id] = dict(row)

    def upsert_document(self, row: dict[str, Any]) -> None:
        document_id = str(row.get("document_id") or "").strip()
        if document_id:
            self.documents[document_id] = dict(row)

    def replace_document_chunks(self, document_id: str, rows: list[dict[str, Any]]) -> None:
        self.chunks[document_id] = [dict(item) for item in rows]

    def append_event(self, row: dict[str, Any]) -> None:
        event_id = str(row.get("event_id") or "").strip()
        if event_id and not any(event_id == str(item.get("event_id") or "") for item in self.events):
            self.events.append(dict(row))

    def replace_message_facts(self, *, message_id: str, rows: list[dict[str, Any]]) -> None:
        self.facts[message_id] = [dict(item) for item in rows]

    def append_fact_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        bucket_key = f"append::{str(rows[0].get('case_id') or '')}:{str(rows[0].get('source_ref') or '')}"
        current = list(self.facts.get(bucket_key) or [])
        seen_fact_ids = {
            str(item.get("fact_id") or "")
            for items in self.facts.values()
            for item in items
            if str(item.get("fact_id") or "")
        }
        for row in rows:
            fact_id = str(row.get("fact_id") or "").strip()
            if fact_id and fact_id in seen_fact_ids:
                continue
            current.append(dict(row))
            if fact_id:
                seen_fact_ids.add(fact_id)
        self.facts[bucket_key] = current

    def upsert_snapshot(self, case_id: str, row: dict[str, Any]) -> None:
        self.snapshots[case_id] = dict(row)

    def append_case_snapshot_version(self, row: dict[str, Any]) -> None:
        case_id = str(row.get("case_id") or "").strip()
        snapshot_id = str(row.get("snapshot_id") or "").strip()
        if not case_id or not snapshot_id:
            return
        rows = self.case_snapshot_versions.setdefault(case_id, [])
        if any(str(item.get("snapshot_id") or "") == snapshot_id for item in rows):
            return
        rows.append(dict(row))
        rows.sort(key=lambda item: int(item.get("version") or 0))

    def fetch_case_snapshot_versions(self, case_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = [dict(item) for item in self.case_snapshot_versions.get(case_id, [])]
        rows.sort(key=lambda item: int(item.get("version") or 0))
        return rows[:limit]

    def fetch_latest_case_snapshot_version(self, case_id: str) -> dict[str, Any] | None:
        rows = self.fetch_case_snapshot_versions(case_id, limit=1_000)
        return rows[-1] if rows else None

    def upsert_next_action(self, case_id: str, row: dict[str, Any]) -> None:
        self.next_actions[case_id] = dict(row)

    def fetch_case(self, case_id: str) -> dict[str, Any] | None:
        item = self.cases.get(case_id)
        return dict(item) if item else None

    def fetch_resolved_cases_by_family_and_fact_keys(
        self,
        *,
        case_family: str,
        fact_keys: list[str],
        exclude_case_id: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        family = str(case_family or "").strip()
        keys = {str(k or "").strip() for k in (fact_keys or []) if str(k or "").strip()}
        exclude = str(exclude_case_id or "").strip()
        if not family or not keys:
            return []
        scored: list[tuple[int, dict[str, Any]]] = []
        for case_id, row in self.cases.items():
            if case_id == exclude:
                continue
            if str(row.get("status") or "") != "resolved":
                continue
            if str(row.get("case_family") or "") != family:
                continue
            case_keys = {
                str(f.get("fact_key") or "").strip()
                for f in self.facts.get(case_id, [])
                if str(f.get("fact_key") or "").strip()
            }
            overlap = len(case_keys & keys)
            if overlap <= 0:
                continue
            item = dict(row)
            item["overlap_count"] = overlap
            scored.append((overlap, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[: max(1, int(limit or 5))]]

    def fetch_case_by_message_id(self, message_id: str) -> dict[str, Any] | None:
        message = self.messages.get(message_id)
        if not message:
            return None
        return self.fetch_case(str(message.get("case_id") or "").strip())

    def fetch_message(self, message_id: str) -> dict[str, Any] | None:
        message_id = str(message_id or "").strip()
        if not message_id:
            return None
        item = self.messages.get(message_id)
        return dict(item) if item else None

    def fetch_any_message(self, *, order: str = "oldest") -> dict[str, Any] | None:
        if not self.messages:
            return None
        rows = [dict(item) for item in self.messages.values() if isinstance(item, dict)]
        if not rows:
            return None
        direction = str(order or "oldest").strip().lower()
        reverse = direction in {"newest", "latest", "desc"}

        def key(item: dict[str, Any]) -> tuple[str, str]:
            return (str(item.get("received_at") or ""), str(item.get("created_at") or ""))

        rows.sort(key=key, reverse=reverse)
        return rows[0] if rows else None

    def fetch_messages_for_case(self, case_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = [dict(item) for item in self.messages.values() if str(item.get("case_id") or "") == case_id]
        rows.sort(key=lambda item: str(item.get("received_at") or ""), reverse=True)
        return rows[:limit]

    def fetch_cases(self, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = [dict(item) for item in self.cases.values()]
        rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def fetch_events_for_case(self, case_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = [dict(item) for item in self.events if str(item.get("case_id") or "") == case_id]
        rows.sort(key=lambda item: str(item.get("occurred_at") or ""), reverse=True)
        return rows[:limit]

    def fetch_events(self, *, event_types: tuple[str, ...] = (), limit: int = 1000) -> list[dict[str, Any]]:
        allowed = set(event_types or ())
        rows = [dict(item) for item in self.events if not allowed or str(item.get("event_type") or "") in allowed]
        rows.sort(key=lambda item: str(item.get("occurred_at") or item.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def upsert_action_proposal(self, row: dict[str, Any]) -> None:
        proposal_id = str(row.get("proposal_id") or "").strip()
        if proposal_id:
            self.action_proposals[proposal_id] = dict(row)

    def fetch_action_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        item = self.action_proposals.get(str(proposal_id or "").strip())
        return dict(item) if item else None

    def fetch_action_proposals(self, *, case_id: str = "", status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        rows = [
            dict(item)
            for item in self.action_proposals.values()
            if (not case_id or str(item.get("case_id") or "") == case_id)
            and (not status or str(item.get("status") or "") == status)
        ]
        rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def upsert_execution_result(self, row: dict[str, Any]) -> None:
        execution_id = str(row.get("execution_id") or "").strip()
        if execution_id:
            self.execution_results[execution_id] = dict(row)

    def fetch_execution_results(self, *, case_id: str = "", proposal_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        rows = [
            dict(item)
            for item in self.execution_results.values()
            if (not case_id or str(item.get("case_id") or "") == case_id)
            and (not proposal_id or str(item.get("proposal_id") or "") == proposal_id)
        ]
        rows.sort(key=lambda item: str(item.get("executed_at") or ""), reverse=True)
        return rows[:limit]

    def upsert_calendar_event(self, row: dict[str, Any]) -> None:
        event_id = str(row.get("calendar_event_id") or "").strip()
        if event_id:
            self.calendar_events[event_id] = dict(row)

    def upsert_calendar_case_link(self, row: dict[str, Any]) -> None:
        key = f"{row.get('calendar_event_id')}:{row.get('case_id')}"
        self.calendar_case_links[key] = dict(row)

    def fetch_calendar_events_for_case(self, case_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = [dict(item) for item in self.calendar_events.values() if str(item.get("case_id") or "") == case_id]
        rows.sort(key=lambda item: str(item.get("start_at") or ""), reverse=False)
        return rows[:limit]

    def upsert_document_intelligence_result(self, row: dict[str, Any]) -> None:
        document_id = str(row.get("document_id") or "").strip()
        if document_id:
            self.document_intelligence_results[document_id] = dict(row)

    def fetch_document_intelligence_for_case(self, case_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = [dict(item) for item in self.document_intelligence_results.values() if str(item.get("case_id") or "") == case_id]
        rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def fetch_latest_adjudication_link_override(self, signal_id: str) -> dict[str, Any] | None:
        sid = str(signal_id or "").strip()
        if not sid:
            return None
        for ev in reversed(self.events):
            if str(ev.get("event_type") or "") != "adjudication_link_override":
                continue
            payload = ev.get("payload")
            if not isinstance(payload, dict):
                continue
            if str(payload.get("signal_id") or "") == sid:
                return dict(payload)
        return None

    def fetch_facts_for_case(self, case_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for items in self.facts.values():
            for item in items:
                if str(item.get("case_id") or "") == case_id:
                    rows.append(dict(item))
        rows.sort(key=lambda item: (str(item.get("fact_key") or ""), -float(item.get("confidence") or 0.0)))
        return rows

    def fetch_documents_for_case(self, case_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = [dict(item) for item in self.documents.values() if str(item.get("case_id") or "") == case_id]
        rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def fetch_chunks_for_case(self, case_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for document_id, items in self.chunks.items():
            doc = self.documents.get(document_id) or {}
            if str(doc.get("case_id") or "") != case_id:
                continue
            rows.extend(dict(item) for item in items)
        rows.sort(key=lambda item: (str(item.get("document_id") or ""), int(item.get("ordinal") or 0)))
        return rows[:limit]

    def fetch_snapshot(self, case_id: str) -> dict[str, Any] | None:
        item = self.snapshots.get(case_id)
        return dict(item) if item else None

    def fetch_next_action(self, case_id: str) -> dict[str, Any] | None:
        item = self.next_actions.get(case_id)
        return dict(item) if item else None

    def upsert_drive_document(self, row: dict[str, Any]) -> None:
        document_id = str(row.get("document_id") or "").strip()
        if document_id:
            self.drive_documents[document_id] = dict(row)
            case_id = str(row.get("case_id") or "").strip()
            if case_id:
                for chunk in self.drive_chunks.get(document_id, []):
                    chunk["case_id"] = case_id

    def replace_drive_document_chunks(self, *, document_id: str, rows: list[dict[str, Any]]) -> None:
        self.drive_chunks[document_id] = [dict(item) for item in rows]

    def replace_drive_document_facts(self, *, document_id: str, rows: list[dict[str, Any]]) -> None:
        self.drive_facts[document_id] = [dict(item) for item in rows]

    def upsert_drive_ingest_run(self, row: dict[str, Any]) -> None:
        run_id = str(row.get("run_id") or "").strip()
        if run_id:
            self.drive_runs[run_id] = dict(row)

    def fetch_drive_documents_for_case(self, case_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = [dict(item) for item in self.drive_documents.values() if str(item.get("case_id") or "") == case_id]
        rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def fetch_drive_chunks_for_case(self, case_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for document_id, items in self.drive_chunks.items():
            doc = self.drive_documents.get(document_id) or {}
            if str(doc.get("case_id") or "") != case_id:
                continue
            rows.extend(dict(item) for item in items)
        rows.sort(key=lambda item: (str(item.get("document_id") or ""), int(item.get("ordinal") or 0)))
        return rows[:limit]

    def fetch_semantic_chunk_candidates_for_case(
        self, case_id: str, query_vector_literal: str, *, limit_mailbox: int = 50, limit_drive: int = 50
    ) -> list[dict[str, Any]]:
        query_vec = _parse_vector_literal_coords(query_vector_literal)
        if not query_vec:
            return []
        lim_m = max(1, int(limit_mailbox))
        lim_d = max(1, int(limit_drive))

        def _score_chunks(chunks: list[dict[str, Any]]) -> list[tuple[float, dict[str, Any]]]:
            scored_local: list[tuple[float, dict[str, Any]]] = []
            for chunk in chunks:
                emb = chunk.get("embedding")
                if not isinstance(emb, (list, tuple)):
                    continue
                if str(chunk.get("embedding_status") or "") != "ready":
                    continue
                try:
                    vec = [float(x) for x in emb]
                except (TypeError, ValueError):
                    continue
                sim = _cosine_similarity(query_vec, vec)
                row = dict(chunk)
                row["vector_similarity"] = sim
                scored_local.append((sim, row))
            scored_local.sort(key=lambda item: item[0], reverse=True)
            return scored_local

        mb = _score_chunks([dict(c) for c in self.fetch_chunks_for_case(case_id, limit=500)])
        out = [row for _, row in mb[:lim_m]]
        dr = _score_chunks([dict(c) for c in self.fetch_drive_chunks_for_case(case_id, limit=500)])
        out.extend(row for _, row in dr[:lim_d])
        return out

    def fetch_drive_facts_for_case(self, case_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for items in self.drive_facts.values():
            for item in items:
                if str(item.get("case_id") or "") == case_id:
                    rows.append(dict(item))
        rows.sort(key=lambda item: (str(item.get("fact_family") or ""), str(item.get("fact_key") or ""), -float(item.get("confidence") or 0.0)))
        return rows

    def fetch_drive_facts_for_document(self, document_id: str) -> list[dict[str, Any]]:
        rows = [dict(item) for item in self.drive_facts.get(document_id, [])]
        rows.sort(key=lambda item: (str(item.get("fact_family") or ""), str(item.get("fact_key") or ""), -float(item.get("confidence") or 0.0)))
        return rows

    def fetch_drive_documents(self, *, limit: int = 100, scopes: tuple[str, ...] = (), lanes: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        rows = [dict(item) for item in self.drive_documents.values()]
        if scopes:
            allowed_scopes = {str(item) for item in scopes}
            rows = [row for row in rows if str(row.get("scope") or "") in allowed_scopes]
        if lanes:
            allowed_lanes = {str(item) for item in lanes}
            rows = [row for row in rows if str(row.get("lane") or "") in allowed_lanes]
        rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def fetch_drive_document_by_item_id(self, drive_item_id: str) -> dict[str, Any] | None:
        for row in self.drive_documents.values():
            if str(row.get("drive_item_id") or "") == drive_item_id:
                return dict(row)
        return None

    def append_raw_observation(self, row: dict[str, Any]) -> bool:
        observation_id = str(row.get("observation_id") or "").strip()
        source_fingerprint = str(row.get("source_fingerprint") or "").strip()
        if not observation_id or not source_fingerprint:
            return False
        existing_observation_id = self.raw_observation_fingerprints.get(source_fingerprint)
        if existing_observation_id:
            return False
        self.raw_observations[observation_id] = dict(row)
        self.raw_observation_fingerprints[source_fingerprint] = observation_id
        return True

    def fetch_raw_observation(self, observation_id: str) -> dict[str, Any] | None:
        item = self.raw_observations.get(observation_id)
        return dict(item) if item else None

    def fetch_raw_observation_by_source_fingerprint(self, source_fingerprint: str) -> dict[str, Any] | None:
        observation_id = self.raw_observation_fingerprints.get(source_fingerprint)
        if not observation_id:
            return None
        return self.fetch_raw_observation(observation_id)

    def fetch_raw_observations_for_source(self, source_kind: str, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = [
            dict(item)
            for item in self.raw_observations.values()
            if str(item.get("source_kind") or "").strip() == source_kind
        ]
        rows.sort(key=lambda item: (str(item.get("observed_at") or ""), str(item.get("created_at") or "")))
        return rows[:limit]

    def append_signal(self, row: dict[str, Any]) -> bool:
        signal_id = str(row.get("signal_id") or "").strip()
        idempotency_key = str(row.get("idempotency_key") or "").strip()
        if not signal_id or not idempotency_key:
            return False
        existing_signal_id = self.signal_idempotency.get(idempotency_key)
        if existing_signal_id:
            return False
        self.signals[signal_id] = dict(row)
        self.signal_idempotency[idempotency_key] = signal_id
        return True

    def fetch_signal(self, signal_id: str) -> dict[str, Any] | None:
        item = self.signals.get(signal_id)
        return dict(item) if item else None

    def patch_signal_engagement_id(self, signal_id: str, engagement_id: str) -> bool:
        item = self.signals.get(str(signal_id or "").strip())
        if not item:
            return False
        item["engagement_id"] = str(engagement_id or "").strip()
        return True

    def fetch_signal_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        signal_id = self.signal_idempotency.get(idempotency_key)
        if not signal_id:
            return None
        return self.fetch_signal(signal_id)

    def fetch_signals_for_case(self, case_id: str = "", *, case_key_hint: str = "", limit: int = 200) -> list[dict[str, Any]]:
        rows = []
        for item in self.signals.values():
            payload = dict(item)
            payload_case_id = str((payload.get("payload_json") or {}).get("case_id") or "").strip()
            if case_id and payload_case_id != case_id:
                continue
            if case_key_hint and str(payload.get("case_key_hint") or "").strip() != case_key_hint:
                continue
            rows.append(payload)
        rows.sort(key=lambda item: (str(item.get("observed_at") or ""), str(item.get("created_at") or "")))
        return rows[:limit]

    def fetch_signals_for_source(self, source_kind: str, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = [
            dict(item)
            for item in self.signals.values()
            if str(item.get("source_kind") or "").strip() == source_kind
        ]
        rows.sort(key=lambda item: (str(item.get("observed_at") or ""), str(item.get("created_at") or "")))
        return rows[:limit]

    def append_signal_processing_attempt(self, row: dict[str, Any]) -> None:
        self.signal_attempts.append(dict(row))

    def fetch_signal_processing_attempts(self, signal_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = [
            dict(item)
            for item in self.signal_attempts
            if str(item.get("signal_id") or "").strip() == signal_id
        ]
        rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def upsert_source_cursor(self, row: dict[str, Any]) -> None:
        cursor_key = str(row.get("cursor_key") or "").strip()
        if cursor_key:
            self.source_cursors[cursor_key] = dict(row)

    def fetch_source_cursor(self, source_kind: str, cursor_scope: str) -> dict[str, Any] | None:
        cursor_key = f"{source_kind}:{cursor_scope}"
        item = self.source_cursors.get(cursor_key)
        return dict(item) if item else None

    def list_source_cursors(self) -> list[dict[str, Any]]:
        rows = [dict(item) for item in self.source_cursors.values()]
        rows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return rows
