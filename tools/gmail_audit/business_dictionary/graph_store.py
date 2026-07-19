"""Neo4j graph integration for Business Dictionary.

Stores business terms as Neo4j nodes with labeled relationships between them.
Uses existing Neo4j pilot connection infrastructure.
"""
from __future__ import annotations

from typing import Any

try:
    from .._protocols import DatabaseConnection
except ImportError:
    from _protocols import DatabaseConnection  # type: ignore[no-redef]

from business_dictionary.model import BusinessTerm, BusinessTermGraph
from log_config import get_logger

logger = get_logger(__name__)


def _neo4j_driver(settings: Any):
    """Get Neo4j driver from existing pilot infrastructure."""
    try:
        from neo4j_pilot import build_neo4j_pilot

        pilot = build_neo4j_pilot(settings)
        if pilot is None:
            return None
        return getattr(pilot, "driver", None)
    except Exception as exc:
        logger.warning("Neo4j not available: %s", exc)
        return None


def upsert_term_node(settings: Any, term: BusinessTerm) -> bool:
    """Create or update a BusinessTerm node in Neo4j with relationships."""
    driver = _neo4j_driver(settings)
    if driver is None:
        return False

    try:
        with driver.session() as session:
            session.run(
                """
                MERGE (t:BusinessTerm {term_id: $term_id})
                SET t.name = $name,
                    t.category = $category,
                    t.definition = $definition,
                    t.source_kind = $source_kind,
                    t.updated_at = $updated_at
                """,
                term_id=term.term_id,
                name=term.name,
                category=term.category,
                definition=term.definition[:500],
                source_kind=term.source_kind,
                updated_at=term.updated_at or "",
            )

            # Create alias nodes and link
            for alias in (term.aliases or []):
                alias_str = str(alias).strip()
                if alias_str:
                    session.run(
                        """
                        MERGE (a:Alias {name: $alias})
                        MERGE (t:BusinessTerm {term_id: $term_id})
                        MERGE (t)-[:HAS_ALIAS]->(a)
                        """,
                        alias=alias_str,
                        term_id=term.term_id,
                    )

            # Create related term links (by name resolution)
            for related in (term.related_terms or []):
                rel_name = str(related).strip()
                if rel_name:
                    session.run(
                        """
                        MATCH (t:BusinessTerm {term_id: $term_id})
                        OPTIONAL MATCH (r:BusinessTerm {name: $related_name})
                        FOREACH (_ IN CASE WHEN r IS NOT NULL THEN [1] ELSE [] END |
                            MERGE (t)-[:RELATED_TO]->(r)
                        )
                        """,
                        term_id=term.term_id,
                        related_name=rel_name,
                    )
        return True
    except Exception as exc:
        logger.warning("Neo4j upsert failed for term %s: %s", term.term_id, exc)
        return False


def search_graph(settings: Any, *, query: str = "", category: str = "", limit: int = 30) -> list[BusinessTermGraph]:
    """Search the Neo4j graph for terms and their relationships."""
    driver = _neo4j_driver(settings)
    if driver is None:
        return []

    results: list[BusinessTermGraph] = []
    try:
        with driver.session() as session:
            if query:
                rows = session.run(
                    """
                    MATCH (t:BusinessTerm)
                    WHERE t.name CONTAINS $query OR t.definition CONTAINS $query
                    OPTIONAL MATCH (t)-[r]-(related)
                    RETURN t.term_id AS term_id, t.name AS name, t.category AS category,
                           collect(DISTINCT {rel: type(r), target: related.name}) AS relationships
                    LIMIT $limit
                    """,
                    query=query,
                    limit=max(1, int(limit)),
                )
            elif category:
                rows = session.run(
                    """
                    MATCH (t:BusinessTerm {category: $category})
                    OPTIONAL MATCH (t)-[r]-(related)
                    RETURN t.term_id AS term_id, t.name AS name, t.category AS category,
                           collect(DISTINCT {rel: type(r), target: related.name}) AS relationships
                    LIMIT $limit
                    """,
                    category=category,
                    limit=max(1, int(limit)),
                )
            else:
                rows = session.run(
                    """
                    MATCH (t:BusinessTerm)
                    OPTIONAL MATCH (t)-[r]-(related)
                    RETURN t.term_id AS term_id, t.name AS name, t.category AS category,
                           collect(DISTINCT {rel: type(r), target: related.name}) AS relationships
                    LIMIT $limit
                    """
                )

            for record in rows:
                rels = list(record.get("relationships") or [])
                clean_rels = [
                    {"target_term_id": r.get("target", ""), "relation_type": r.get("rel", ""), "weight": 1.0}
                    for r in rels
                    if r.get("target")
                ]
                results.append(BusinessTermGraph(
                    term_id=str(record.get("term_id") or ""),
                    name=str(record.get("name") or ""),
                    category=str(record.get("category") or ""),
                    relationships=clean_rels,
                ))

    except Exception as exc:
        logger.warning("Neo4j search failed: %s", exc)

    return results


def get_graph_stats(settings: Any) -> dict[str, int]:
    """Get Neo4j graph node/edge counts."""
    driver = _neo4j_driver(settings)
    if driver is None:
        return {"nodes": 0, "edges": 0}

    try:
        with driver.session() as session:
            nodes = session.run("MATCH (t:BusinessTerm) RETURN count(t) AS cnt").single()
            edges = session.run("MATCH (t:BusinessTerm)-[r]-() RETURN count(DISTINCT r) AS cnt").single()
            return {
                "nodes": nodes.get("cnt", 0) if nodes else 0,
                "edges": edges.get("cnt", 0) if edges else 0,
            }
    except Exception as exc:
        logger.warning("Neo4j stats failed: %s", exc)
        return {"nodes": 0, "edges": 0}


def process_outbox(conn: DatabaseConnection, settings: Any, *, limit: int = 50, dry_run: bool = False) -> dict[str, int]:
    """Process pending sync_outbox entries: replicate PG terms to Neo4j.

    Called after PG commit. Idempotent — each entry is processed once.
    """
    from business_dictionary.model import BusinessTerm
    from business_dictionary.store import ensure_sync_outbox_table

    ensure_sync_outbox_table(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, entity_type, entity_id, operation, payload FROM sync_outbox WHERE processed_at IS NULL ORDER BY created_at ASC LIMIT %s",
            (max(1, int(limit)),),
        )
        rows = cur.fetchall() or []

    stats = {"processed": 0, "failed": 0, "skipped": 0}
    for row in rows:
        oid = row[0] if not isinstance(row, dict) else row.get("id", "")
        entity_type = str(row[1] if not isinstance(row, dict) else row.get("entity_type", "") or "")
        operation = str(row[3] if not isinstance(row, dict) else row.get("operation", "") or "")
        payload = row[4] if not isinstance(row, dict) else row.get("payload", {})
        if isinstance(payload, str):
            try:
                import json
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}

        if entity_type != "business_term" or operation != "upsert":
            stats["skipped"] += 1
            _mark_outbox_processed(conn, oid)
            continue

        if dry_run:
            stats["skipped"] += 1
            continue

        try:
            term = BusinessTerm(
                term_id=str(payload.get("term_id", "")),
                name=str(payload.get("name", "")),
                category=str(payload.get("category", "")),
                definition=str(payload.get("definition", "")),
                source_document=str(payload.get("source_document", "")),
                source_kind=str(payload.get("source_kind", "")),
                aliases=list(payload.get("aliases") or []),
                related_terms=list(payload.get("related_terms") or []),
                confidence=float(payload.get("confidence", 0.0)),
            )
            ok = upsert_term_node(settings, term)
            if ok:
                _mark_outbox_processed(conn, oid)
                stats["processed"] += 1
            else:
                stats["failed"] += 1
        except Exception as exc:
            logger.warning("Outbox processing failed for %s: %s", oid, exc)
            stats["failed"] += 1

    conn.commit()
    return stats


def _mark_outbox_processed(conn: DatabaseConnection, outbox_id: str) -> None:
    """Mark a single outbox entry as processed."""
    from datetime import datetime, timezone
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sync_outbox SET processed_at = %s WHERE id = %s",
            (datetime.now(timezone.utc).isoformat(), outbox_id),
        )
