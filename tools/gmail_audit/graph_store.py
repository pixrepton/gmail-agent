"""Operational graph substrate for Drive/Gmail shared memory in Postgres."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


GRAPH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_nodes (
    node_id TEXT PRIMARY KEY,
    node_type TEXT NOT NULL,
    natural_key TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_nodes_type_natural_key ON graph_nodes(node_type, natural_key);

CREATE TABLE IF NOT EXISTS graph_edges (
    edge_id TEXT PRIMARY KEY,
    src_node_id TEXT NOT NULL,
    dst_node_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_graph_edges_src ON graph_edges(src_node_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_dst ON graph_edges(dst_node_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_edges_relation_identity ON graph_edges(src_node_id, dst_node_id, relation_type, source_ref);
"""


class GraphStore(Protocol):
    def bootstrap(self) -> None: ...
    def upsert_node(self, row: dict[str, Any]) -> None: ...
    def upsert_edge(self, row: dict[str, Any]) -> None: ...
    def upsert_many(self, *, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None: ...
    def fetch_case_hints(self, case_id: str, *, limit: int = 20) -> list[dict[str, Any]]: ...


@dataclass(slots=True)
class InMemoryGraphStore:
    nodes: dict[str, dict[str, Any]] | None = None
    edges: dict[str, dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self.nodes = self.nodes or {}
        self.edges = self.edges or {}

    def bootstrap(self) -> None:
        return None

    def upsert_node(self, row: dict[str, Any]) -> None:
        node_id = str(row.get("node_id") or "").strip()
        if node_id:
            self.nodes[node_id] = dict(row)

    def upsert_edge(self, row: dict[str, Any]) -> None:
        edge_id = str(row.get("edge_id") or "").strip()
        if edge_id:
            self.edges[edge_id] = dict(row)

    def upsert_many(self, *, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
        for row in nodes:
            self.upsert_node(row)
        for row in edges:
            self.upsert_edge(row)

    def fetch_case_hints(self, case_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        case_node_id = stable_graph_node_id("Case", case_id)
        hints: list[dict[str, Any]] = []
        for edge in self.edges.values():
            src = str(edge.get("src_node_id") or "")
            dst = str(edge.get("dst_node_id") or "")
            if src != case_node_id and dst != case_node_id:
                continue
            related_id = dst if src == case_node_id else src
            related_node = self.nodes.get(related_id) or {}
            hints.append(
                {
                    "relation_type": str(edge.get("relation_type") or ""),
                    "related_node_id": related_id,
                    "related_node_type": str(related_node.get("node_type") or ""),
                    "related_title": str(related_node.get("title") or ""),
                    "confidence": float(edge.get("confidence") or 0.0),
                    "source_ref": str(edge.get("source_ref") or ""),
                    "metadata": dict(edge.get("metadata") or {}),
                }
            )
        hints.sort(key=lambda item: item["confidence"], reverse=True)
        return hints[:limit]


class PostgresGraphStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = str(database_url or "").strip()
        if not self.database_url:
            raise ValueError("database_url is required for PostgresGraphStore")

    def bootstrap(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(GRAPH_SCHEMA_SQL)
            conn.commit()

    def upsert_node(self, row: dict[str, Any]) -> None:
        self._upsert(
            """
            INSERT INTO graph_nodes (
                node_id, node_type, natural_key, title, source, source_ref, confidence, payload, created_at, updated_at
            ) VALUES (
                %(node_id)s, %(node_type)s, %(natural_key)s, %(title)s, %(source)s, %(source_ref)s, %(confidence)s, %(payload)s::jsonb, %(created_at)s, %(updated_at)s
            )
            ON CONFLICT (node_id) DO UPDATE SET
                node_type = EXCLUDED.node_type,
                natural_key = EXCLUDED.natural_key,
                title = EXCLUDED.title,
                source = EXCLUDED.source,
                source_ref = EXCLUDED.source_ref,
                confidence = EXCLUDED.confidence,
                payload = EXCLUDED.payload,
                updated_at = EXCLUDED.updated_at
            """,
            _prep(row, json_fields={"payload"}, time_fields={"created_at", "updated_at"}),
        )

    def upsert_edge(self, row: dict[str, Any]) -> None:
        self._upsert(
            """
            INSERT INTO graph_edges (
                edge_id, src_node_id, dst_node_id, relation_type, source, source_ref, confidence, metadata, created_at, updated_at
            ) VALUES (
                %(edge_id)s, %(src_node_id)s, %(dst_node_id)s, %(relation_type)s, %(source)s, %(source_ref)s, %(confidence)s, %(metadata)s::jsonb, %(created_at)s, %(updated_at)s
            )
            ON CONFLICT (edge_id) DO UPDATE SET
                src_node_id = EXCLUDED.src_node_id,
                dst_node_id = EXCLUDED.dst_node_id,
                relation_type = EXCLUDED.relation_type,
                source = EXCLUDED.source,
                source_ref = EXCLUDED.source_ref,
                confidence = EXCLUDED.confidence,
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at
            """,
            _prep(row, json_fields={"metadata"}, time_fields={"created_at", "updated_at"}),
        )

    def upsert_many(self, *, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                if nodes:
                    cur.executemany(
                        """
                        INSERT INTO graph_nodes (
                            node_id, node_type, natural_key, title, source, source_ref, confidence, payload, created_at, updated_at
                        ) VALUES (
                            %(node_id)s, %(node_type)s, %(natural_key)s, %(title)s, %(source)s, %(source_ref)s, %(confidence)s, %(payload)s::jsonb, %(created_at)s, %(updated_at)s
                        )
                        ON CONFLICT (node_id) DO UPDATE SET
                            node_type = EXCLUDED.node_type,
                            natural_key = EXCLUDED.natural_key,
                            title = EXCLUDED.title,
                            source = EXCLUDED.source,
                            source_ref = EXCLUDED.source_ref,
                            confidence = EXCLUDED.confidence,
                            payload = EXCLUDED.payload,
                            updated_at = EXCLUDED.updated_at
                        """,
                        [_prep(row, json_fields={"payload"}, time_fields={"created_at", "updated_at"}) for row in nodes],
                    )
                if edges:
                    cur.executemany(
                        """
                        INSERT INTO graph_edges (
                            edge_id, src_node_id, dst_node_id, relation_type, source, source_ref, confidence, metadata, created_at, updated_at
                        ) VALUES (
                            %(edge_id)s, %(src_node_id)s, %(dst_node_id)s, %(relation_type)s, %(source)s, %(source_ref)s, %(confidence)s, %(metadata)s::jsonb, %(created_at)s, %(updated_at)s
                        )
                        ON CONFLICT (edge_id) DO UPDATE SET
                            src_node_id = EXCLUDED.src_node_id,
                            dst_node_id = EXCLUDED.dst_node_id,
                            relation_type = EXCLUDED.relation_type,
                            source = EXCLUDED.source,
                            source_ref = EXCLUDED.source_ref,
                            confidence = EXCLUDED.confidence,
                            metadata = EXCLUDED.metadata,
                            updated_at = EXCLUDED.updated_at
                        """,
                        [_prep(row, json_fields={"metadata"}, time_fields={"created_at", "updated_at"}) for row in edges],
                    )
            conn.commit()

    def fetch_case_hints(self, case_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        case_node_id = stable_graph_node_id("Case", case_id)
        return self._fetch_all(
            """
            WITH case_edges AS (
                SELECT
                    e.edge_id,
                    e.relation_type,
                    e.confidence,
                    e.source_ref,
                    e.metadata,
                    CASE WHEN e.src_node_id = %(case_node_id)s THEN e.dst_node_id ELSE e.src_node_id END AS related_node_id
                FROM graph_edges e
                WHERE e.src_node_id = %(case_node_id)s OR e.dst_node_id = %(case_node_id)s
                ORDER BY e.confidence DESC, e.updated_at DESC
                LIMIT %(limit)s
            )
            SELECT
                ce.relation_type,
                ce.related_node_id,
                n.node_type AS related_node_type,
                n.title AS related_title,
                ce.confidence,
                ce.source_ref,
                ce.metadata
            FROM case_edges ce
            LEFT JOIN graph_nodes n ON n.node_id = ce.related_node_id
            ORDER BY ce.confidence DESC
            """,
            {"case_node_id": case_node_id, "limit": limit},
        )

    def _upsert(self, sql: str, params: dict[str, Any]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()

    def _fetch_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() or []
        return [dict(row) for row in rows]

    def _connect(self, *, row_factory: bool = False):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - handled by runtime
            raise RuntimeError("psycopg is required for PostgresGraphStore") from exc

        kwargs: dict[str, Any] = {}
        if row_factory:
            kwargs["row_factory"] = dict_row
        return psycopg.connect(self.database_url, **kwargs)


def stable_graph_node_id(node_type: str, natural_key: str) -> str:
    digest = hashlib.sha1(f"{node_type}::{natural_key}".encode("utf-8")).hexdigest()[:12]
    return f"gnd_{digest}"


def stable_graph_edge_id(src_node_id: str, relation_type: str, dst_node_id: str, source_ref: str) -> str:
    digest = hashlib.sha1(f"{src_node_id}::{relation_type}::{dst_node_id}::{source_ref}".encode("utf-8")).hexdigest()[:12]
    return f"ged_{digest}"


def build_graph_node(
    *,
    node_type: str,
    natural_key: str,
    title: str,
    source: str,
    source_ref: str,
    confidence: float,
    payload: dict[str, Any] | None = None,
    observed_at: str = "",
) -> dict[str, Any]:
    timestamp = observed_at or datetime.now().astimezone().isoformat()
    return {
        "node_id": stable_graph_node_id(node_type, natural_key),
        "node_type": node_type,
        "natural_key": natural_key,
        "title": title,
        "source": source,
        "source_ref": source_ref,
        "confidence": round(max(0.0, min(1.0, float(confidence))), 4),
        "payload": payload or {},
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def build_graph_edge(
    *,
    src_node_id: str,
    dst_node_id: str,
    relation_type: str,
    source: str,
    source_ref: str,
    confidence: float,
    metadata: dict[str, Any] | None = None,
    observed_at: str = "",
) -> dict[str, Any]:
    timestamp = observed_at or datetime.now().astimezone().isoformat()
    return {
        "edge_id": stable_graph_edge_id(src_node_id, relation_type, dst_node_id, source_ref),
        "src_node_id": src_node_id,
        "dst_node_id": dst_node_id,
        "relation_type": relation_type,
        "source": source,
        "source_ref": source_ref,
        "confidence": round(max(0.0, min(1.0, float(confidence))), 4),
        "metadata": metadata or {},
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _coerce_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _prep(params: dict[str, Any], *, json_fields: set[str], time_fields: set[str]) -> dict[str, Any]:
    prepared = dict(params)
    now = datetime.now().astimezone()
    for field_name in json_fields:
        prepared[field_name] = json.dumps(prepared.get(field_name), ensure_ascii=False)
    for field_name in time_fields:
        prepared[field_name] = _coerce_iso(prepared.get(field_name)) or now
    return prepared


__all__ = [
    "GRAPH_SCHEMA_SQL",
    "GraphStore",
    "InMemoryGraphStore",
    "PostgresGraphStore",
    "build_graph_edge",
    "build_graph_node",
    "stable_graph_edge_id",
    "stable_graph_node_id",
]
