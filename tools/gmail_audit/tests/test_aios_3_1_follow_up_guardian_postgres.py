"""FG-04 — Follow-up Guardian beyond InMemory (Postgres reader + tick).

Proof levels:
* Hermetic: `PostgresOperatorEngagementStore.list_recent_snapshots_with_updated_at` SQL/shape
  via mocked `_fetch_all` (always runs in Gate A).
* Live Postgres: list_recent + tick propose when a DB URL (or local Docker default) is reachable;
  otherwise `pytest.skip` with an explicit reason.
* Does not claim live worker process proof (see `test_aios_3_1_follow_up_guardian_worker.py`).

Preserves FG-02 (`lifecycle_state_since` preferred) and FG-03 (closed-case skip remains in
InMemory suite — not re-proven against mailbox SoT here).
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_runtime.store import (  # noqa: E402
    PostgresOperatorEngagementStore,
    build_initial_snapshot,
)
from follow_up_guardian import (  # noqa: E402
    has_active_follow_up_proposal,
    run_follow_up_guardian_tick,
)
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2  # noqa: E402

_DEFAULT_LOCAL_DOCKER_URL = "postgresql://mailbox_memory:memorka@127.0.0.1:54129/mailbox_memory"
_CANDIDATE_ENV_KEYS = (
    "MAILBOX_MEMORY_TEST_DATABASE_URL",
    "MAILBOX_MEMORY_DATABASE_URL",
    "DATABASE_URL",
)


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _probe_url(url: str) -> bool:
    text = str(url or "").strip()
    if not text:
        return False
    try:
        import psycopg
    except ImportError:
        return False
    try:
        with psycopg.connect(text, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:
        return False


def _resolve_postgres_url() -> str | None:
    for key in _CANDIDATE_ENV_KEYS:
        candidate = str(os.environ.get(key) or "").strip()
        if candidate and _probe_url(candidate):
            return candidate
    if _probe_url(_DEFAULT_LOCAL_DOCKER_URL):
        return _DEFAULT_LOCAL_DOCKER_URL
    return None


_PG_URL = _resolve_postgres_url()
requires_postgres = pytest.mark.skipif(
    not _PG_URL,
    reason=(
        "FG-04 Postgres integration skipped: set MAILBOX_MEMORY_TEST_DATABASE_URL / "
        "MAILBOX_MEMORY_DATABASE_URL / DATABASE_URL, or start local mailbox-memory on :54129"
    ),
)


# ── Hermetic Postgres reader shape (no live DB) ───────────────────────────────────────────────


def test_postgres_list_recent_snapshots_with_updated_at_sql_and_shape_hermetic(monkeypatch):
    """Strongest Gate A proof when live Postgres is unavailable: SQL contract + return shape."""
    store = PostgresOperatorEngagementStore("postgresql://fg04-hermetic/unused")
    snap = build_initial_snapshot(
        case_id="case_fg04_hermetic",
        engagement_id="eng_fg04_hermetic",
        trace_id="trace_fg04_hermetic",
    )
    captured: dict[str, object] = {}

    def _fake_fetch_all(sql: str, params: dict) -> list[dict]:
        captured["sql"] = sql
        captured["params"] = params
        return [
            {
                "engagement_id": snap.engagement_id,
                "case_id": snap.case_id,
                "version": 1,
                "snapshot_data": snap.model_dump(mode="python"),
                "last_trace_id": snap.trace_id,
                "updated_at": "2026-01-01 12:00:00+00:00",
            }
        ]

    monkeypatch.setattr(store, "_fetch_all", _fake_fetch_all)
    rows = store.list_recent_snapshots_with_updated_at(limit=7)

    sql = str(captured.get("sql") or "")
    assert "operator_engagement_snapshots" in sql
    assert "updated_at" in sql
    assert "ORDER BY updated_at DESC" in sql
    assert "expired" in sql and "materialized" in sql
    assert captured.get("params") == {"limit": 7}
    assert len(rows) == 1
    out_snap, updated_at = rows[0]
    assert isinstance(out_snap, EngagementSnapshotV2)
    assert out_snap.engagement_id == "eng_fg04_hermetic"
    assert out_snap.case_id == "case_fg04_hermetic"
    assert updated_at == "2026-01-01 12:00:00+00:00"


def test_postgres_list_recent_falls_back_when_status_column_missing(monkeypatch):
    store = PostgresOperatorEngagementStore("postgresql://fg04-hermetic/unused")
    snap = build_initial_snapshot(
        case_id="case_fg04_fb", engagement_id="eng_fg04_fb", trace_id="t_fb"
    )
    calls: list[str] = []

    def _fake_fetch_all(sql: str, params: dict) -> list[dict]:
        calls.append(sql)
        if len(calls) == 1:
            raise RuntimeError("column status does not exist")
        return [
            {
                "engagement_id": snap.engagement_id,
                "case_id": snap.case_id,
                "version": 1,
                "snapshot_data": snap.model_dump(mode="python"),
                "last_trace_id": snap.trace_id,
                "updated_at": "2026-02-02T00:00:00+00:00",
            }
        ]

    monkeypatch.setattr(store, "_fetch_all", _fake_fetch_all)
    rows = store.list_recent_snapshots_with_updated_at(limit=3)
    assert len(calls) == 2
    assert "status" not in calls[1] or "COALESCE(status" not in calls[1]
    assert len(rows) == 1
    assert rows[0][1] == "2026-02-02T00:00:00+00:00"


# ── Live Postgres (skip when unavailable) ─────────────────────────────────────────────────────


def _seed_aged(
    store: PostgresOperatorEngagementStore,
    *,
    engagement_id: str,
    case_id: str,
    status_code: str,
    hours_ago: float,
) -> None:
    snapshot = build_initial_snapshot(
        case_id=case_id, engagement_id=engagement_id, trace_id=f"trace_{engagement_id}"
    )
    snapshot = snapshot.model_copy(
        update={
            "operational_status": snapshot.operational_status.model_copy(
                update={"code": status_code}
            )
        }
    )
    store.insert_snapshot(snapshot)
    aged = _iso(hours_ago)
    with store._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE operator_engagement_snapshots
                SET
                    updated_at = %(aged)s::timestamptz,
                    snapshot_data = jsonb_set(
                        COALESCE(snapshot_data, '{}'::jsonb),
                        '{lifecycle_state_since}',
                        to_jsonb(%(aged)s::text)
                    )
                WHERE engagement_id = %(engagement_id)s
                """,
                {"aged": aged, "engagement_id": engagement_id},
            )
        conn.commit()


def _cleanup(store: PostgresOperatorEngagementStore, engagement_ids: list[str]) -> None:
    with store._connect() as conn:
        with conn.cursor() as cur:
            for eid in engagement_ids:
                cur.execute(
                    "DELETE FROM operator_engagement_snapshots WHERE engagement_id = %(eid)s",
                    {"eid": eid},
                )
        conn.commit()


#: Busy local DBs can have hundreds of newer rows; keep FG-04 seeds visible.
_PG_SCAN_LIMIT = 5000


@requires_postgres
def test_postgres_list_recent_snapshots_with_updated_at_live_shape():
    assert _PG_URL
    store = PostgresOperatorEngagementStore(_PG_URL)
    store.bootstrap()
    unique = uuid.uuid4().hex[:10]
    eid = f"fg04_list_{unique}"
    cid = f"case_fg04_list_{unique}"
    try:
        # Recent updated_at so the row ranks near the top of ORDER BY updated_at DESC;
        # lifecycle_state_since still set (shape proof for both fields).
        _seed_aged(
            store,
            engagement_id=eid,
            case_id=cid,
            status_code="enriching",
            hours_ago=0.05,
        )
        rows = store.list_recent_snapshots_with_updated_at(limit=_PG_SCAN_LIMIT)
        match = [(snap, updated_at) for snap, updated_at in rows if snap.engagement_id == eid]
        assert len(match) == 1, f"seed {eid} missing from list_recent (got {len(rows)} rows)"
        snap, updated_at = match[0]
        assert snap.case_id == cid
        assert snap.operational_status.code == "enriching"
        assert str(updated_at).strip()
        assert str(snap.lifecycle_state_since or "").strip()
    finally:
        _cleanup(store, [eid])


@requires_postgres
def test_postgres_tick_proposes_follow_up_for_stagnating_case():
    assert _PG_URL
    store = PostgresOperatorEngagementStore(_PG_URL)
    store.bootstrap()
    unique = uuid.uuid4().hex[:10]
    eid = f"fg04_tick_{unique}"
    cid = f"case_fg04_tick_{unique}"
    try:
        _seed_aged(
            store,
            engagement_id=eid,
            case_id=cid,
            status_code="enriching",
            hours_ago=60.0,
        )
        # Promote row to the head of list_recent while keeping aged lifecycle_state_since (FG-02).
        with store._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE operator_engagement_snapshots
                    SET updated_at = NOW()
                    WHERE engagement_id = %(engagement_id)s
                    """,
                    {"engagement_id": eid},
                )
            conn.commit()

        result = run_follow_up_guardian_tick(store, limit=_PG_SCAN_LIMIT)
        assert result["ok"] is True
        assert cid in result["proposed_case_ids"]
        loaded = store.load_snapshot(eid)
        assert loaded is not None
        assert has_active_follow_up_proposal(loaded) is True
    finally:
        _cleanup(store, [eid])


@requires_postgres
def test_postgres_tick_prefers_lifecycle_state_since_over_fresh_updated_at():
    """FG-02 on Postgres: unrelated row bump must not clear stagnation."""
    assert _PG_URL
    store = PostgresOperatorEngagementStore(_PG_URL)
    store.bootstrap()
    unique = uuid.uuid4().hex[:10]
    eid = f"fg04_since_{unique}"
    cid = f"case_fg04_since_{unique}"
    try:
        _seed_aged(
            store,
            engagement_id=eid,
            case_id=cid,
            status_code="enriching",
            hours_ago=60.0,
        )
        with store._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE operator_engagement_snapshots
                    SET updated_at = NOW()
                    WHERE engagement_id = %(engagement_id)s
                    """,
                    {"engagement_id": eid},
                )
            conn.commit()

        result = run_follow_up_guardian_tick(store, limit=_PG_SCAN_LIMIT)
        assert result["ok"] is True
        assert cid in result["proposed_case_ids"]
        loaded = store.load_snapshot(eid)
        assert loaded is not None
        assert has_active_follow_up_proposal(loaded) is True
    finally:
        _cleanup(store, [eid])
