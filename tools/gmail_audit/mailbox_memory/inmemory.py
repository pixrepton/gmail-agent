"""In-memory implementation of MailboxMemoryStore for unit tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from .protocol import MailboxMemoryStore
from .schema import _case_payload_with_defaults, _cosine_similarity, _parse_vector_literal_coords
from .facts import attach_subject_metadata, fact_subject_ref, merge_fact_evidence, proposition_identity, subject_supersession_allowed


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
    thread_memories: dict[str, dict[str, Any]] | None = None
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
    policy_decisions: dict[str, dict[str, Any]] | None = None
    action_proposals_v2: dict[str, dict[str, Any]] | None = None
    execution_results: dict[str, dict[str, Any]] | None = None
    calendar_events: dict[str, dict[str, Any]] | None = None
    calendar_case_links: dict[str, dict[str, Any]] | None = None
    document_intelligence_results: dict[str, dict[str, Any]] | None = None
    decision_revisions: dict[str, dict[str, Any]] | None = None
    decision_revision_requests: dict[str, dict[str, Any]] | None = None
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
        self.thread_memories = self.thread_memories or {}
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
        self.policy_decisions = self.policy_decisions or {}
        self.action_proposals_v2 = self.action_proposals_v2 or {}
        self.execution_results = self.execution_results or {}
        self.calendar_events = self.calendar_events or {}
        self.calendar_case_links = self.calendar_case_links or {}
        self.document_intelligence_results = self.document_intelligence_results or {}
        self.decision_revisions = self.decision_revisions or {}
        self.decision_revision_requests = self.decision_revision_requests or {}

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
        """Replace one message's extract snapshot, then apply canonical supersession.

        Cross-message actives for the same logical identity are superseded.
        Distinct values *within* the same source snapshot remain dual-active
        (legal conflict). Separately promoted ``structured_document_parse`` rows for
        the same message_id are preserved and may legally conflict with extract rows.
        Snapshot retire + write are atomic for InMemory.
        """
        mid = str(message_id or "").strip()
        snapshot = {key: [dict(item) for item in items] for key, items in self.facts.items()}
        try:
            for bucket_key, items in list(self.facts.items()):
                kept = [
                    item
                    for item in items
                    if not (
                        str(item.get("message_id") or "") == mid
                        and str(item.get("source_type") or "") != "structured_document_parse"
                    )
                ]
                if kept:
                    self.facts[bucket_key] = kept
                else:
                    self.facts.pop(bucket_key, None)
            # Legacy bucket keyed by message_id holds extract snapshot only.
            if mid in self.facts:
                kept_mid = [
                    item
                    for item in self.facts[mid]
                    if str(item.get("source_type") or "") == "structured_document_parse"
                ]
                if kept_mid:
                    self.facts[mid] = kept_mid
                else:
                    self.facts.pop(mid, None)
            if rows:
                self._apply_replaced_message_fact_rows(mid, [dict(item) for item in rows])
        except Exception:
            self.facts = snapshot
            raise

    def _apply_replaced_message_fact_rows(self, message_id: str, rows: list[dict[str, Any]]) -> None:
        mid = str(message_id or "").strip()
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for row in rows:
            payload = attach_subject_metadata(dict(row))
            payload.setdefault("status", "active")
            case_id = str(payload.get("case_id") or "").strip()
            fact_key = str(payload.get("fact_key") or "").strip()
            if not case_id or not fact_key:
                continue
            groups.setdefault(proposition_identity(payload), []).append(payload)

        for identity, group_rows in groups.items():
            case_id = str(group_rows[0].get("case_id") or "").strip()
            distinct_values = {
                str(item.get("normalized_value") or "").strip() for item in group_rows
            }
            winner_id = str(group_rows[0].get("fact_id") or "")
            observed_at = group_rows[0].get("observed_at")
            # Retire other-message actives with different values. Same-message siblings
            # (e.g. structured_document_parse) remain as legal conflicts.
            for bucket_key, items in list(self.facts.items()):
                updated: list[dict[str, Any]] = []
                for item in items:
                    if (
                        str(item.get("case_id") or "") == case_id
                        and proposition_identity(item) == identity
                        and str(item.get("status") or "active") == "active"
                        and str(item.get("message_id") or "") != mid
                        and (
                            len(distinct_values) != 1
                            or str(item.get("normalized_value") or "").strip() not in distinct_values
                        )
                    ):
                        meta = dict(item.get("metadata") or {})
                        meta["superseded_at"] = observed_at
                        meta["superseded_by_fact_id"] = winner_id
                        meta["supersede_reason"] = "replace_message_facts"
                        updated.append({**item, "status": "superseded", "metadata": meta})
                    else:
                        updated.append(item)
                self.facts[bucket_key] = updated
            for payload in group_rows:
                # Idempotent: skip insert when an active same-message/same-value already exists.
                new_value = str(payload.get("normalized_value") or "").strip()
                already = False
                for items in self.facts.values():
                    for item in items:
                        if (
                            str(item.get("case_id") or "") == case_id
                            and proposition_identity(item) == identity
                            and str(item.get("status") or "active") == "active"
                            and str(item.get("normalized_value") or "").strip() == new_value
                            and str(item.get("message_id") or "") == mid
                            and str(item.get("fact_id") or "") == str(payload.get("fact_id") or "")
                        ):
                            already = True
                            break
                    if already:
                        break
                if already:
                    continue
                bucket_key = mid or f"append::{case_id}:{str(payload.get('source_ref') or '')}"
                current = list(self.facts.get(bucket_key) or [])
                current.append(payload)
                self.facts[bucket_key] = current

    def reassign_case_facts(self, *, source_case_id: str, target_case_id: str) -> dict[str, int]:
        """Move facts from source case to target and reconcile dual-active identities."""
        source = str(source_case_id or "").strip()
        target = str(target_case_id or "").strip()
        moved = 0
        if not source or not target or source == target:
            return {"moved": 0, "reconciled": 0}
        for bucket_key, items in list(self.facts.items()):
            updated: list[dict[str, Any]] = []
            for item in items:
                row = dict(item)
                if str(row.get("case_id") or "") == source:
                    row["case_id"] = target
                    moved += 1
                updated.append(row)
            self.facts[bucket_key] = updated
        reconciled = self.reconcile_active_fact_identities(target)
        return {"moved": moved, "reconciled": reconciled}

    def reconcile_active_fact_identities(self, case_id: str) -> int:
        """Keep newest active row per (entity_scope, fact_key); supersede the rest."""
        cid = str(case_id or "").strip()
        if not cid:
            return 0
        groups: dict[tuple[str, ...], list[tuple[str, int, dict[str, Any]]]] = {}
        for bucket_key, items in self.facts.items():
            for idx, item in enumerate(items):
                if str(item.get("case_id") or "") != cid:
                    continue
                if str(item.get("status") or "active") == "superseded":
                    continue
                fact_key = str(item.get("fact_key") or "").strip()
                if not fact_key:
                    continue
                groups.setdefault(proposition_identity(item), []).append((bucket_key, idx, item))
        reconciled = 0
        for _identity, entries in groups.items():
            if len(entries) < 2:
                continue
            entries_sorted = sorted(
                entries,
                key=lambda entry: str(entry[2].get("observed_at") or ""),
                reverse=True,
            )
            winner = entries_sorted[0]
            winner_id = str(winner[2].get("fact_id") or "")
            for bucket_key, idx, item in entries_sorted[1:]:
                meta = dict(item.get("metadata") or {})
                meta["superseded_at"] = winner[2].get("observed_at")
                meta["superseded_by_fact_id"] = winner_id
                meta["supersede_reason"] = "reconcile_active_fact_identities"
                self.facts[bucket_key][idx] = {**item, "status": "superseded", "metadata": meta}
                reconciled += 1
        return reconciled

    def append_fact_rows(self, rows: list[dict[str, Any]]) -> None:
        self.append_facts_with_supersession(rows)

    def append_facts_with_supersession(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        stats = {"inserted": 0, "superseded": 0, "unchanged": 0}
        if not rows:
            return stats
        for row in rows:
            payload = attach_subject_metadata(dict(row))
            case_id = str(payload.get("case_id") or "").strip()
            fact_key = str(payload.get("fact_key") or "").strip()
            new_value = str(payload.get("normalized_value") or "").strip()
            if not case_id or not fact_key:
                continue
            new_identity = proposition_identity(payload)
            new_subject_ref = fact_subject_ref(payload)
            skip_insert = False
            for bucket_key, items in list(self.facts.items()):
                updated_items: list[dict[str, Any]] = []
                for item in items:
                    if (
                        str(item.get("case_id") or "") == case_id
                        and proposition_identity(item) == new_identity
                        and str(item.get("status") or "active") == "active"
                    ):
                        old_value = str(item.get("normalized_value") or "").strip()
                        if old_value == new_value:
                            stats["unchanged"] += 1
                            skip_insert = True
                            merged_meta = merge_fact_evidence(item.get("metadata"), payload)
                            if merged_meta != (item.get("metadata") if isinstance(item.get("metadata"), dict) else {}):
                                item = {**item, "metadata": merged_meta}
                            updated_items.append(item)
                            continue
                        if (
                            new_subject_ref is not None
                            and new_subject_ref.kind != "CASE"
                            and not subject_supersession_allowed(payload)
                        ):
                            updated_items.append(item)
                            continue
                        meta = dict(item.get("metadata") or {})
                        meta["superseded_at"] = payload.get("observed_at")
                        meta["superseded_by_fact_id"] = str(payload.get("fact_id") or "")
                        updated_items.append({**item, "status": "superseded", "metadata": meta})
                        stats["superseded"] += 1
                    else:
                        updated_items.append(item)
                self.facts[bucket_key] = updated_items
            if skip_insert:
                continue
            bucket_key = f"append::{case_id}:{str(payload.get('source_ref') or '')}"
            current = list(self.facts.get(bucket_key) or [])
            current.append(payload)
            self.facts[bucket_key] = current
            stats["inserted"] += 1
        return stats

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

    def upsert_thread_memory(self, row: dict[str, Any], *, only_if_absent: bool = False) -> None:
        thread_id = str(row.get("thread_id") or "").strip()
        if not thread_id:
            return
        with self._lock:
            existing = self.thread_memories.get(thread_id)
            if existing is not None and only_if_absent:
                return
            payload = dict(row)
            payload["thread_id"] = thread_id
            if existing is None:
                payload["version"] = 1
                payload.setdefault("created_at", payload.get("updated_at"))
            else:
                same_content = str(existing.get("memory_sha256") or "") == str(payload.get("memory_sha256") or "")
                payload["version"] = int(existing.get("version") or 1) + (0 if same_content else 1)
                payload["created_at"] = existing.get("created_at")
                if same_content:
                    payload["updated_at"] = existing.get("updated_at")
            self.thread_memories[thread_id] = payload

    def fetch_thread_memory(self, thread_id: str) -> dict[str, Any] | None:
        item = self.thread_memories.get(str(thread_id or "").strip())
        return dict(item) if item else None

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
                and str(f.get("status") or "active") != "superseded"
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

    def append_policy_decision(self, row: dict[str, Any]) -> bool:
        policy_decision_id = str(row.get("policy_decision_id") or "").strip()
        if not policy_decision_id:
            return False
        with self._lock:
            if policy_decision_id in self.policy_decisions:
                return False
            self.policy_decisions[policy_decision_id] = dict(row)
            return True

    def fetch_policy_decision(self, policy_decision_id: str) -> dict[str, Any] | None:
        item = self.policy_decisions.get(str(policy_decision_id or "").strip())
        return dict(item) if item else None

    def fetch_policy_decisions(
        self,
        *,
        case_id: str = "",
        source_signal_id: str = "",
        source_message_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = [
            dict(item)
            for item in self.policy_decisions.values()
            if (not case_id or str(item.get("case_id") or "") == case_id)
            and (
                not source_signal_id
                or str(item.get("source_signal_id") or "") == source_signal_id
            )
            and (
                not source_message_id
                or str(item.get("source_message_id") or "") == source_message_id
            )
        ]
        rows.sort(key=lambda item: str(item.get("generated_at") or ""), reverse=True)
        return rows[:limit]

    def append_action_proposal_v2(self, row: dict[str, Any]) -> bool:
        proposal_id = str(row.get("proposal_id") or "").strip()
        if not proposal_id:
            return False
        with self._lock:
            if proposal_id in self.action_proposals_v2:
                return False
            self.action_proposals_v2[proposal_id] = dict(row)
            return True

    def fetch_action_proposal_v2(self, proposal_id: str) -> dict[str, Any] | None:
        item = self.action_proposals_v2.get(str(proposal_id or "").strip())
        return dict(item) if item else None

    def fetch_action_proposals_v2(
        self,
        *,
        case_id: str = "",
        source_signal_id: str = "",
        source_message_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = [
            dict(item)
            for item in self.action_proposals_v2.values()
            if (not case_id or str(item.get("case_id") or "") == case_id)
            and (
                not source_signal_id
                or str(item.get("source_signal_id") or "") == source_signal_id
            )
            and (
                not source_message_id
                or str(item.get("source_message_id") or "") == source_message_id
            )
        ]
        rows.sort(key=lambda item: str(item.get("generated_at") or ""), reverse=True)
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

    def fetch_active_facts_for_case(self, case_id: str) -> list[dict[str, Any]]:
        rows = [
            item
            for item in self.fetch_facts_for_case(case_id)
            if str(item.get("status") or "active") != "superseded"
        ]
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

    # -- DecisionRevision lineage (P1.1P durable state) --------------------

    def append_decision_revision(self, row: dict[str, Any]) -> None:
        """Persist one CAD revision row (idempotent per decision_version_id)."""
        version_id = str(row.get("decision_version_id") or "").strip()
        if not version_id:
            return
        with self._lock:
            existing = self.decision_revisions.get(version_id)
            if existing is not None:
                return
            payload = dict(row)
            payload.setdefault("revision_status", "CURRENT")
            payload.setdefault("supersedes_version_id", "")
            payload.setdefault("superseded_by_version_id", "")
            self.decision_revisions[version_id] = payload

    def append_decision_revision_request(self, row: dict[str, Any]) -> None:
        """Upsert one revision request row (idempotent per request_id)."""
        request_id = str(row.get("request_id") or "").strip()
        if not request_id:
            return
        with self._lock:
            payload = dict(row)
            payload.setdefault("reject_reason", "")
            existing = self.decision_revision_requests.get(request_id)
            if existing is not None:
                existing = dict(existing)
                existing["status"] = payload.get("status") or existing.get("status") or "PENDING"
                existing["reject_reason"] = payload.get("reject_reason") or existing.get("reject_reason") or ""
                self.decision_revision_requests[request_id] = existing
                return
            self.decision_revision_requests[request_id] = payload

    def accept_decision_revision_transition(
        self, *, old_cad: dict[str, Any], new_cad: dict[str, Any], request: dict[str, Any]
    ) -> None:
        """Atomically persist old -> SUPERSEDED, new -> CURRENT, request -> ACCEPTED."""
        old_version = str(old_cad.get("decision_version_id") or "").strip()
        new_version = str(new_cad.get("decision_version_id") or "").strip()
        request_id = str(request.get("request_id") or "").strip()
        decision_id = str(old_cad.get("decision_id") or "").strip()
        if not decision_id or not old_version or not new_version or not request_id:
            raise ValueError(
                "accept_decision_revision_transition requires decision_id, old/new version ids and request_id"
            )
        with self._lock:
            old_row = self.decision_revisions.get(old_version)
            if old_row is None or str(old_row.get("revision_status") or "") != "CURRENT":
                raise RuntimeError("decision_revision_conflict: old CAD no longer CURRENT")
            if new_version in self.decision_revisions:
                raise RuntimeError(f"decision_revision_conflict: version already exists: {new_version}")
            self.decision_revisions[old_version] = {
                **old_row,
                "revision_status": "SUPERSEDED",
                "superseded_by_version_id": new_version,
            }
            new_row = dict(new_cad)
            new_row["revision_status"] = "CURRENT"
            new_row["supersedes_version_id"] = old_version
            self.decision_revisions[new_version] = new_row
            existing_request = self.decision_revision_requests.get(request_id)
            if existing_request is not None:
                self.decision_revision_requests[request_id] = {
                    **existing_request,
                    "status": "ACCEPTED",
                }
            else:
                self.decision_revision_requests[request_id] = {
                    **dict(request),
                    "status": "ACCEPTED",
                    "reject_reason": "",
                }

    def fetch_decision_revisions(self, decision_id: str) -> list[dict[str, Any]]:
        rows = [
            dict(item)
            for item in self.decision_revisions.values()
            if str(item.get("decision_id") or "").strip() == str(decision_id or "").strip()
        ]
        rows.sort(key=lambda item: int(item.get("revision") or 0))
        return rows

    def fetch_decision_revision_requests(self, decision_id: str) -> list[dict[str, Any]]:
        rows = [
            dict(item)
            for item in self.decision_revision_requests.values()
            if str(item.get("decision_id") or "").strip() == str(decision_id or "").strip()
        ]
        rows.sort(key=lambda item: str(item.get("requested_at") or item.get("created_at") or ""))
        return rows

    def list_decision_lineage_ids(self) -> list[str]:
        return sorted(
            {
                str(item.get("decision_id") or "").strip()
                for item in self.decision_revisions.values()
                if str(item.get("decision_id") or "").strip()
            }
        )
