"""Persistence for correlation registry."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from correlation_registry.link_types import normalize_link_type
from correlation_registry.schema import CORRELATION_REGISTRY_SCHEMA_SQL

log = logging.getLogger(__name__)

POSTGRES_CONNECT_TIMEOUT_SEC = 15
DEFAULT_ENGAGEMENT_WINDOW_DAYS = 30


class RegistryLinkConflictError(ValueError):
    """mailbox_case link already points at a different engagement_id."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def _new_uuid() -> str:
    return str(uuid.uuid4())


class CorrelationRegistryStore(Protocol):
    def bootstrap(self) -> None: ...

    def create_identity(self, *, email: str, display_name: str = "", metadata: dict[str, Any] | None = None) -> str: ...

    def merge_identity_metadata(self, identity_id: str, metadata: dict[str, Any]) -> bool: ...

    def merge_engagement_metadata(self, engagement_id: str, metadata: dict[str, Any]) -> bool: ...

    def update_identity_display_name(self, identity_id: str, display_name: str) -> None: ...

    def update_identity_primary_email(self, *, identity_id: str, email: str) -> bool: ...

    def find_recent_engagement_for_email(
        self,
        *,
        email: str,
        within_days: int = DEFAULT_ENGAGEMENT_WINDOW_DAYS,
        identity_id: str = "",
    ) -> str | None: ...

    def resolve_or_create_engagement(
        self,
        *,
        identity_id: str,
        anchor_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str: ...

    def find_engagement_by_link(self, *, link_type: str, target_id: str, source_repo: str = "") -> str | None: ...

    def find_engagement_for_identity_recent(
        self,
        *,
        identity_id: str,
        within_days: int = DEFAULT_ENGAGEMENT_WINDOW_DAYS,
    ) -> str | None: ...

    def upsert_link(
        self,
        *,
        engagement_id: str,
        link_type: str,
        target_id: str,
        source_repo: str,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def list_links_for_engagement(self, engagement_id: str) -> list[dict[str, Any]]: ...

    def get_engagement(self, engagement_id: str) -> dict[str, Any] | None: ...

    def get_identity(self, identity_id: str) -> dict[str, Any] | None: ...

    def find_engagement_by_case_id(self, case_id: str) -> dict[str, Any] | None: ...

    def find_identity_by_email(self, email: str) -> dict[str, Any] | None: ...


@dataclass(slots=True)
class InMemoryCorrelationRegistryStore:
    identities: dict[str, dict[str, Any]] = field(default_factory=dict)
    identity_ids_by_email: dict[str, list[str]] = field(default_factory=dict)
    engagements: dict[str, dict[str, Any]] = field(default_factory=dict)
    links: dict[str, dict[str, Any]] = field(default_factory=dict)
    link_index: dict[tuple[str, str, str], str] = field(default_factory=dict)
    binding_suggestions: dict[str, dict[str, Any]] = field(default_factory=dict)
    merge_logs: list[dict[str, Any]] = field(default_factory=list)

    def bootstrap(self) -> None:
        return None

    def create_identity(self, *, email: str, display_name: str = "", metadata: dict[str, Any] | None = None) -> str:
        from correlation_registry.identity_metadata import normalize_identity_metadata

        normalized = _normalize_email(email)
        if not normalized:
            raise ValueError("email required for identity")
        identity_id = _new_uuid()
        ts = _now_iso()
        meta = normalize_identity_metadata(metadata, email=normalized, display_name=display_name or "")
        self.identities[identity_id] = {
            "identity_id": identity_id,
            "primary_email": normalized,
            "display_name": display_name or "",
            "metadata": meta,
            "created_at": ts,
            "updated_at": ts,
        }
        self.identity_ids_by_email.setdefault(normalized, []).append(identity_id)
        return identity_id

    def merge_identity_metadata(self, identity_id: str, metadata: dict[str, Any]) -> bool:
        from correlation_registry.identity_metadata import merge_identity_metadata

        row = self.identities.get(identity_id)
        if not row:
            return False
        row["metadata"] = merge_identity_metadata(
            row.get("metadata"),
            email=str(row.get("primary_email") or ""),
            display_name=str(row.get("display_name") or ""),
            hints=metadata,
        )
        row["updated_at"] = _now_iso()
        return True

    def merge_engagement_metadata(self, engagement_id: str, metadata: dict[str, Any]) -> bool:
        from correlation_registry.identity_metadata import merge_engagement_metadata

        row = self.engagements.get(engagement_id)
        if not row:
            return False
        row["metadata"] = merge_engagement_metadata(row.get("metadata"), hints=metadata)
        row["updated_at"] = _now_iso()
        return True

    def update_identity_display_name(self, identity_id: str, display_name: str) -> None:
        if not display_name or identity_id not in self.identities:
            return
        row = self.identities[identity_id]
        if not row.get("display_name"):
            row["display_name"] = display_name
            row["updated_at"] = _now_iso()

    def update_identity_primary_email(self, *, identity_id: str, email: str) -> bool:
        normalized = _normalize_email(email)
        if not normalized or identity_id not in self.identities:
            return False
        row = self.identities[identity_id]
        previous = _normalize_email(str(row.get("primary_email") or ""))
        if previous == normalized:
            return False
        if previous:
            ids = self.identity_ids_by_email.get(previous, [])
            self.identity_ids_by_email[previous] = [item for item in ids if item != identity_id]
            if not self.identity_ids_by_email[previous]:
                del self.identity_ids_by_email[previous]
        row["primary_email"] = normalized
        row["updated_at"] = _now_iso()
        self.identity_ids_by_email.setdefault(normalized, []).append(identity_id)
        return True

    def find_recent_engagement_for_email(
        self,
        *,
        email: str,
        within_days: int = DEFAULT_ENGAGEMENT_WINDOW_DAYS,
        identity_id: str = "",
    ) -> str | None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, within_days))
        candidates: list[str] = []
        if identity_id:
            candidates = [identity_id]
        elif email:
            candidates = list(self.identity_ids_by_email.get(_normalize_email(email), []))
        best: tuple[datetime, str] | None = None
        for cand_id in candidates:
            for engagement_id, row in self.engagements.items():
                if str(row.get("identity_id") or "") != cand_id:
                    continue
                anchor_raw = str(row.get("anchor_at") or row.get("created_at") or "")
                try:
                    anchor = datetime.fromisoformat(anchor_raw.replace("Z", "+00:00"))
                except ValueError:
                    anchor = datetime.now(timezone.utc)
                if anchor < cutoff:
                    continue
                if best is None or anchor > best[0]:
                    best = (anchor, engagement_id)
        return best[1] if best else None

    def resolve_or_create_engagement(
        self,
        *,
        identity_id: str,
        anchor_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if identity_id not in self.identities:
            raise ValueError(f"unknown identity_id: {identity_id}")
        engagement_id = _new_uuid()
        ts = _now_iso()
        self.engagements[engagement_id] = {
            "engagement_id": engagement_id,
            "identity_id": identity_id,
            "status": "open",
            "anchor_at": anchor_at or ts,
            "metadata": dict(metadata or {}),
            "created_at": ts,
            "updated_at": ts,
        }
        return engagement_id

    def find_engagement_by_link(self, *, link_type: str, target_id: str, source_repo: str = "") -> str | None:
        key = (normalize_link_type(link_type), str(target_id).strip(), str(source_repo or "gmail-agent").strip())
        link_id = self.link_index.get(key)
        if not link_id:
            return None
        row = self.links.get(link_id)
        return str(row.get("engagement_id") or "") if row else None

    def find_engagement_for_identity_recent(
        self,
        *,
        identity_id: str,
        within_days: int = DEFAULT_ENGAGEMENT_WINDOW_DAYS,
    ) -> str | None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, within_days))
        best: tuple[datetime, str] | None = None
        for engagement_id, row in self.engagements.items():
            if str(row.get("identity_id") or "") != identity_id:
                continue
            anchor_raw = str(row.get("anchor_at") or row.get("created_at") or "")
            try:
                anchor = datetime.fromisoformat(anchor_raw.replace("Z", "+00:00"))
            except ValueError:
                anchor = datetime.now(timezone.utc)
            if anchor < cutoff:
                continue
            if best is None or anchor > best[0]:
                best = (anchor, engagement_id)
        return best[1] if best else None

    def upsert_link(
        self,
        *,
        engagement_id: str,
        link_type: str,
        target_id: str,
        source_repo: str,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if engagement_id not in self.engagements:
            raise ValueError(f"unknown engagement_id: {engagement_id}")
        lt = normalize_link_type(link_type)
        tid = str(target_id).strip()
        repo = str(source_repo or "gmail-agent").strip() or "gmail-agent"
        if not tid:
            raise ValueError("target_id required")
        key = (lt, tid, repo)
        ts = _now_iso()
        link_id = self.link_index.get(key)
        if link_id and link_id in self.links:
            row = self.links[link_id]
            if lt == "mailbox_case":
                existing_eid = str(row.get("engagement_id") or "").strip()
                if existing_eid and existing_eid != str(engagement_id).strip():
                    log.warning(
                        "registry link conflict: mailbox_case %s already linked to %s, refused %s",
                        tid,
                        existing_eid,
                        engagement_id,
                    )
                    raise RegistryLinkConflictError(
                        f"mailbox_case {tid} already linked to engagement {existing_eid}"
                    )
            row["confidence"] = float(confidence)
            row["metadata"] = dict(metadata or {})
            row["updated_at"] = ts
            return dict(row)
        link_id = _new_uuid()
        row = {
            "link_id": link_id,
            "engagement_id": engagement_id,
            "link_type": lt,
            "target_id": tid,
            "source_repo": repo,
            "confidence": float(confidence),
            "metadata": dict(metadata or {}),
            "created_at": ts,
            "updated_at": ts,
        }
        self.links[link_id] = row
        self.link_index[key] = link_id
        return dict(row)

    def list_links_for_engagement(self, engagement_id: str) -> list[dict[str, Any]]:
        rows = [dict(item) for item in self.links.values() if str(item.get("engagement_id") or "") == engagement_id]
        rows.sort(key=lambda item: (str(item.get("link_type") or ""), str(item.get("target_id") or "")))
        return rows

    def get_engagement(self, engagement_id: str) -> dict[str, Any] | None:
        row = self.engagements.get(engagement_id)
        return dict(row) if row else None

    def get_identity(self, identity_id: str) -> dict[str, Any] | None:
        row = self.identities.get(identity_id)
        return dict(row) if row else None

    def find_engagement_by_case_id(self, case_id: str) -> dict[str, Any] | None:
        engagement_id = self.find_engagement_by_link(
            link_type="mailbox_case",
            target_id=case_id,
            source_repo="gmail-agent",
        )
        if not engagement_id:
            return None
        engagement = self.get_engagement(engagement_id)
        if not engagement:
            return None
        identity = self.get_identity(str(engagement.get("identity_id") or ""))
        return {
            "engagement_id": engagement_id,
            "engagement": engagement,
            "identity": identity,
            "links": self.list_links_for_engagement(engagement_id),
        }

    def find_identity_by_email(self, email: str) -> dict[str, Any] | None:
        ids = self.identity_ids_by_email.get(_normalize_email(email), [])
        if not ids:
            return None
        return self.get_identity(ids[-1])

    def list_identities_recent(self, *, within_days: int = 90, limit: int = 100) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, within_days))
        rows: list[dict[str, Any]] = []
        for row in self.identities.values():
            ts_raw = str(row.get("updated_at") or row.get("created_at") or "")
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(timezone.utc)
            if ts >= cutoff:
                rows.append(dict(row))
        rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
        return rows[: max(1, int(limit))]

    def upsert_identity_binding_suggestion(
        self,
        *,
        source_identity_id: str,
        target_identity_id: str,
        signal_type: str,
        confidence: float,
        evidence_json: dict[str, Any] | None = None,
    ) -> bool:
        key = (source_identity_id, target_identity_id, signal_type)
        existing = next(
            (
                row
                for row in self.binding_suggestions.values()
                if (
                    row.get("source_identity_id") == source_identity_id
                    and row.get("target_identity_id") == target_identity_id
                    and row.get("signal_type") == signal_type
                )
            ),
            None,
        )
        if existing and str(existing.get("status") or "") != "pending_operator":
            return False
        sid = str(existing.get("suggestion_id") if existing else _new_uuid())
        ts = _now_iso()
        self.binding_suggestions[sid] = {
            "suggestion_id": sid,
            "source_identity_id": source_identity_id,
            "target_identity_id": target_identity_id,
            "signal_type": signal_type,
            "confidence": float(confidence),
            "status": "pending_operator",
            "evidence_json": dict(evidence_json or {}),
            "created_at": existing.get("created_at", ts) if existing else ts,
            "updated_at": ts,
        }
        return True

    def list_identity_binding_suggestions(self, *, status: str = "pending_operator", limit: int = 50) -> list[dict[str, Any]]:
        rows = [
            dict(row)
            for row in self.binding_suggestions.values()
            if str(row.get("status") or "") == str(status or "pending_operator")
        ]
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return rows[: max(1, int(limit))]

    def update_identity_binding_suggestion_status(
        self,
        *,
        suggestion_id: str,
        status: str,
        reviewed_by: str = "operator",
    ) -> bool:
        row = self.binding_suggestions.get(suggestion_id)
        if not row:
            return False
        row["status"] = str(status or "").strip()
        row["updated_at"] = _now_iso()
        evidence = row.get("evidence_json") if isinstance(row.get("evidence_json"), dict) else {}
        evidence["reviewed_by"] = reviewed_by
        row["evidence_json"] = evidence
        return True

    def get_identity_binding_suggestion(self, *, suggestion_id: str) -> dict[str, Any] | None:
        row = self.binding_suggestions.get(str(suggestion_id or "").strip())
        return dict(row) if row else None

    def list_engagements_for_identity(
        self,
        *,
        identity_id: str,
        status: str = "",
    ) -> list[dict[str, Any]]:
        identity_id = str(identity_id or "").strip()
        rows: list[dict[str, Any]] = []
        for row in self.engagements.values():
            if str(row.get("identity_id") or "") != identity_id:
                continue
            if status and str(row.get("status") or "") != str(status):
                continue
            rows.append(dict(row))
        return rows

    def merge_identities(
        self,
        *,
        source_identity_id: str,
        target_identity_id: str,
        suggestion_id: str = "",
        operator_id: str = "operator",
        log_id: str,
        detail: dict[str, Any] | None = None,
    ) -> int:
        """Atomically repoint engagements, write the audit log, then delete the source identity.

        Mirrors the real Postgres schema's cascade behavior: deleting the source identity
        cascades away any identity_binding_suggestions row referencing it (source or target),
        and any merge_logs row referencing such a suggestion has its suggestion_id nulled
        (ON DELETE SET NULL) rather than being removed. The log is written before the delete
        so it is never blocked by the suggestion already being gone.
        """
        source = str(source_identity_id or "").strip()
        target = str(target_identity_id or "").strip()
        repointed = 0
        if source and target and source != target:
            for row in self.engagements.values():
                if str(row.get("identity_id") or "") == source:
                    row["identity_id"] = target
                    row["updated_at"] = _now_iso()
                    repointed += 1

        self.merge_logs.append(
            {
                "log_id": log_id,
                "suggestion_id": suggestion_id or None,
                "source_identity_id": source,
                "target_identity_id": target,
                "operator_id": operator_id,
                "engagements_repointed": repointed,
                "detail": dict(detail or {}),
            }
        )

        if source in self.identities:
            row = self.identities.pop(source)
            email_key = _normalize_email(str(row.get("primary_email") or ""))
            if email_key in self.identity_ids_by_email:
                self.identity_ids_by_email[email_key] = [
                    item for item in self.identity_ids_by_email[email_key] if item != source
                ]
                if not self.identity_ids_by_email[email_key]:
                    del self.identity_ids_by_email[email_key]

        cascaded_suggestion_ids = [
            sid
            for sid, s in self.binding_suggestions.items()
            if str(s.get("source_identity_id") or "") == source
            or str(s.get("target_identity_id") or "") == source
        ]
        for sid in cascaded_suggestion_ids:
            del self.binding_suggestions[sid]
        if cascaded_suggestion_ids:
            for log_row in self.merge_logs:
                if log_row.get("suggestion_id") in cascaded_suggestion_ids:
                    log_row["suggestion_id"] = None

        return repointed

    def find_duplicate_email_groups(self, *, limit: int = 0) -> list[dict[str, Any]]:
        grouped: dict[str, list[tuple[str, str]]] = {}
        for identity_id, row in self.identities.items():
            email_norm = _normalize_email(str(row.get("primary_email") or ""))
            if not email_norm:
                continue
            grouped.setdefault(email_norm, []).append(
                (str(row.get("created_at") or ""), identity_id)
            )
        out: list[dict[str, Any]] = []
        for email_norm, items in sorted(grouped.items()):
            if len(items) < 2:
                continue
            items.sort(key=lambda pair: pair[0])
            out.append(
                {
                    "email_norm": email_norm,
                    "identity_count": len(items),
                    "identity_ids": [identity_id for _, identity_id in items],
                }
            )
            if limit > 0 and len(out) >= limit:
                break
        return out

    def count_duplicate_email_groups(self) -> int:
        return len(self.find_duplicate_email_groups())

    def merge_email_duplicate_group(
        self,
        *,
        email_norm: str,
        canonical_identity_id: str,
        duplicate_identity_ids: list[str],
        operator_id: str = "system",
    ) -> dict[str, Any]:
        canonical = str(canonical_identity_id or "").strip()
        duplicates = [str(x).strip() for x in duplicate_identity_ids if str(x).strip() and str(x).strip() != canonical]
        engagements_repointed = 0
        for engagement in self.engagements.values():
            if str(engagement.get("identity_id") or "") in duplicates:
                engagement["identity_id"] = canonical
                engagement["updated_at"] = _now_iso()
                engagements_repointed += 1
        identities_deleted = 0
        for duplicate_id in duplicates:
            row = self.identities.pop(duplicate_id, None)
            if not row:
                continue
            identities_deleted += 1
            email_key = _normalize_email(str(row.get("primary_email") or ""))
            if email_key in self.identity_ids_by_email:
                self.identity_ids_by_email[email_key] = [
                    item for item in self.identity_ids_by_email[email_key] if item != duplicate_id
                ]
                if not self.identity_ids_by_email[email_key]:
                    del self.identity_ids_by_email[email_key]
        if canonical in self.identities:
            ids = self.identity_ids_by_email.setdefault(email_norm, [])
            if canonical not in ids:
                ids.append(canonical)
        return {
            "email_norm": email_norm,
            "canonical_identity_id": canonical,
            "duplicate_identity_ids": duplicates,
            "engagements_repointed": engagements_repointed,
            "identities_deleted": identities_deleted,
            "operator_id": operator_id,
        }


class PostgresCorrelationRegistryStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = str(database_url or "").strip()
        if not self.database_url:
            raise ValueError("database_url is required")

    def bootstrap(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(CORRELATION_REGISTRY_SCHEMA_SQL)
            conn.commit()

    def create_identity(self, *, email: str, display_name: str = "", metadata: dict[str, Any] | None = None) -> str:
        from correlation_registry.identity_metadata import normalize_identity_metadata

        normalized = _normalize_email(email)
        if not normalized:
            raise ValueError("email required for identity")
        identity_id = _new_uuid()
        meta = normalize_identity_metadata(metadata, email=normalized, display_name=display_name or "")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO topinstal_identities (
                        identity_id, primary_email, display_name, metadata, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s::jsonb, NOW(), NOW())
                    """,
                    (identity_id, normalized, display_name or "", json.dumps(meta, ensure_ascii=False)),
                )
            conn.commit()
        return identity_id

    def merge_identity_metadata(self, identity_id: str, metadata: dict[str, Any]) -> bool:
        from correlation_registry.identity_metadata import merge_identity_metadata

        row = self.get_identity(identity_id)
        if not row:
            return False
        merged = merge_identity_metadata(
            row.get("metadata"),
            email=str(row.get("primary_email") or ""),
            display_name=str(row.get("display_name") or ""),
            hints=metadata,
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE topinstal_identities
                    SET metadata = %s::jsonb, updated_at = NOW()
                    WHERE identity_id = %s
                    """,
                    (json.dumps(merged, ensure_ascii=False), identity_id),
                )
                updated = cur.rowcount > 0
            conn.commit()
        return updated

    def merge_engagement_metadata(self, engagement_id: str, metadata: dict[str, Any]) -> bool:
        from correlation_registry.identity_metadata import merge_engagement_metadata

        row = self.get_engagement(engagement_id)
        if not row:
            return False
        merged = merge_engagement_metadata(row.get("metadata"), hints=metadata)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE topinstal_engagements
                    SET metadata = %s::jsonb, updated_at = NOW()
                    WHERE engagement_id = %s
                    """,
                    (json.dumps(merged, ensure_ascii=False), engagement_id),
                )
                updated = cur.rowcount > 0
            conn.commit()
        return updated

    def update_identity_display_name(self, identity_id: str, display_name: str) -> None:
        if not display_name:
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE topinstal_identities
                    SET display_name = COALESCE(NULLIF(%s, ''), display_name),
                        updated_at = NOW()
                    WHERE identity_id = %s
                    """,
                    (display_name, identity_id),
                )
            conn.commit()

    def update_identity_primary_email(self, *, identity_id: str, email: str) -> bool:
        normalized = _normalize_email(email)
        if not normalized:
            return False
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE topinstal_identities
                    SET primary_email = %s,
                        updated_at = NOW()
                    WHERE identity_id = %s
                      AND primary_email IS DISTINCT FROM %s
                    """,
                    (normalized, identity_id, normalized),
                )
                updated = cur.rowcount > 0
            conn.commit()
        return updated

    def find_recent_engagement_for_email(
        self,
        *,
        email: str,
        within_days: int = DEFAULT_ENGAGEMENT_WINDOW_DAYS,
        identity_id: str = "",
    ) -> str | None:
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                if identity_id:
                    cur.execute(
                        """
                        SELECT engagement_id FROM topinstal_engagements
                        WHERE identity_id = %s
                          AND anchor_at >= NOW() - (%s || ' days')::interval
                        ORDER BY anchor_at DESC
                        LIMIT 1
                        """,
                        (identity_id, str(max(1, within_days))),
                    )
                else:
                    normalized = _normalize_email(email)
                    if not normalized:
                        return None
                    cur.execute(
                        """
                        SELECT e.engagement_id FROM topinstal_engagements e
                        JOIN topinstal_identities i ON i.identity_id = e.identity_id
                        WHERE lower(i.primary_email) = %s
                          AND e.anchor_at >= NOW() - (%s || ' days')::interval
                        ORDER BY e.anchor_at DESC
                        LIMIT 1
                        """,
                        (normalized, str(max(1, within_days))),
                    )
                row = cur.fetchone()
        if not row:
            return None
        return str(row.get("engagement_id") or "")

    def resolve_or_create_engagement(
        self,
        *,
        identity_id: str,
        anchor_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        engagement_id = _new_uuid()
        meta_json = json.dumps(dict(metadata or {}))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO topinstal_engagements (
                        engagement_id, identity_id, status, anchor_at, metadata, created_at, updated_at
                    ) VALUES (%s, %s, 'open', COALESCE(%s::timestamptz, NOW()), %s::jsonb, NOW(), NOW())
                    """,
                    (engagement_id, identity_id, anchor_at, meta_json),
                )
            conn.commit()
        return engagement_id

    def find_engagement_by_link(self, *, link_type: str, target_id: str, source_repo: str = "") -> str | None:
        lt = normalize_link_type(link_type)
        tid = str(target_id).strip()
        repo = str(source_repo or "gmail-agent").strip() or "gmail-agent"
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT engagement_id FROM correlation_links
                    WHERE link_type = %s AND target_id = %s AND source_repo = %s
                    LIMIT 1
                    """,
                    (lt, tid, repo),
                )
                row = cur.fetchone()
        if not row:
            return None
        return str(row.get("engagement_id") or "")

    def find_engagement_for_identity_recent(
        self,
        *,
        identity_id: str,
        within_days: int = DEFAULT_ENGAGEMENT_WINDOW_DAYS,
    ) -> str | None:
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT engagement_id FROM topinstal_engagements
                    WHERE identity_id = %s
                      AND anchor_at >= NOW() - (%s || ' days')::interval
                    ORDER BY anchor_at DESC
                    LIMIT 1
                    """,
                    (identity_id, str(max(1, within_days))),
                )
                row = cur.fetchone()
        if not row:
            return None
        return str(row.get("engagement_id") or "")

    def upsert_link(
        self,
        *,
        engagement_id: str,
        link_type: str,
        target_id: str,
        source_repo: str,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        lt = normalize_link_type(link_type)
        tid = str(target_id).strip()
        repo = str(source_repo or "gmail-agent").strip() or "gmail-agent"
        if not tid:
            raise ValueError("target_id required")
        link_id = _new_uuid()
        meta_json = json.dumps(dict(metadata or {}))
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                if lt == "mailbox_case":
                    cur.execute(
                        """
                        SELECT engagement_id FROM correlation_links
                        WHERE link_type = %s AND target_id = %s AND source_repo = %s
                        """,
                        (lt, tid, repo),
                    )
                    existing = cur.fetchone()
                    if existing:
                        old_eid = str(existing.get("engagement_id") or "").strip()
                        new_eid = str(engagement_id).strip()
                        if old_eid and new_eid and old_eid != new_eid:
                            log.warning(
                                "registry link conflict: mailbox_case %s already linked to %s, refused %s",
                                tid,
                                old_eid,
                                new_eid,
                            )
                            raise RegistryLinkConflictError(
                                f"mailbox_case {tid} already linked to engagement {old_eid}"
                            )
                cur.execute(
                    """
                    INSERT INTO correlation_links (
                        link_id, engagement_id, link_type, target_id, source_repo,
                        confidence, metadata, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, NOW(), NOW())
                    ON CONFLICT (link_type, target_id, source_repo) DO UPDATE SET
                        engagement_id = EXCLUDED.engagement_id,
                        confidence = EXCLUDED.confidence,
                        metadata = EXCLUDED.metadata,
                        updated_at = NOW()
                    RETURNING link_id, engagement_id, link_type, target_id, source_repo,
                              confidence, metadata, created_at, updated_at
                    """,
                    (link_id, engagement_id, lt, tid, repo, float(confidence), meta_json),
                )
                row = cur.fetchone()
            conn.commit()
        return dict(row) if row else {}

    def list_links_for_engagement(self, engagement_id: str) -> list[dict[str, Any]]:
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT link_id, engagement_id, link_type, target_id, source_repo,
                           confidence, metadata, created_at, updated_at
                    FROM correlation_links
                    WHERE engagement_id = %s
                    ORDER BY link_type, target_id
                    """,
                    (engagement_id,),
                )
                rows = cur.fetchall()
        return [dict(row) for row in rows]

    def get_engagement(self, engagement_id: str) -> dict[str, Any] | None:
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT engagement_id, identity_id, status, anchor_at, metadata, created_at, updated_at
                    FROM topinstal_engagements WHERE engagement_id = %s
                    """,
                    (engagement_id,),
                )
                row = cur.fetchone()
        return dict(row) if row else None

    def get_identity(self, identity_id: str) -> dict[str, Any] | None:
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT identity_id, primary_email, display_name, metadata, created_at, updated_at
                    FROM topinstal_identities WHERE identity_id = %s
                    """,
                    (identity_id,),
                )
                row = cur.fetchone()
        return dict(row) if row else None

    def find_engagement_by_case_id(self, case_id: str) -> dict[str, Any] | None:
        engagement_id = self.find_engagement_by_link(
            link_type="mailbox_case",
            target_id=case_id,
            source_repo="gmail-agent",
        )
        if not engagement_id:
            return None
        engagement = self.get_engagement(engagement_id)
        if not engagement:
            return None
        identity = self.get_identity(str(engagement.get("identity_id") or ""))
        return {
            "engagement_id": engagement_id,
            "engagement": engagement,
            "identity": identity,
            "links": self.list_links_for_engagement(engagement_id),
        }

    def find_identity_by_email(self, email: str) -> dict[str, Any] | None:
        normalized = _normalize_email(email)
        if not normalized:
            return None
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT identity_id, primary_email, display_name, metadata, created_at, updated_at
                    FROM topinstal_identities
                    WHERE lower(primary_email) = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (normalized,),
                )
                row = cur.fetchone()
        return dict(row) if row else None

    def list_identities_recent(self, *, within_days: int = 90, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT identity_id, primary_email, display_name, metadata, created_at, updated_at
                    FROM topinstal_identities
                    WHERE updated_at >= NOW() - make_interval(days => %s)
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (max(1, int(within_days)), max(1, int(limit))),
                )
                rows = cur.fetchall()
        return [dict(row) for row in rows]

    def upsert_identity_binding_suggestion(
        self,
        *,
        source_identity_id: str,
        target_identity_id: str,
        signal_type: str,
        confidence: float,
        evidence_json: dict[str, Any] | None = None,
    ) -> bool:
        suggestion_id = _new_uuid()
        meta_json = json.dumps(dict(evidence_json or {}))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO identity_binding_suggestions (
                        suggestion_id, source_identity_id, target_identity_id,
                        signal_type, confidence, status, evidence_json, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, 'pending_operator', %s::jsonb, NOW(), NOW())
                    ON CONFLICT (source_identity_id, target_identity_id, signal_type) DO UPDATE SET
                        confidence = EXCLUDED.confidence,
                        evidence_json = EXCLUDED.evidence_json,
                        updated_at = NOW()
                    WHERE identity_binding_suggestions.status = 'pending_operator'
                    """,
                    (
                        suggestion_id,
                        source_identity_id,
                        target_identity_id,
                        signal_type,
                        float(confidence),
                        meta_json,
                    ),
                )
                inserted = cur.rowcount > 0
            conn.commit()
        return inserted

    def list_identity_binding_suggestions(
        self,
        *,
        status: str = "pending_operator",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT suggestion_id, source_identity_id, target_identity_id,
                           signal_type, confidence, status, evidence_json, created_at, updated_at
                    FROM identity_binding_suggestions
                    WHERE status = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (str(status or "pending_operator"), max(1, int(limit))),
                )
                rows = cur.fetchall()
        return [dict(row) for row in rows]

    def update_identity_binding_suggestion_status(
        self,
        *,
        suggestion_id: str,
        status: str,
        reviewed_by: str = "operator",
    ) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE identity_binding_suggestions
                    SET status = %s,
                        evidence_json = evidence_json || %s::jsonb,
                        updated_at = NOW()
                    WHERE suggestion_id = %s
                    """,
                    (
                        str(status or "").strip(),
                        json.dumps({"reviewed_by": reviewed_by}),
                        suggestion_id,
                    ),
                )
                updated = cur.rowcount > 0
            conn.commit()
        return updated

    def get_identity_binding_suggestion(self, *, suggestion_id: str) -> dict[str, Any] | None:
        sid = str(suggestion_id or "").strip()
        if not sid:
            return None
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT suggestion_id, source_identity_id, target_identity_id,
                           signal_type, confidence, status, evidence_json, created_at, updated_at
                    FROM identity_binding_suggestions
                    WHERE suggestion_id = %s
                    """,
                    (sid,),
                )
                row = cur.fetchone()
        return dict(row) if row else None

    def list_engagements_for_identity(
        self,
        *,
        identity_id: str,
        status: str = "",
    ) -> list[dict[str, Any]]:
        identity_id = str(identity_id or "").strip()
        if not identity_id:
            return []
        sql = """
            SELECT engagement_id, identity_id, status, anchor_at, metadata, created_at, updated_at
            FROM topinstal_engagements
            WHERE identity_id = %s
        """
        params: list[Any] = [identity_id]
        if str(status or "").strip():
            sql += " AND status = %s"
            params.append(str(status))
        sql += " ORDER BY anchor_at DESC"
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() or []
        return [dict(row) for row in rows]

    def merge_identities(
        self,
        *,
        source_identity_id: str,
        target_identity_id: str,
        suggestion_id: str = "",
        operator_id: str = "operator",
        log_id: str,
        detail: dict[str, Any] | None = None,
    ) -> int:
        """Atomically repoint engagements, write the audit log, then delete the source identity.

        Single transaction: a mid-sequence failure rolls back the whole merge rather than
        leaving a partial repoint or an orphaned identity.

        Order matters: identity_merge_log is written BEFORE the source identity is deleted.
        Deleting topinstal_identities cascades (ON DELETE CASCADE) to remove the
        identity_binding_suggestions row this merge is for, and identity_merge_log.suggestion_id
        references that row with ON DELETE SET NULL -- so writing the log first satisfies the
        FK at insert time, and the later cascade only nulls the suggestion_id on the log row
        instead of failing to insert it or removing it. Writing the log after the delete (the
        previous, buggy order) always raised a ForeignKeyViolation, because by then the
        referenced suggestion row no longer existed.
        """
        source = str(source_identity_id or "").strip()
        target = str(target_identity_id or "").strip()
        repointed = 0
        with self._connect() as conn:
            with conn.cursor() as cur:
                if source and target and source != target:
                    cur.execute(
                        """
                        UPDATE topinstal_engagements
                        SET identity_id = %s, updated_at = NOW()
                        WHERE identity_id = %s
                        """,
                        (target, source),
                    )
                    repointed = int(cur.rowcount or 0)

                cur.execute(
                    """
                    INSERT INTO identity_merge_log (
                        log_id, suggestion_id, source_identity_id, target_identity_id,
                        operator_id, engagements_repointed, status, detail, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'completed', %s::jsonb, NOW())
                    """,
                    (
                        log_id,
                        suggestion_id or None,
                        source,
                        target,
                        operator_id,
                        repointed,
                        json.dumps(detail or {}, ensure_ascii=False),
                    ),
                )

                cur.execute(
                    "DELETE FROM topinstal_identities WHERE identity_id = %s",
                    (source,),
                )
            conn.commit()
        return repointed

    def find_duplicate_email_groups(self, *, limit: int = 0) -> list[dict[str, Any]]:
        sql = """
            SELECT lower(trim(primary_email)) AS email_norm,
                   COUNT(*)::int AS identity_count,
                   array_agg(identity_id ORDER BY created_at ASC) AS identity_ids
            FROM topinstal_identities
            WHERE primary_email IS NOT NULL AND trim(primary_email) <> ''
            GROUP BY 1
            HAVING COUNT(*) > 1
            ORDER BY 1
        """
        params: tuple[Any, ...] = ()
        if int(limit or 0) > 0:
            sql += " LIMIT %s"
            params = (int(limit),)
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() or []
        return [dict(row) for row in rows]

    def count_duplicate_email_groups(self) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)::int
                    FROM (
                        SELECT lower(trim(primary_email)) AS email_norm
                        FROM topinstal_identities
                        WHERE primary_email IS NOT NULL AND trim(primary_email) <> ''
                        GROUP BY 1
                        HAVING COUNT(*) > 1
                    ) t
                    """
                )
                row = cur.fetchone()
        return int(row[0] if row else 0)

    def merge_email_duplicate_group(
        self,
        *,
        email_norm: str,
        canonical_identity_id: str,
        duplicate_identity_ids: list[str],
        operator_id: str = "system",
    ) -> dict[str, Any]:
        canonical = str(canonical_identity_id or "").strip()
        duplicates = [str(x).strip() for x in duplicate_identity_ids if str(x).strip() and str(x).strip() != canonical]
        if not canonical or not duplicates:
            return {
                "email_norm": email_norm,
                "canonical_identity_id": canonical,
                "duplicate_identity_ids": duplicates,
                "engagements_repointed": 0,
                "identities_deleted": 0,
                "operator_id": operator_id,
            }
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE topinstal_engagements
                    SET identity_id = %s, updated_at = NOW()
                    WHERE identity_id = ANY(%s)
                    """,
                    (canonical, duplicates),
                )
                engagements_repointed = int(cur.rowcount or 0)
                cur.execute(
                    "DELETE FROM topinstal_identities WHERE identity_id = ANY(%s)",
                    (duplicates,),
                )
                identities_deleted = int(cur.rowcount or 0)
                for duplicate_id in duplicates:
                    row_log_id = f"iml_{uuid.uuid4().hex[:16]}"
                    cur.execute(
                        """
                        INSERT INTO identity_merge_log (
                            log_id, source_identity_id, target_identity_id, operator_id,
                            engagements_repointed, status, detail, created_at
                        ) VALUES (%s, %s, %s, %s, %s, 'completed', %s::jsonb, NOW())
                        """,
                        (
                            row_log_id,
                            duplicate_id,
                            canonical,
                            operator_id,
                            engagements_repointed,
                            json.dumps({"email_norm": email_norm, "mode": "email_identical_dedup"}),
                        ),
                    )
            conn.commit()
        return {
            "email_norm": email_norm,
            "canonical_identity_id": canonical,
            "duplicate_identity_ids": duplicates,
            "engagements_repointed": engagements_repointed,
            "identities_deleted": identities_deleted,
            "operator_id": operator_id,
        }

    def _connect(self, *, row_factory: bool = False):
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required for correlation registry Postgres access.") from exc
        kwargs: dict[str, Any] = {"connect_timeout": POSTGRES_CONNECT_TIMEOUT_SEC}
        if row_factory:
            from psycopg.rows import dict_row  # type: ignore[import-not-found]

            kwargs["row_factory"] = dict_row
        return psycopg.connect(self.database_url, **kwargs)


def build_registry_store(database_url: str = "", *, in_memory: bool = False) -> CorrelationRegistryStore:
    if in_memory:
        return InMemoryCorrelationRegistryStore()
    if not str(database_url or "").strip():
        raise ValueError("database_url required unless in_memory=True")
    return PostgresCorrelationRegistryStore(database_url)
