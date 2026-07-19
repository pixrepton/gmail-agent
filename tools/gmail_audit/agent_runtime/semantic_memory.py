"""Semantic memory (RAG) hooks for constitution hot-reload (PR-B+)."""

from __future__ import annotations

from typing import Any


def fetch_constitution_rag_chunks(
    query: str,
    *,
    database_url: str = "",
    limit: int = 5,
) -> list[dict[str, Any]]:
    """
    Return pgvector/RAG chunks for constitution augmentation.
    Degrades to empty list when vector store is unavailable (prep/local).
    """
    _ = (query, database_url, limit)
    return []
