
"""Operator engagement snapshot store with optimistic locking (PR-A)."""

from __future__ import annotations

from log_config import get_logger
import json
import os
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from llm_contracts.engagement_snapshot_v2 import (
    AgentMemory,
    EngagementSnapshotV2,
    HitlGate,
    HvacProfile,
    OperationalStatus,
)

AGENT_RUNTIME_SCHEMA_PATH = Path(__file__).resolve().parent / "AGENT_RUNTIME_SCHEMA.sql"
POSTGRES_CONNECT_TIMEOUT_SEC = int(os.getenv("AGENT_POSTGRES_CONNECT_TIMEOUT", "10"))

logger = get_logger(__name__)


class AgentConcurrencyError(RuntimeError):
    """Raised when optimistic lock compare-and-swap fails."""


class OperatorEngagementStore(ABC):
    @abstractmethod
    def load_snapshot(self, engagement_id: str) -> EngagementSnapshotV2 | None: ...

    @abstractmethod
    def load_snapshot_by_case_id(self, case_id: str) -> EngagementSnapshotV2 | None: ...

    @abstractmethod
    def save_snapshot(self, snapshot: EngagementSnapshotV2, expected_version: int) -> int: ...

    @abstractmethod
    def insert_snapshot(self, snapshot: EngagementSnapshotV2) -> EngagementSnapshotV2:
        """Persist a new engagement row at version 1 (first write only)."""

    @abstractmethod
    def init_snapshot_from_signal(
        self,
        *,
        signal: Mapping[str, Any],
        case_id: str,
        engagement_id: str,
        trace_id: str | None = None,
    ) -> EngagementSnapshotV2:
        """Build + insert (convenience). Prefer build_snapshot_from_signal + insert_snapshot."""

    def load_snapshots_for_case_ids(self, case_ids: list[str]) -> list[EngagementSnapshotV2]:
        """Load latest snapshot per case_id (PR-D feed bridge)."""
        out: list[EngagementSnapshotV2] = []
        for cid in case_ids:
            snap = self.load_snapshot_by_case_id(str(cid or "").strip())
            if snap is not None:
                out.append(snap)
        return out

    def list_recent_snapshots(self, *, limit: int = 50) -> list[EngagementSnapshotV2]:
        """List snapshots ordered by recency (PR-E feed)."""
        _ = limit
        return []

    def list_recent_snapshots_with_updated_at(
        self, *, limit: int = 50
    ) -> list[tuple[EngagementSnapshotV2, str]]:
        """Roadmap 3.1 (Follow-up Guardian): snapshots paired with their row `updated_at`.

        `updated_at` is not a field on `EngagementSnapshotV2` itself (see `_snapshot_from_row`);
        it lives only on the storage row. FG-02: prefer snapshot `lifecycle_state_since` for
        hours-in-state; fall back to this row `updated_at` only when since is missing (legacy).
        Default empty; overridden by each concrete store.
        """
        _ = limit
        return []


def _resolve_trace_id(signal: Mapping[str, Any], trace_id: str | None) -> str:
    explicit = str(trace_id or "").strip()
    if explicit:
        return explicit
    for key in ("signal_id", "trace_id", "id"):
        value = str(signal.get(key) or "").strip()
        if value:
            return value
    return ""


def _default_steps_remaining() -> int:
    return int(os.environ.get("AGENT_MAX_ROUNDS", "12"))


def build_snapshot_from_signal(
    *,
    signal: Mapping[str, Any],
    case_id: str,
    engagement_id: str,
    signal_id: str | None = None,
    trace_id: str | None = None,
    feed_visibility: Any | None = None,
) -> EngagementSnapshotV2:
    """Pure factory — does not persist (PR-A checklist).

    SLICE-2B1: `feed_visibility` is forwarded. Before this it was silently dropped here while the
    dry-run branch of `ensure_engagement_snapshot` did pass it, so the real case-bound production
    path wrote every snapshot with no classification at all.
    """
    eid = str(engagement_id or "").strip()
    cid = str(case_id or "").strip()
    if not eid or not cid:
        raise ValueError("engagement_id and case_id are required")
    return build_initial_snapshot(
        case_id=cid,
        engagement_id=eid,
        signal_id=str(signal_id or signal.get("signal_id") or "").strip(),
        trace_id=_resolve_trace_id(signal, trace_id),
        feed_visibility=feed_visibility,
    )


def build_staging_snapshot(
    *,
    engagement_id: str,
    signal_id: str = "",
    trace_id: str,
    feed_visibility: Any | None = None,
) -> EngagementSnapshotV2:
    """SLICE-2B: `feed_visibility` is optional routing metadata. Omitting it keeps the previous
    behaviour exactly (the field stays None and the reader applies the legacy main_feed fallback)."""
    return EngagementSnapshotV2(
        feed_visibility=feed_visibility,
        engagement_id=engagement_id,
        case_id="",
        version=1,
        signal_id=str(signal_id or "").strip(),
        trace_id=trace_id,
        operational_status=OperationalStatus(
            code="enriching",
            steps_remaining=_default_steps_remaining(),
            blocking=False,
        ),
        hvac_profile=HvacProfile(),
        gaps=[],
        agent_memory=AgentMemory(),
        actions=[],
        hitl_gate=HitlGate(required=False, reason=""),
        case_kind="niezaklasyfikowane",
    )
def build_initial_snapshot(
    *,
    case_id: str,
    engagement_id: str,
    signal_id: str = "",
    trace_id: str,
    feed_visibility: Any | None = None,
) -> EngagementSnapshotV2:
    return EngagementSnapshotV2(
        feed_visibility=feed_visibility,
        engagement_id=engagement_id,
        case_id=case_id,
        version=1,
        signal_id=str(signal_id or "").strip(),
        trace_id=trace_id,
        operational_status=OperationalStatus(
            code="raw_inquiry",
            steps_remaining=_default_steps_remaining(),
        ),
        hvac_profile=HvacProfile(),
        gaps=[],
        agent_memory=AgentMemory(),
        actions=[],
        hitl_gate=HitlGate(required=False, reason=""),
    )


class InMemoryOperatorEngagementStore(OperatorEngagementStore):
    """In-process store for unit tests (same locking semantics as Postgres)."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def load_snapshot(self, engagement_id: str) -> EngagementSnapshotV2 | None:
        row = self._rows.get(str(engagement_id or "").strip())
        if not row:
            return None
        if str(row.get("status") or "") == "expired":
            return None
        return _snapshot_from_row(row)

    def load_snapshot_by_case_id(self, case_id: str) -> EngagementSnapshotV2 | None:
        cid = str(case_id or "").strip()
        for row in self._rows.values():
            if str(row.get("case_id") or "") == cid:
                return _snapshot_from_row(row)
        return None

    def list_recent_snapshots(self, *, limit: int = 50) -> list[EngagementSnapshotV2]:
        rows = sorted(
            self._rows.values(),
            key=lambda r: str(r.get("updated_at") or ""),
            reverse=True,
        )
        out: list[EngagementSnapshotV2] = []
        for row in rows[: max(1, int(limit))]:
            out.append(_snapshot_from_row(row))
        return out

    def list_recent_snapshots_with_updated_at(
        self, *, limit: int = 50
    ) -> list[tuple[EngagementSnapshotV2, str]]:
        rows = sorted(
            self._rows.values(),
            key=lambda r: str(r.get("updated_at") or ""),
            reverse=True,
        )
        out: list[tuple[EngagementSnapshotV2, str]] = []
        for row in rows:
            status = str(row.get("status") or "active").strip().lower()
            if status in {"expired", "materialized"}:
                continue
            out.append((_snapshot_from_row(row), str(row.get("updated_at") or "")))
            if len(out) >= max(1, int(limit)):
                break
        return out

    def list_staging_engagement_ids(self) -> list[str]:
        out: list[str] = []
        for eid, row in self._rows.items():
            if not str(eid).startswith("stg_"):
                continue
            if str(row.get("case_id") or "").strip():
                continue
            out.append(str(eid))
        return out

    def delete_snapshot(self, engagement_id: str) -> None:
        self._rows.pop(str(engagement_id or "").strip(), None)

    def soft_delete_snapshot(self, engagement_id: str) -> None:
        eid = str(engagement_id or "").strip()
        if eid in self._rows:
            self._rows[eid]["status"] = "expired"
            self._rows[eid]["expired_at"] = _utc_now_iso()

    def save_snapshot(self, snapshot: EngagementSnapshotV2, expected_version: int) -> int:
        engagement_id = str(snapshot.engagement_id or "").strip()
        if not engagement_id:
            raise ValueError("engagement_id is required")
        row = self._rows.get(engagement_id)
        if row is None:
            raise AgentConcurrencyError(
                f"no snapshot row for engagement_id={engagement_id!r}"
            )
        current_version = int(row.get("version") or 0)
        if current_version != int(expected_version):
            raise AgentConcurrencyError(
                f"version conflict for {engagement_id!r}: "
                f"expected={expected_version}, actual={current_version}"
            )
        previous_payload = _row_snapshot_payload(row)
        stamped = _apply_lifecycle_state_since(snapshot, previous_payload=previous_payload)
        new_version = int(expected_version) + 1
        updated = stamped.model_copy(update={"version": new_version})
        EngagementSnapshotV2.model_validate(updated.model_dump(mode="python"))
        row["version"] = new_version
        row["case_id"] = updated.case_id
        row["last_trace_id"] = updated.trace_id
        row["snapshot_data"] = updated.model_dump(mode="python")
        row["updated_at"] = _utc_now_iso()
        return new_version

    def insert_snapshot(self, snapshot: EngagementSnapshotV2) -> EngagementSnapshotV2:
        eid = str(snapshot.engagement_id or "").strip()
        if not eid:
            raise ValueError("engagement_id is required")
        if eid in self._rows:
            raise AgentConcurrencyError(f"snapshot already exists for {eid!r}")
        if int(snapshot.version) != 1:
            raise ValueError("insert_snapshot requires version=1")
        stamped = _apply_lifecycle_state_since(snapshot, previous_payload=None)
        self._rows[eid] = {
            "engagement_id": eid,
            "case_id": stamped.case_id,
            "version": 1,
            "last_trace_id": stamped.trace_id,
            "snapshot_data": stamped.model_dump(mode="python"),
            "updated_at": _utc_now_iso(),
        }
        return stamped

    def init_snapshot_from_signal(
        self,
        *,
        signal: Mapping[str, Any],
        case_id: str,
        engagement_id: str,
        trace_id: str | None = None,
    ) -> EngagementSnapshotV2:
        snapshot = build_snapshot_from_signal(
            signal=signal,
            case_id=case_id,
            engagement_id=engagement_id,
            signal_id=str(signal.get("signal_id") or "").strip(),
            trace_id=trace_id,
        )
        return self.insert_snapshot(snapshot)


class PostgresOperatorEngagementStore(OperatorEngagementStore):
    """Postgres-backed operator engagement snapshots."""

    def __init__(self, database_url: str) -> None:
        self.database_url = str(database_url or "").strip()
        if not self.database_url:
            raise ValueError("database_url is required for PostgresOperatorEngagementStore")

    def bootstrap(self) -> None:
        from agent_runtime.bootstrap import bootstrap_agent_runtime

        with self._connect() as conn:
            bootstrap_agent_runtime(conn)

    def load_snapshot(self, engagement_id: str) -> EngagementSnapshotV2 | None:
        row = self._fetch_one(
            """
            SELECT engagement_id, case_id, version, snapshot_data, last_trace_id, updated_at
            FROM operator_engagement_snapshots
            WHERE engagement_id = %(engagement_id)s
            """,
            {"engagement_id": str(engagement_id or "").strip()},
        )
        if not row:
            return None
        return _snapshot_from_row(row)

    def load_snapshot_by_case_id(self, case_id: str) -> EngagementSnapshotV2 | None:
        row = self._fetch_one(
            """
            SELECT engagement_id, case_id, version, snapshot_data, last_trace_id, updated_at
            FROM operator_engagement_snapshots
            WHERE case_id = %(case_id)s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            {"case_id": str(case_id or "").strip()},
        )
        if not row:
            return None
        return _snapshot_from_row(row)

    def list_recent_snapshots(self, *, limit: int = 50) -> list[EngagementSnapshotV2]:
        rows = self._fetch_all(
            """
            SELECT engagement_id, case_id, version, snapshot_data, last_trace_id, updated_at
            FROM operator_engagement_snapshots
            ORDER BY updated_at DESC
            LIMIT %(limit)s
            """,
            {"limit": max(1, int(limit))},
        )
        return [_snapshot_from_row(row) for row in rows]

    def list_recent_snapshots_with_updated_at(
        self, *, limit: int = 50
    ) -> list[tuple[EngagementSnapshotV2, str]]:
        # Prefer active rows only when `status` exists (AGENT_RUNTIME_SCHEMA); fall back
        # if older DBs lack the column — soft_delete already handles that path.
        try:
            rows = self._fetch_all(
                """
                SELECT engagement_id, case_id, version, snapshot_data, last_trace_id, updated_at
                FROM operator_engagement_snapshots
                WHERE COALESCE(status, 'active') NOT IN ('expired', 'materialized')
                ORDER BY updated_at DESC
                LIMIT %(limit)s
                """,
                {"limit": max(1, int(limit))},
            )
        except Exception:
            rows = self._fetch_all(
                """
                SELECT engagement_id, case_id, version, snapshot_data, last_trace_id, updated_at
                FROM operator_engagement_snapshots
                ORDER BY updated_at DESC
                LIMIT %(limit)s
                """,
                {"limit": max(1, int(limit))},
            )
        return [
            (_snapshot_from_row(row), str(row.get("updated_at") or ""))
            for row in rows
        ]

    def list_staging_engagement_ids(self) -> list[str]:
        rows = self._fetch_all(
            """
            SELECT engagement_id FROM operator_engagement_snapshots
            WHERE engagement_id LIKE 'stg_%%' AND COALESCE(case_id, '') = ''
            """,
            {},
        )
        return [str(row.get("engagement_id") or "") for row in rows if row.get("engagement_id")]

    def soft_delete_snapshot(self, engagement_id: str) -> None:
        """Mark snapshot as expired instead of hard-deleting."""
        eid = str(engagement_id or "").strip()
        if not eid:
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "UPDATE operator_engagement_snapshots SET status = 'expired', expired_at = NOW() WHERE engagement_id = %(eid)s",
                        {"eid": eid},
                    )
                    if cur.rowcount == 0:
                        # No status column yet — fallback to delete
                        cur.execute(
                            "DELETE FROM operator_engagement_snapshots WHERE engagement_id = %(eid)s",
                            {"eid": eid},
                        )
                except Exception as exc:
                    logger.warning(
                        "soft_delete_snapshot status update failed eid=%s - falling back to delete exc=%s",
                        eid, exc,
                    )
                    # Fallback to hard delete if status column doesn't exist
                    cur.execute(
                        "DELETE FROM operator_engagement_snapshots WHERE engagement_id = %(eid)s",
                        {"eid": eid},
                    )
            conn.commit()

    def save_snapshot(self, snapshot: EngagementSnapshotV2, expected_version: int) -> int:
        engagement_id = str(snapshot.engagement_id or "").strip()
        if not engagement_id:
            raise ValueError("engagement_id is required")
        new_version = int(expected_version) + 1

        # PR-8J: diff-based storage — oblicz różnicę względem poprzedniego snapshotu
        prev_row = self._fetch_one(
            "SELECT snapshot_data FROM operator_engagement_snapshots WHERE engagement_id = %(engagement_id)s",
            {"engagement_id": engagement_id},
        )
        previous_payload = _row_snapshot_payload(prev_row)
        stamped = _apply_lifecycle_state_since(snapshot, previous_payload=previous_payload)
        updated = stamped.model_copy(update={"version": new_version})
        EngagementSnapshotV2.model_validate(updated.model_dump(mode="python"))
        payload = updated.model_dump(mode="python")

        snapshot_diff: str | None = None
        if previous_payload is not None and isinstance(payload, dict):
            diff = _compute_diff(dict(previous_payload), payload)
            if diff:
                snapshot_diff = json.dumps(diff, ensure_ascii=False, default=str)

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE operator_engagement_snapshots
                    SET
                        case_id = %(case_id)s,
                        version = %(new_version)s,
                        snapshot_data = %(snapshot_data)s::jsonb,
                        snapshot_diff = %(snapshot_diff)s::jsonb,
                        last_trace_id = %(last_trace_id)s,
                        updated_at = NOW()
                    WHERE engagement_id = %(engagement_id)s
                      AND version = %(expected_version)s
                    """,
                    {
                        "engagement_id": engagement_id,
                        "case_id": updated.case_id,
                        "new_version": new_version,
                        "expected_version": int(expected_version),
                        "snapshot_data": json.dumps(payload, ensure_ascii=False),
                        "snapshot_diff": snapshot_diff,
                        "last_trace_id": updated.trace_id,
                    },
                )
                if cur.rowcount != 1:
                    raise AgentConcurrencyError(
                        f"version conflict for {engagement_id!r}: expected={expected_version}"
                    )
            conn.commit()
        return new_version

    def insert_snapshot(self, snapshot: EngagementSnapshotV2) -> EngagementSnapshotV2:
        eid = str(snapshot.engagement_id or "").strip()
        if not eid:
            raise ValueError("engagement_id is required")
        if int(snapshot.version) != 1:
            raise ValueError("insert_snapshot requires version=1")
        stamped = _apply_lifecycle_state_since(snapshot, previous_payload=None)
        payload = stamped.model_dump(mode="python")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO operator_engagement_snapshots (
                        engagement_id, case_id, version, snapshot_data, last_trace_id, updated_at
                    ) VALUES (
                        %(engagement_id)s, %(case_id)s, 1, %(snapshot_data)s::jsonb,
                        %(last_trace_id)s, NOW()
                    )
                    ON CONFLICT (engagement_id) DO NOTHING
                    """,
                    {
                        "engagement_id": eid,
                        "case_id": stamped.case_id,
                        "snapshot_data": json.dumps(payload, ensure_ascii=False),
                        "last_trace_id": stamped.trace_id,
                    },
                )
                if cur.rowcount != 1:
                    raise AgentConcurrencyError(f"snapshot already exists for {eid!r}")
            conn.commit()
        return stamped

    def init_snapshot_from_signal(
        self,
        *,
        signal: Mapping[str, Any],
        case_id: str,
        engagement_id: str,
        trace_id: str | None = None,
    ) -> EngagementSnapshotV2:
        snapshot = build_snapshot_from_signal(
            signal=signal,
            case_id=case_id,
            engagement_id=engagement_id,
            signal_id=str(signal.get("signal_id") or "").strip(),
            trace_id=trace_id,
        )
        return self.insert_snapshot(snapshot)

    def _fetch_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
        return dict(row) if row else None

    def _fetch_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        return [dict(row) for row in rows]

    def _connect(self, *, row_factory: bool = False):
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required for Postgres operator engagement store.") from exc
        kwargs: dict[str, Any] = {"connect_timeout": POSTGRES_CONNECT_TIMEOUT_SEC}
        if row_factory:
            from psycopg.rows import dict_row  # type: ignore[import-not-found]

            kwargs["row_factory"] = dict_row
        return psycopg.connect(self.database_url, **kwargs)


def _operational_status_code_from_payload(payload: Mapping[str, Any] | None) -> str:
    if not isinstance(payload, Mapping):
        return ""
    ops = payload.get("operational_status")
    if isinstance(ops, Mapping):
        return str(ops.get("code") or "").strip().lower()
    return ""


def _apply_lifecycle_state_since(
    snapshot: EngagementSnapshotV2,
    *,
    previous_payload: Mapping[str, Any] | None,
    now_iso: str | None = None,
) -> EngagementSnapshotV2:
    """Stamp or preserve `lifecycle_state_since` (FG-02).

    - First insert (`previous_payload is None`): seed to now unless already set.
    - Save when `operational_status.code` changes: bump to now.
    - Save when code is unchanged: keep the previous durable since (ignore blank caller dumps
      and unrelated mutations that would otherwise reset the stagnation clock via `updated_at`).
    """
    stamp = str(now_iso or "").strip() or _utc_now_iso()
    if previous_payload is None:
        existing = str(snapshot.lifecycle_state_since or "").strip()
        return snapshot if existing else snapshot.model_copy(update={"lifecycle_state_since": stamp})

    new_code = str(snapshot.operational_status.code or "").strip().lower()
    prev_code = _operational_status_code_from_payload(previous_payload)
    if new_code != prev_code:
        return snapshot.model_copy(update={"lifecycle_state_since": stamp})

    prev_since = str(previous_payload.get("lifecycle_state_since") or "").strip()
    if prev_since:
        return snapshot.model_copy(update={"lifecycle_state_since": prev_since})
    # Legacy row never stamped: leave empty so readers fall back to row `updated_at`.
    return snapshot.model_copy(update={"lifecycle_state_since": ""})


def _row_snapshot_payload(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    raw = row.get("snapshot_data")
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    elif isinstance(raw, dict):
        data = dict(raw)
    else:
        return {}
    return data if isinstance(data, dict) else {}


def _compute_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Compute shallow diff between two dicts (PR-8J diff-based storage).

    Returns dict of {key: new_value} for keys that changed.
    Removed keys are represented as {key: None}.
    Nested dicts are compared by identity — nie pogłębiamy dla prostoty.
    """
    diff: dict[str, Any] = {}
    all_keys = set(old.keys()) | set(new.keys())
    for key in all_keys:
        old_val = old.get(key)
        new_val = new.get(key)
        if key in old and key not in new:
            diff[key] = None  # key removed
        elif old_val != new_val:
            diff[key] = new_val
    return diff


def _snapshot_from_row(row: Mapping[str, Any]) -> EngagementSnapshotV2:
    raw = row.get("snapshot_data")
    if isinstance(raw, str):
        data = json.loads(raw)
    elif isinstance(raw, dict):
        data = dict(raw)
    else:
        data = {}
    row_version = int(row.get("version") or data.get("version") or 1)
    data["version"] = row_version
    data.setdefault("engagement_id", str(row.get("engagement_id") or ""))
    data.setdefault("case_id", str(row.get("case_id") or ""))
    data["signal_id"] = str(data.get("signal_id") or data.get("trace_id") or "")
    trace = str(row.get("last_trace_id") or data.get("trace_id") or "")
    data["trace_id"] = trace
    return EngagementSnapshotV2.model_validate(data)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
