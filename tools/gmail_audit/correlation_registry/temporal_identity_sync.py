"""Identity L3 merge → Temporal Entity Graph sync (W3/W5)."""

from __future__ import annotations

from typing import Any


def _graphstore_dsn(settings: Any | None = None) -> str:
    if settings is None:
        from config import load_settings

        settings = load_settings(require_groq=False, require_google=False)
    return str(
        getattr(settings, "graphstore_database_url", "")
        or getattr(settings, "graph_store_dsn", "")
        or __import__("os").environ.get("GRAPHSTORE_DSN")
        or __import__("os").environ.get("GRAPHSTORE_DATABASE_URL")
        or ""
    ).strip()


def sync_identity_merge_to_temporal_graph(
    merge_result: dict[str, Any],
    *,
    store: Any,
    settings: Any | None = None,
) -> dict[str, Any]:
    """Post execute_identity_merge — upsert entity + canonical name fact in GraphStore."""
    tgt_id = str(merge_result.get("target_identity_id") or "").strip()
    log_id = str(merge_result.get("log_id") or "").strip()
    if not tgt_id:
        return {"ok": False, "skipped": True, "reason": "missing target_identity_id"}

    dsn = _graphstore_dsn(settings)
    if not dsn:
        return {"ok": False, "skipped": True, "reason": "GRAPHSTORE_DSN not configured"}

    canonical_name = tgt_id
    get_identity = getattr(store, "get_identity", None)
    if callable(get_identity):
        row = get_identity(identity_id=tgt_id) or get_identity(tgt_id)
        if isinstance(row, dict):
            canonical_name = str(
                row.get("display_name") or row.get("canonical_name") or canonical_name
            ).strip()

    try:
        from infrastructure.storage.temporal_entity_graph import TemporalEntityGraph
    except ImportError:
        import sys
        from pathlib import Path

        rag_backend = Path(__file__).resolve().parents[4] / "rag-chat-asystent" / "backend"
        if str(rag_backend) not in sys.path:
            sys.path.insert(0, str(rag_backend))
        from infrastructure.storage.temporal_entity_graph import TemporalEntityGraph

    graph = TemporalEntityGraph(dsn)
    graph.ensure_schema()
    graph.sync_identity_merge(
        entity_id=f"ent_{tgt_id}",
        canonical_name=canonical_name,
        identity_id=tgt_id,
        merge_log_id=log_id,
    )
    return {
        "ok": True,
        "entity_id": f"ent_{tgt_id}",
        "identity_id": tgt_id,
        "merge_log_id": log_id,
    }


__all__ = ["sync_identity_merge_to_temporal_graph"]
