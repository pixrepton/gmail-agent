"""Pydantic models for Business Dictionary terms."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class BusinessTerm:
    """A single business term extracted from company documents."""

    term_id: str
    name: str
    category: str  # product, service, pricing, term, rule, template, contact
    definition: str
    source_document: str  # file path or engagement_id
    source_kind: str  # drive, gmail, manual
    aliases: list[str] = field(default_factory=list)
    related_terms: list[str] = field(default_factory=list)
    confidence: float = 0.0  # 0.0-1.0 extraction confidence
    created_at: str = ""
    updated_at: str = ""


@dataclass
class BusinessTermGraph:
    """Neo4j graph representation of term relationships."""

    term_id: str
    name: str
    category: str
    relationships: list[dict[str, Any]] = field(default_factory=list)
    # relationships: [{"target_term_id": "...", "relation_type": "is_a|has_part|related_to|priced_at", "weight": 1.0}]


@dataclass
class BusinessDictionaryStats:
    """Aggregated statistics for the business dictionary."""

    total_terms: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    by_source: dict[str, int] = field(default_factory=dict)
    last_extracted_at: str = ""
    neo4j_nodes: int = 0
    neo4j_edges: int = 0
