"""Temporal entity recall for agent tools (GraphStore PG)."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _rag_backend_on_path() -> None:
    workspace = Path(__file__).resolve().parents[4]
    backend = workspace / "rag-chat-asystent" / "backend"
    if backend.is_dir() and str(backend) not in sys.path:
        sys.path.insert(0, str(backend))


def graphstore_dsn() -> str:
    return str(
        os.environ.get("GRAPHSTORE_DSN")
        or os.environ.get("GRAPHSTORE_DATABASE_URL")
        or ""
    ).strip()


def recall_temporal_fact(
    *,
    entity_id: str,
    fact_key: str = "summary",
    as_of: datetime | None = None,
    limit: int = 1,
) -> dict[str, Any] | None:
    dsn = graphstore_dsn()
    if not dsn:
        return None
    _rag_backend_on_path()
    from infrastructure.storage.temporal_entity_graph import TemporalEntityGraph

    graph = TemporalEntityGraph(dsn)
    graph.ensure_schema()
    eid = entity_id if entity_id.startswith("ent_") else f"ent_case_{entity_id}"
    return graph.temporal_query(eid, fact_key, as_of=as_of or datetime.now(timezone.utc))


__all__ = ["graphstore_dsn", "recall_temporal_fact"]
