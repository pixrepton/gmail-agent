"""Bounded Neo4j GraphRAG pilot for case-context projection and retrieval.

This module is intentionally projection-only. Postgres/mailbox memory remains the
source of truth; Neo4j stores a bounded case-scoped helper graph for retrieval.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Protocol

from mailbox_memory.active_facts import fetch_current_facts_for_case, is_live_fact
from mailbox_memory_store import MailboxMemoryStore


NODE_LABELS = ("Case", "Message", "Document", "Contact", "Location")
RELATION_TYPES = (
    "HAS_MESSAGE",
    "HAS_DOCUMENT",
    "HAS_CONTACT",
    "HAS_LOCATION",
    "MENTIONS_LOCATION",
    "MESSAGE_HAS_DOCUMENT",
)
EXPLICIT_ANCHOR_MODES = ("document", "contact", "location")
ALL_ANCHOR_MODES = ("auto", "document", "contact", "location")
PATH_LIMIT = 50


class Neo4jPilotError(RuntimeError):
    """Raised when the bounded Neo4j pilot cannot proceed."""


@dataclass(slots=True, frozen=True)
class Neo4jPilotConfig:
    enabled: bool = False
    uri: str = ""
    username: str = ""
    password: str = ""
    database: str = "neo4j"

    @property
    def configured(self) -> bool:
        return bool(self.uri and self.username and self.password and self.database)


@dataclass(slots=True)
class Neo4jProjectionPayload:
    case_id: str
    nodes: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "node_count": len(self.nodes),
            "relationship_count": len(self.relationships),
            "warnings": list(self.warnings),
        }


class Neo4jPilotBackend(Protocol):
    def replace_case_projection(self, payload: Neo4jProjectionPayload) -> dict[str, Any]: ...
    def fetch_case_neighborhood(
        self,
        *,
        case_id: str,
        anchor_node_keys: list[str],
        max_hops: int,
        limit: int,
    ) -> dict[str, Any]: ...
    def close(self) -> None: ...


def build_neo4j_pilot_config(settings: Any) -> Neo4jPilotConfig:
    return Neo4jPilotConfig(
        enabled=bool(getattr(settings, "neo4j_pilot_enabled", False)),
        uri=str(getattr(settings, "neo4j_uri", "") or "").strip(),
        username=str(getattr(settings, "neo4j_username", "") or "").strip(),
        password=str(getattr(settings, "neo4j_password", "") or ""),
        database=str(getattr(settings, "neo4j_database", "") or "neo4j").strip() or "neo4j",
    )


def build_neo4j_pilot_connectivity_check(settings: Any) -> dict[str, Any]:
    """Doctor-style Neo4j readiness: disabled, misconfigured, unreachable, or ok."""
    from intake_policy import CHECK_STATUS_FAILED, CHECK_STATUS_OK, CHECK_STATUS_SKIPPED

    cfg = build_neo4j_pilot_config(settings)
    doc: dict[str, Any] = {
        "neo4j_pilot_enabled": cfg.enabled,
        "neo4j_uri": cfg.uri or None,
        "neo4j_database": cfg.database,
    }
    if not cfg.enabled:
        doc["status"] = CHECK_STATUS_SKIPPED
        doc["reason"] = "Neo4j pilot disabled (NEO4J_PILOT_ENABLED=0)."
        return doc
    if not cfg.configured:
        doc["status"] = CHECK_STATUS_FAILED
        doc["reason"] = (
            "NEO4J_PILOT_ENABLED=1 but Neo4j connection is incomplete "
            "(need NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE)."
        )
        return doc
    client = Neo4jPilotClient(cfg)
    try:
        client.verify_connectivity()
    except Neo4jPilotError as exc:
        doc["status"] = CHECK_STATUS_FAILED
        doc["error"] = str(exc)
        return doc
    except Exception as exc:  # pragma: no cover - driver/network dependent
        doc["status"] = CHECK_STATUS_FAILED
        doc["error"] = str(exc)
        return doc
    finally:
        client.close()
    doc["status"] = CHECK_STATUS_OK
    return doc


class Neo4jPilotClient:
    """Thin driver wrapper with case-scoped delete + recreate semantics."""

    def __init__(self, config: Neo4jPilotConfig) -> None:
        if not config.enabled:
            raise Neo4jPilotError("Neo4j pilot is disabled by NEO4J_PILOT_ENABLED=0.")
        if not config.configured:
            raise Neo4jPilotError(
                "Neo4j pilot is enabled but missing one of: NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE."
            )
        self.config = config
        self._driver = None

    def close(self) -> None:
        driver = self._driver
        if driver is not None:
            driver.close()
            self._driver = None

    def verify_connectivity(self) -> None:
        """Raise ``Neo4jPilotError`` if the server is unreachable or auth fails."""
        self._get_driver().verify_connectivity()

    def _get_driver(self):
        if self._driver is not None:
            return self._driver
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:  # pragma: no cover - exercised by runtime/proof only
            raise Neo4jPilotError(
                "Neo4j Python driver is not installed. Install tools/gmail_audit/requirements.txt first."
            ) from exc
        self._driver = GraphDatabase.driver(
            self.config.uri,
            auth=(self.config.username, self.config.password),
        )
        return self._driver

    def replace_case_projection(self, payload: Neo4jProjectionPayload) -> dict[str, Any]:
        driver = self._get_driver()
        grouped_nodes: dict[str, list[dict[str, Any]]] = {label: [] for label in NODE_LABELS}
        grouped_rels: dict[str, list[dict[str, Any]]] = {rel_type: [] for rel_type in RELATION_TYPES}
        for row in payload.nodes:
            label = str(row.get("label") or "")
            if label in grouped_nodes:
                grouped_nodes[label].append(dict(row.get("properties") or {}))
        for row in payload.relationships:
            rel_type = str(row.get("type") or "")
            if rel_type in grouped_rels:
                grouped_rels[rel_type].append(
                    {
                        "src_node_key": str(row.get("src_node_key") or ""),
                        "dst_node_key": str(row.get("dst_node_key") or ""),
                        "properties": dict(row.get("properties") or {}),
                    }
                )

        with driver.session(database=self.config.database) as session:
            session.run("MATCH (n {pilot_case_id: $case_id}) DETACH DELETE n", case_id=payload.case_id).consume()
            for label, rows in grouped_nodes.items():
                if not rows:
                    continue
                session.run(f"UNWIND $rows AS row CREATE (n:{label}) SET n = row", rows=rows).consume()
            for rel_type, rows in grouped_rels.items():
                if not rows:
                    continue
                session.run(
                    f"""
                    UNWIND $rows AS row
                    MATCH (src {{node_key: row.src_node_key, pilot_case_id: $case_id}})
                    MATCH (dst {{node_key: row.dst_node_key, pilot_case_id: $case_id}})
                    CREATE (src)-[r:{rel_type}]->(dst)
                    SET r = row.properties
                    """,
                    rows=rows,
                    case_id=payload.case_id,
                ).consume()
            node_count = session.run(
                "MATCH (n {pilot_case_id: $case_id}) RETURN count(n) AS c",
                case_id=payload.case_id,
            ).single(strict=True)["c"]
            relationship_count = session.run(
                "MATCH ()-[r {pilot_case_id: $case_id}]->() RETURN count(r) AS c",
                case_id=payload.case_id,
            ).single(strict=True)["c"]
        return {
            "status": "ok",
            "case_id": payload.case_id,
            "projected": True,
            "deleted_existing_subgraph": True,
            "node_count": int(node_count),
            "relationship_count": int(relationship_count),
            "warnings": list(payload.warnings),
        }

    def fetch_case_neighborhood(
        self,
        *,
        case_id: str,
        anchor_node_keys: list[str],
        max_hops: int,
        limit: int,
    ) -> dict[str, Any]:
        driver = self._get_driver()
        bounded_hops = max(1, min(4, int(max_hops or 1)))
        bounded_limit = max(1, min(PATH_LIMIT, int(limit or 10)))
        cypher_projection = """
            [node IN nodes(p) | {
                node_key: coalesce(node.node_key, ""),
                labels: labels(node),
                value: coalesce(node.title, node.file_name, node.message_id, node.email, node.address, node.city, node.case_id, ""),
                case_id: coalesce(node.case_id, ""),
                message_id: coalesce(node.message_id, ""),
                document_id: coalesce(node.document_id, ""),
                source_kind: coalesce(node.source_kind, ""),
                document_kind: coalesce(node.document_kind, ""),
                email: coalesce(node.email, ""),
                name: coalesce(node.name, ""),
                address: coalesce(node.address, ""),
                city: coalesce(node.city, "")
            }] AS path_nodes,
            [rel IN relationships(p) | type(rel)] AS rel_chain
        """
        case_paths_query = f"""
            MATCH p=(c:Case {{case_id: $case_id, pilot_case_id: $case_id}})-[*1..{bounded_hops}]-(n)
            WHERE n.pilot_case_id = $case_id
            RETURN {cypher_projection}
            LIMIT $limit
        """
        anchor_paths_query = f"""
            MATCH (a {{pilot_case_id: $case_id}})
            WHERE a.node_key IN $anchor_node_keys
            MATCH p=(a)-[*1..{bounded_hops}]-(n)
            WHERE n.pilot_case_id = $case_id
            RETURN {cypher_projection}
            LIMIT $limit
        """
        with driver.session(database=self.config.database) as session:
            anchor_nodes = [
                dict(row)
                for row in session.run(
                    """
                    MATCH (a {pilot_case_id: $case_id})
                    WHERE a.node_key IN $anchor_node_keys
                    RETURN
                        a.node_key AS node_key,
                        labels(a) AS labels,
                        coalesce(a.title, a.file_name, a.message_id, a.email, a.address, a.city, a.case_id, '') AS value,
                        coalesce(a.case_id, '') AS case_id,
                        coalesce(a.message_id, '') AS message_id,
                        coalesce(a.document_id, '') AS document_id,
                        coalesce(a.source_kind, '') AS source_kind,
                        coalesce(a.document_kind, '') AS document_kind,
                        coalesce(a.email, '') AS email,
                        coalesce(a.name, '') AS name,
                        coalesce(a.address, '') AS address,
                        coalesce(a.city, '') AS city
                    ORDER BY a.node_key ASC
                    """,
                    case_id=case_id,
                    anchor_node_keys=anchor_node_keys,
                )
            ]
            anchor_documents = [dict(item) for item in anchor_nodes if "Document" in list(item.get("labels") or [])]
            path_rows = [
                {"origin": "case", **dict(row)}
                for row in session.run(case_paths_query, case_id=case_id, limit=bounded_limit)
            ]
            if anchor_node_keys:
                path_rows.extend(
                    {"origin": "anchor", **dict(row)}
                    for row in session.run(
                        anchor_paths_query,
                        case_id=case_id,
                        anchor_node_keys=anchor_node_keys,
                        limit=bounded_limit,
                    )
                )
        compact_paths: list[dict[str, Any]] = []
        neighborhood_nodes: dict[str, dict[str, Any]] = {}
        seen_paths: set[tuple[Any, ...]] = set()
        for row in path_rows:
            path_nodes = list(row.get("path_nodes") or [])
            rel_chain = list(row.get("rel_chain") or [])
            signature = tuple([str(row.get("origin") or "")] + [str(item.get("node_key") or "") for item in path_nodes] + rel_chain)
            if signature in seen_paths:
                continue
            seen_paths.add(signature)
            compact_paths.append(
                {
                    "origin": str(row.get("origin") or ""),
                    "rel_chain": rel_chain,
                    "nodes": path_nodes,
                }
            )
            for node in path_nodes:
                key = str(node.get("node_key") or "")
                if key and key not in neighborhood_nodes:
                    neighborhood_nodes[key] = dict(node)
        return {
            "status": "ok",
            "case_id": case_id,
            "anchor_documents": anchor_documents,
            "anchor_nodes": anchor_nodes,
            "neighborhood_nodes": list(neighborhood_nodes.values()),
            "paths": compact_paths[:bounded_limit],
            "max_hops": bounded_hops,
            "limit": bounded_limit,
            "warnings": [],
        }


def build_case_projection_payload(*, store: MailboxMemoryStore, case_id: str) -> Neo4jProjectionPayload:
    case_row = store.fetch_case(case_id) or {}
    if not case_row:
        raise Neo4jPilotError(f"No mailbox-memory case found for Neo4j pilot projection: {case_id}")
    snapshot_row = store.fetch_snapshot(case_id) or {}
    snapshot = snapshot_row.get("snapshot_json") if isinstance(snapshot_row.get("snapshot_json"), dict) else snapshot_row
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    # Active-fact contract (FACT-02 / 4.2b): project live rows via store active fetch.
    case_facts = list(fetch_current_facts_for_case(store, case_id))
    mailbox_documents = list(store.fetch_documents_for_case(case_id, limit=50) or [])
    drive_documents = list(getattr(store, "fetch_drive_documents_for_case")(case_id, limit=50) or [])
    drive_facts = [
        fact for fact in list(getattr(store, "fetch_drive_facts_for_case")(case_id) or []) if is_live_fact(fact)
    ]
    messages = list(store.fetch_messages_for_case(case_id, limit=50) or [])
    payload = Neo4jProjectionPayload(case_id=case_id)
    nodes: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []

    case_node_key = _case_node_key(case_id)
    case_title = str(case_row.get("case_key") or case_row.get("subject") or case_id)
    nodes.append(
        _node_row(
            "Case",
            {
                "node_key": case_node_key,
                "pilot_case_id": case_id,
                "case_id": case_id,
                "case_key": str(case_row.get("case_key") or ""),
                "title": case_title,
                "subject": str(case_row.get("subject") or ""),
                "status": str((snapshot.get("status") or case_row.get("status") or "open")),
                "customer_name": str(case_row.get("customer_name") or ""),
                "customer_email": str(case_row.get("customer_email") or ""),
                "updated_at": str(snapshot.get("updated_at") or case_row.get("updated_at") or ""),
                "source_kind": "mailbox_memory_case",
            },
        )
    )

    for message in messages:
        message_id = str(message.get("message_id") or "").strip()
        if not message_id:
            continue
        message_node_key = _message_node_key(message_id)
        nodes.append(
            _node_row(
                "Message",
                {
                    "node_key": message_node_key,
                    "pilot_case_id": case_id,
                    "case_id": case_id,
                    "message_id": message_id,
                    "thread_id": str(message.get("thread_id") or ""),
                    "title": str(message.get("subject") or message_id),
                    "subject": str(message.get("subject") or ""),
                    "sender": str(message.get("sender") or ""),
                    "sender_email": str(message.get("sender_email") or ""),
                    "received_at": str(message.get("received_at") or ""),
                    "source_kind": "gmail_message",
                },
            )
        )
        relationships.append(
            _relationship_row(
                "HAS_MESSAGE",
                src_node_key=case_node_key,
                dst_node_key=message_node_key,
                case_id=case_id,
                properties={"pilot_case_id": case_id, "source_kind": "mailbox_memory_case"},
            )
        )

    message_node_keys = {str(item.get("message_id") or ""): _message_node_key(str(item.get("message_id") or "")) for item in messages}
    location_info = _pick_case_location(case_row=case_row, snapshot=snapshot, case_facts=case_facts, drive_facts=drive_facts)
    location_node_key = ""
    if location_info:
        location_node_key = _location_node_key(case_id, location_info)
        nodes.append(
            _node_row(
                "Location",
                {
                    "node_key": location_node_key,
                    "pilot_case_id": case_id,
                    "case_id": case_id,
                    "address": str(location_info.get("address") or ""),
                    "city": str(location_info.get("city") or ""),
                    "title": str(location_info.get("title") or ""),
                    "source_kind": "case_location",
                },
            )
        )
        relationships.append(
            _relationship_row(
                "HAS_LOCATION",
                src_node_key=case_node_key,
                dst_node_key=location_node_key,
                case_id=case_id,
                properties={"pilot_case_id": case_id, "source_kind": "mailbox_memory_case"},
            )
        )

    contact_rows = _collect_contacts(case_id=case_id, case_row=case_row, snapshot=snapshot, messages=messages)
    for contact in contact_rows:
        nodes.append(_node_row("Contact", contact))
        relationships.append(
            _relationship_row(
                "HAS_CONTACT",
                src_node_key=case_node_key,
                dst_node_key=str(contact.get("node_key") or ""),
                case_id=case_id,
                properties={"pilot_case_id": case_id, "source_kind": "mailbox_canonical"},
            )
        )

    mailbox_facts_by_document = _facts_by_document(case_facts)
    for document in mailbox_documents:
        document_id = str(document.get("document_id") or "").strip()
        if not document_id:
            continue
        document_node_key = _document_node_key("mailbox", document_id)
        nodes.append(
            _node_row(
                "Document",
                {
                    "node_key": document_node_key,
                    "pilot_case_id": case_id,
                    "case_id": case_id,
                    "document_id": document_id,
                    "file_name": str(document.get("file_name") or ""),
                    "title": str(document.get("file_name") or document_id),
                    "document_kind": str(document.get("document_kind") or ""),
                    "source_kind": "mailbox_document",
                    "source_type": str(document.get("source_type") or ""),
                    "message_id": str(document.get("message_id") or ""),
                    "content_sha256": str(document.get("content_sha256") or ""),
                    "summary_text": str(document.get("summary_text") or ""),
                },
            )
        )
        relationships.append(
            _relationship_row(
                "HAS_DOCUMENT",
                src_node_key=case_node_key,
                dst_node_key=document_node_key,
                case_id=case_id,
                properties={"pilot_case_id": case_id, "source_kind": "mailbox_document"},
            )
        )
        message_id = str(document.get("message_id") or "").strip()
        if message_id and message_node_keys.get(message_id):
            relationships.append(
                _relationship_row(
                    "MESSAGE_HAS_DOCUMENT",
                    src_node_key=message_node_keys[message_id],
                    dst_node_key=document_node_key,
                    case_id=case_id,
                    properties={"pilot_case_id": case_id, "source_kind": "mailbox_document"},
                )
            )
        if location_node_key and _document_has_location(mailbox_facts_by_document.get(document_id) or []):
            relationships.append(
                _relationship_row(
                    "MENTIONS_LOCATION",
                    src_node_key=document_node_key,
                    dst_node_key=location_node_key,
                    case_id=case_id,
                    properties={"pilot_case_id": case_id, "source_kind": "mailbox_document"},
                )
            )

    drive_facts_by_document = _facts_by_document(drive_facts, document_key="drive_document_id")
    for document in drive_documents:
        document_id = str(document.get("document_id") or "").strip()
        if not document_id:
            continue
        document_node_key = _document_node_key("drive", document_id)
        nodes.append(
            _node_row(
                "Document",
                {
                    "node_key": document_node_key,
                    "pilot_case_id": case_id,
                    "case_id": case_id,
                    "document_id": document_id,
                    "drive_item_id": str(document.get("drive_item_id") or ""),
                    "file_name": str(document.get("file_name") or ""),
                    "title": str(document.get("file_name") or document_id),
                    "document_kind": str(document.get("document_kind") or ""),
                    "source_kind": "drive_document",
                    "source_type": "drive_document",
                    "content_sha256": str(document.get("content_sha256") or ""),
                    "summary_text": str(document.get("summary_text") or ""),
                    "source_ref": str(document.get("source_ref") or ""),
                },
            )
        )
        relationships.append(
            _relationship_row(
                "HAS_DOCUMENT",
                src_node_key=case_node_key,
                dst_node_key=document_node_key,
                case_id=case_id,
                properties={"pilot_case_id": case_id, "source_kind": "drive_document"},
            )
        )
        if location_node_key and _document_has_location(drive_facts_by_document.get(document_id) or []):
            relationships.append(
                _relationship_row(
                    "MENTIONS_LOCATION",
                    src_node_key=document_node_key,
                    dst_node_key=location_node_key,
                    case_id=case_id,
                    properties={"pilot_case_id": case_id, "source_kind": "drive_document"},
                )
            )

    payload.nodes = _dedupe_by_node_key(nodes)
    payload.relationships = _dedupe_relationships(relationships)
    return payload


def build_case_context_neo4j_pilot_block(
    *,
    settings: Any,
    store: MailboxMemoryStore,
    case_id: str,
    context_pack: dict[str, Any],
    project: bool,
    graph_aware: bool,
    max_hops: int,
    limit: int,
    anchor_mode: str = "auto",
    backend: Neo4jPilotBackend | None = None,
) -> dict[str, Any]:
    bounded_limit = max(1, min(PATH_LIMIT, int(limit or 10)))
    requested_anchor_mode = _normalize_anchor_mode(anchor_mode)
    block = {
        "status": "disabled",
        "anchoring": {
            "requested_mode": requested_anchor_mode,
            "resolved_mode": "case",
            "available_modes": [],
            "selected_anchors": [],
        },
        "projection": {
            "attempted": bool(project),
            "status": "skipped",
            "node_count": 0,
            "relationship_count": 0,
            "warnings": [],
        },
        "retrieval": {
            "attempted": bool(graph_aware),
            "status": "skipped",
            "anchor_documents": [],
            "anchor_nodes": [],
            "neighborhood_nodes": [],
            "paths": [],
            "warnings": [],
        },
        "why_this_matters": [],
        "evidence_cards": [],
        "gaps": [],
        "inconsistencies": [],
        "graph_warnings": [],
        "snapshot": {},
        "warnings": [],
    }
    config = build_neo4j_pilot_config(settings)
    if not (project or graph_aware):
        return block
    if not config.enabled:
        block["status"] = "disabled"
        message = "Neo4j pilot is disabled by NEO4J_PILOT_ENABLED=0."
        block["warnings"].append(message)
        if project:
            block["projection"]["status"] = "disabled"
        if graph_aware:
            block["retrieval"]["status"] = "disabled"
        return block
    if not config.configured:
        block["status"] = "failed"
        message = "Neo4j pilot is enabled but configuration is incomplete."
        block["warnings"].append(message)
        if project:
            block["projection"]["status"] = "failed"
            block["projection"]["error"] = message
        if graph_aware:
            block["retrieval"]["status"] = "failed"
            block["retrieval"]["error"] = message
        return block

    projection_payload = build_case_projection_payload(store=store, case_id=case_id)
    projection_index = _build_projection_index(projection_payload)
    anchoring = _resolve_anchor_selection(
        context_pack=context_pack,
        projection_index=projection_index,
        requested_mode=requested_anchor_mode,
    )
    block["anchoring"] = anchoring["public"]

    owns_backend = backend is None
    client = backend
    try:
        if client is None:
            client = Neo4jPilotClient(config)
        if project:
            projection_result = client.replace_case_projection(projection_payload)
            projection_result["node_summary"] = dict(projection_index["node_summary"])
            projection_result["relationship_summary"] = dict(projection_index["relationship_summary"])
            block["projection"].update(projection_result)
        if graph_aware:
            retrieval_result = client.fetch_case_neighborhood(
                case_id=case_id,
                anchor_node_keys=list(anchoring["anchor_node_keys"]),
                max_hops=max_hops,
                limit=bounded_limit,
            )
            enriched = _enrich_graph_result(
                case_id=case_id,
                projection_index=projection_index,
                retrieval_result=retrieval_result,
                anchoring_public=anchoring["public"],
                limit=bounded_limit,
            )
            block["retrieval"].update(enriched["retrieval"])
            block["why_this_matters"] = enriched["why_this_matters"]
            block["evidence_cards"] = enriched["evidence_cards"]
            block["gaps"] = enriched["gaps"]
            block["inconsistencies"] = enriched["inconsistencies"]
            block["graph_warnings"] = enriched["graph_warnings"]
            block["snapshot"] = enriched["snapshot"]
        block["status"] = "ok"
        if project:
            block["projection"]["status"] = "ok"
        if graph_aware:
            block["retrieval"]["status"] = "ok"
        return block
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        block["status"] = "failed"
        block["warnings"].append(message)
        if project:
            block["projection"]["status"] = "failed"
            block["projection"]["error"] = message
        if graph_aware:
            block["retrieval"]["status"] = "failed"
            block["retrieval"]["error"] = message
        return block
    finally:
        if owns_backend and client is not None:
            client.close()


def anchor_document_node_keys(context_pack: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for chunk in list(context_pack.get("relevant_chunks") or []):
        document_id = str(chunk.get("document_id") or "").strip()
        if not document_id:
            continue
        source_type = str(chunk.get("source_type") or "").strip()
        if source_type == "mailbox_document_chunk":
            node_key = _document_node_key("mailbox", document_id)
        elif source_type == "drive_document_chunk":
            node_key = _document_node_key("drive", document_id)
        else:
            continue
        if node_key in seen:
            continue
        seen.add(node_key)
        keys.append(node_key)
    return keys[:5]


def _build_projection_index(payload: Neo4jProjectionPayload) -> dict[str, Any]:
    nodes_by_key: dict[str, dict[str, Any]] = {}
    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
    node_summary: Counter[str] = Counter()
    relationship_summary: Counter[str] = Counter()
    relationships: list[dict[str, Any]] = []
    for row in payload.nodes:
        label = str(row.get("label") or "")
        properties = dict(row.get("properties") or {})
        node_key = str(properties.get("node_key") or "")
        node_summary[label] += 1
        normalized = {
            "node_key": node_key,
            "label": label,
            "labels": [label],
            "value": _node_value(properties),
            **properties,
        }
        if node_key:
            nodes_by_key[node_key] = normalized
    for row in payload.relationships:
        rel_type = str(row.get("type") or "")
        src_node_key = str(row.get("src_node_key") or "")
        dst_node_key = str(row.get("dst_node_key") or "")
        relationship_summary[rel_type] += 1
        normalized = {
            "type": rel_type,
            "src_node_key": src_node_key,
            "dst_node_key": dst_node_key,
            "properties": dict(row.get("properties") or {}),
        }
        relationships.append(normalized)
        outgoing[src_node_key].append(normalized)
        incoming[dst_node_key].append(normalized)
    return {
        "case_id": payload.case_id,
        "nodes_by_key": nodes_by_key,
        "relationships": relationships,
        "outgoing": outgoing,
        "incoming": incoming,
        "node_summary": node_summary,
        "relationship_summary": relationship_summary,
    }


def _resolve_anchor_selection(
    *,
    context_pack: dict[str, Any],
    projection_index: dict[str, Any],
    requested_mode: str,
) -> dict[str, Any]:
    nodes_by_key = dict(projection_index.get("nodes_by_key") or {})
    relevant_document_keys = anchor_document_node_keys(context_pack)
    document_relevant = [nodes_by_key[key] for key in relevant_document_keys if key in nodes_by_key]
    document_fallback = sorted(
        [node for node in nodes_by_key.values() if str(node.get("label") or "") == "Document"],
        key=lambda item: (str(item.get("source_kind") or ""), str(item.get("value") or ""), str(item.get("node_key") or "")),
    )
    location_nodes = sorted(
        [node for node in nodes_by_key.values() if str(node.get("label") or "") == "Location"],
        key=lambda item: (str(item.get("value") or ""), str(item.get("node_key") or "")),
    )
    contact_nodes = sorted(
        [node for node in nodes_by_key.values() if str(node.get("label") or "") == "Contact"],
        key=lambda item: (str(item.get("source_kind") or ""), str(item.get("value") or ""), str(item.get("node_key") or "")),
    )
    available_modes = [
        mode
        for mode, rows in (
            ("document", document_fallback),
            ("contact", contact_nodes),
            ("location", location_nodes),
        )
        if rows
    ]

    selected_mode = "case"
    selected_nodes: list[dict[str, Any]] = []
    if requested_mode == "auto":
        if document_relevant:
            selected_mode = "document"
            selected_nodes = document_relevant[:3]
        elif location_nodes:
            selected_mode = "location"
            selected_nodes = location_nodes[:2]
        elif contact_nodes:
            selected_mode = "contact"
            selected_nodes = contact_nodes[:2]
    elif requested_mode == "document" and document_fallback:
        selected_mode = "document"
        selected_nodes = (document_relevant or document_fallback)[:3]
    elif requested_mode == "contact" and contact_nodes:
        selected_mode = "contact"
        selected_nodes = contact_nodes[:2]
    elif requested_mode == "location" and location_nodes:
        selected_mode = "location"
        selected_nodes = location_nodes[:2]

    selected_anchors = [
        _build_anchor_record(
            node,
            selection_reason=_selection_reason_for_anchor(
                requested_mode=requested_mode,
                resolved_mode=selected_mode,
                relevant_document_keys=relevant_document_keys,
            ),
        )
        for node in selected_nodes
    ]
    public = {
        "requested_mode": requested_mode,
        "resolved_mode": selected_mode,
        "available_modes": available_modes or ["case"],
        "selected_anchors": selected_anchors,
    }
    return {
        "public": public,
        "anchor_node_keys": [str(item.get("node_key") or "") for item in selected_anchors if str(item.get("node_key") or "")],
    }


def _selection_reason_for_anchor(*, requested_mode: str, resolved_mode: str, relevant_document_keys: list[str]) -> str:
    if requested_mode == "auto" and resolved_mode == "document" and relevant_document_keys:
        return "selected_from_relevant_chunks"
    if requested_mode == "auto":
        return "selected_by_auto_fallback"
    return "selected_by_requested_mode"


def _build_anchor_record(node: dict[str, Any], *, selection_reason: str) -> dict[str, Any]:
    anchor_type = _node_anchor_type(node)
    return {
        "anchor_type": anchor_type,
        "anchor_id": _anchor_id(node),
        "node_key": str(node.get("node_key") or ""),
        "value": str(node.get("value") or ""),
        "source_kind": str(node.get("source_kind") or ""),
        "selection_reason": selection_reason,
    }


def _enrich_graph_result(
    *,
    case_id: str,
    projection_index: dict[str, Any],
    retrieval_result: dict[str, Any],
    anchoring_public: dict[str, Any],
    limit: int,
) -> dict[str, Any]:
    cross_source_state = _cross_source_state(projection_index)
    enriched_paths = [
        _enrich_path(
            raw_path,
            resolved_mode=str(anchoring_public.get("resolved_mode") or "case"),
            cross_source_state=cross_source_state,
        )
        for raw_path in list(retrieval_result.get("paths") or [])
    ]
    enriched_paths.sort(
        key=lambda item: (
            -float(item.get("priority_score") or 0.0),
            0 if str(item.get("origin") or "") == "anchor" else 1,
            str(item.get("path_summary") or ""),
        )
    )
    top_paths = enriched_paths[:limit]
    why_this_matters = _build_why_this_matters(top_paths)
    evidence_cards = _build_evidence_cards(top_paths)
    findings = _detect_graph_findings(
        projection_index=projection_index,
        cross_source_state=cross_source_state,
        neighborhood_nodes=list(retrieval_result.get("neighborhood_nodes") or []),
        paths=top_paths,
    )
    retrieval = dict(retrieval_result)
    retrieval["paths"] = top_paths
    retrieval["anchor_mode"] = str(anchoring_public.get("resolved_mode") or "case")
    snapshot = _build_snapshot(
        case_id=case_id,
        anchoring_public=anchoring_public,
        projection_index=projection_index,
        top_paths=top_paths,
        evidence_cards=evidence_cards,
        gaps=findings["gaps"],
        inconsistencies=findings["inconsistencies"],
        graph_warnings=findings["graph_warnings"],
    )
    return {
        "retrieval": retrieval,
        "why_this_matters": why_this_matters,
        "evidence_cards": evidence_cards,
        "gaps": findings["gaps"],
        "inconsistencies": findings["inconsistencies"],
        "graph_warnings": findings["graph_warnings"],
        "snapshot": snapshot,
    }


def _enrich_path(
    raw_path: dict[str, Any],
    *,
    resolved_mode: str,
    cross_source_state: dict[str, Any],
) -> dict[str, Any]:
    nodes = [_normalize_path_node(item) for item in list(raw_path.get("nodes") or [])]
    rel_chain = [str(item) for item in list(raw_path.get("rel_chain") or [])]
    anchor_node = nodes[0] if nodes else {}
    anchor_type = _node_anchor_type(anchor_node)
    anchor_id = _anchor_id(anchor_node)
    operational_tags = _path_operational_tags(rel_chain=rel_chain, nodes=nodes, cross_source_state=cross_source_state)
    priority_score = _path_priority_score(
        origin=str(raw_path.get("origin") or ""),
        rel_chain=rel_chain,
        operational_tags=operational_tags,
        anchor_type=anchor_type,
        resolved_mode=resolved_mode,
    )
    path_summary = _path_summary(rel_chain=rel_chain, nodes=nodes)
    importance_reason = _importance_reason(
        rel_chain=rel_chain,
        nodes=nodes,
        cross_source_state=cross_source_state,
    )
    return {
        "origin": str(raw_path.get("origin") or ""),
        "anchor_type": anchor_type,
        "anchor_id": anchor_id,
        "rel_chain": rel_chain,
        "nodes": nodes,
        "path_summary": path_summary,
        "priority_score": priority_score,
        "importance_reason": importance_reason,
        "operational_tags": operational_tags,
    }


def _build_why_this_matters(paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in paths:
        signature = (
            str(path.get("anchor_type") or ""),
            str(path.get("anchor_id") or ""),
            str(path.get("path_summary") or ""),
        )
        if signature in seen:
            continue
        seen.add(signature)
        items.append(
            {
                "anchor_type": str(path.get("anchor_type") or ""),
                "anchor_id": str(path.get("anchor_id") or ""),
                "path_summary": str(path.get("path_summary") or ""),
                "importance_reason": str(path.get("importance_reason") or ""),
                "priority_score": float(path.get("priority_score") or 0.0),
            }
        )
        if len(items) >= 5:
            break
    return items


def _build_evidence_cards(paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in paths:
        title = _evidence_title(path)
        signature = (
            title,
            str(path.get("anchor_id") or ""),
            str(path.get("path_summary") or ""),
        )
        if signature in seen:
            continue
        seen.add(signature)
        cards.append(
            {
                "title": title,
                "importance_reason": str(path.get("importance_reason") or ""),
                "anchor_type": str(path.get("anchor_type") or ""),
                "anchor_id": str(path.get("anchor_id") or ""),
                "path_summary": str(path.get("path_summary") or ""),
                "supporting_nodes": [_supporting_node_summary(node) for node in list(path.get("nodes") or [])],
                "supporting_relationships": _unique_strings(list(path.get("rel_chain") or [])),
                "confidence_mode": _confidence_mode_for_path(path),
            }
        )
        if len(cards) >= 5:
            break
    return cards


def _detect_graph_findings(
    *,
    projection_index: dict[str, Any],
    cross_source_state: dict[str, Any],
    neighborhood_nodes: list[dict[str, Any]],
    paths: list[dict[str, Any]],
) -> dict[str, Any]:
    nodes_by_key = dict(projection_index.get("nodes_by_key") or {})
    relationships = list(projection_index.get("relationships") or [])
    case_nodes = [node for node in nodes_by_key.values() if str(node.get("label") or "") == "Case"]
    document_nodes = [node for node in nodes_by_key.values() if str(node.get("label") or "") == "Document"]
    contact_nodes = [node for node in nodes_by_key.values() if str(node.get("label") or "") == "Contact"]
    location_nodes = [node for node in nodes_by_key.values() if str(node.get("label") or "") == "Location"]

    gaps: list[dict[str, Any]] = []
    inconsistencies: list[dict[str, Any]] = []
    graph_warnings: list[dict[str, Any]] = []

    if document_nodes and not contact_nodes:
        gaps.append(
            _finding(
                code="documents_without_contact",
                severity="warning",
                message="The case has projected documents but no projected contact.",
                supporting_node_keys=[node.get("node_key") for node in document_nodes[:3]],
                supporting_relationships=["HAS_DOCUMENT"],
            )
        )
    if document_nodes and not location_nodes:
        gaps.append(
            _finding(
                code="documents_without_location",
                severity="warning",
                message="The case has projected documents but no projected location.",
                supporting_node_keys=[node.get("node_key") for node in document_nodes[:3]],
                supporting_relationships=["HAS_DOCUMENT"],
            )
        )

    has_case_location = any(str(rel.get("type") or "") == "HAS_LOCATION" for rel in relationships)
    if not has_case_location:
        for rel in relationships:
            if str(rel.get("type") or "") != "MENTIONS_LOCATION":
                continue
            inconsistencies.append(
                _finding(
                    code="document_mentions_unlinked_location",
                    severity="warning",
                    message="A document mentions a location, but the case has no linked location node.",
                    supporting_node_keys=[str(rel.get("src_node_key") or ""), str(rel.get("dst_node_key") or "")],
                    supporting_relationships=["MENTIONS_LOCATION"],
                )
            )

    if cross_source_state["has_mailbox_documents"] and cross_source_state["has_drive_documents"] and not cross_source_state["shared_location"]:
        graph_warnings.append(
            _finding(
                code="cross_source_coherence_not_proven",
                severity="info",
                message="Mailbox and Drive documents are present, but bounded graph evidence does not prove cross-source coherence.",
                supporting_node_keys=list(cross_source_state["mailbox_document_keys"][:2] + cross_source_state["drive_document_keys"][:2]),
                supporting_relationships=["HAS_DOCUMENT", "MENTIONS_LOCATION"],
            )
        )

    if len(neighborhood_nodes) < 4 or len(paths) < 3:
        supporting_keys = [str(node.get("node_key") or "") for node in list(case_nodes)[:1] + document_nodes[:2]]
        graph_warnings.append(
            _finding(
                code="neighborhood_too_sparse_for_cross_source_claim",
                severity="info",
                message="The bounded case neighborhood is too small to claim strong cross-source coherence.",
                supporting_node_keys=supporting_keys,
                supporting_relationships=["HAS_DOCUMENT"],
            )
        )

    return {
        "gaps": gaps,
        "inconsistencies": inconsistencies,
        "graph_warnings": graph_warnings,
    }


def _build_snapshot(
    *,
    case_id: str,
    anchoring_public: dict[str, Any],
    projection_index: dict[str, Any],
    top_paths: list[dict[str, Any]],
    evidence_cards: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    inconsistencies: list[dict[str, Any]],
    graph_warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "snapshot_version": "neo4j_pilot_snapshot.v2",
        "case_id": case_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "anchor_mode_requested": str(anchoring_public.get("requested_mode") or "auto"),
        "anchor_mode_used": str(anchoring_public.get("resolved_mode") or "case"),
        "node_summary": dict(projection_index.get("node_summary") or {}),
        "relationship_summary": dict(projection_index.get("relationship_summary") or {}),
        "top_paths": [
            {
                "path_summary": str(path.get("path_summary") or ""),
                "priority_score": float(path.get("priority_score") or 0.0),
                "importance_reason": str(path.get("importance_reason") or ""),
            }
            for path in top_paths[:3]
        ],
        "evidence_cards": evidence_cards[:3],
        "gap_summary": {
            "gap_count": len(gaps),
            "inconsistency_count": len(inconsistencies),
            "graph_warning_count": len(graph_warnings),
            "codes": _unique_strings(
                [str(item.get("code") or "") for item in gaps + inconsistencies + graph_warnings if str(item.get("code") or "")]
            ),
        },
    }


def _cross_source_state(projection_index: dict[str, Any]) -> dict[str, Any]:
    nodes_by_key = dict(projection_index.get("nodes_by_key") or {})
    relationships = list(projection_index.get("relationships") or [])
    mailbox_document_keys = sorted(
        [node_key for node_key, node in nodes_by_key.items() if str(node.get("source_kind") or "") == "mailbox_document"]
    )
    drive_document_keys = sorted(
        [node_key for node_key, node in nodes_by_key.items() if str(node.get("source_kind") or "") == "drive_document"]
    )
    docs_by_location: dict[str, set[str]] = defaultdict(set)
    for rel in relationships:
        if str(rel.get("type") or "") != "MENTIONS_LOCATION":
            continue
        src_node = nodes_by_key.get(str(rel.get("src_node_key") or ""), {})
        dst_node_key = str(rel.get("dst_node_key") or "")
        source_kind = str(src_node.get("source_kind") or "")
        if source_kind in {"mailbox_document", "drive_document"} and dst_node_key:
            docs_by_location[dst_node_key].add(source_kind)
    shared_location = any({"mailbox_document", "drive_document"}.issubset(kinds) for kinds in docs_by_location.values())
    return {
        "has_mailbox_documents": bool(mailbox_document_keys),
        "has_drive_documents": bool(drive_document_keys),
        "mailbox_document_keys": mailbox_document_keys,
        "drive_document_keys": drive_document_keys,
        "shared_location": shared_location,
    }


def _normalize_anchor_mode(value: str) -> str:
    candidate = str(value or "auto").strip().lower()
    return candidate if candidate in ALL_ANCHOR_MODES else "auto"


def _normalize_path_node(node: dict[str, Any]) -> dict[str, Any]:
    labels = list(node.get("labels") or [])
    label = str(labels[0] if labels else node.get("label") or "")
    normalized = dict(node)
    normalized["labels"] = labels or ([label] if label else [])
    normalized["label"] = label
    normalized["value"] = str(node.get("value") or _node_value(node))
    return normalized


def _path_summary(*, rel_chain: list[str], nodes: list[dict[str, Any]]) -> str:
    if not nodes:
        return ""
    parts = [str(nodes[0].get("label") or "Node")]
    for index, rel_type in enumerate(rel_chain):
        parts.append(str(rel_type or "RELATED_TO"))
        if index + 1 < len(nodes):
            parts.append(str(nodes[index + 1].get("label") or "Node"))
    return " -> ".join(parts)


def _path_operational_tags(
    *,
    rel_chain: list[str],
    nodes: list[dict[str, Any]],
    cross_source_state: dict[str, Any],
) -> list[str]:
    tags: list[str] = []
    if "MESSAGE_HAS_DOCUMENT" in rel_chain:
        tags.append("message_attachment")
    if "HAS_DOCUMENT" in rel_chain:
        tags.append("case_document")
    if "MENTIONS_LOCATION" in rel_chain:
        tags.append("location_confirmation")
    if "HAS_CONTACT" in rel_chain:
        tags.append("contact_identity")
    if "HAS_LOCATION" in rel_chain:
        tags.append("case_location")
    source_kinds = {str(node.get("source_kind") or "") for node in nodes}
    if cross_source_state.get("shared_location") and source_kinds.intersection({"mailbox_document", "drive_document"}):
        tags.append("cross_source_correlated")
    return tags


def _path_priority_score(
    *,
    origin: str,
    rel_chain: list[str],
    operational_tags: list[str],
    anchor_type: str,
    resolved_mode: str,
) -> float:
    score = 0.0
    if "MESSAGE_HAS_DOCUMENT" in rel_chain:
        score += 100.0
    if "HAS_DOCUMENT" in rel_chain:
        score += 40.0
    if "MENTIONS_LOCATION" in rel_chain:
        score += 35.0
    if "HAS_CONTACT" in rel_chain:
        score += 30.0
    if "HAS_LOCATION" in rel_chain:
        score += 25.0
    if "cross_source_correlated" in operational_tags:
        score += 20.0
    if origin == "anchor":
        score += 5.0
    if anchor_type == resolved_mode and resolved_mode != "case":
        score += 10.0
    score += max(0.0, 5.0 - float(len(rel_chain)))
    return round(score, 3)


def _importance_reason(
    *,
    rel_chain: list[str],
    nodes: list[dict[str, Any]],
    cross_source_state: dict[str, Any],
) -> str:
    document_node = next((node for node in reversed(nodes) if str(node.get("label") or "") == "Document"), {})
    source_kind = str(document_node.get("source_kind") or "")
    if "MESSAGE_HAS_DOCUMENT" in rel_chain and source_kind == "mailbox_document":
        message = "Mailbox document matters because it is linked to the case message through MESSAGE_HAS_DOCUMENT."
    elif "HAS_DOCUMENT" in rel_chain and "MENTIONS_LOCATION" in rel_chain and source_kind == "drive_document":
        message = "Drive document matters because it is linked to the case through HAS_DOCUMENT and confirms the case location through MENTIONS_LOCATION."
    elif "HAS_DOCUMENT" in rel_chain and "MENTIONS_LOCATION" in rel_chain:
        message = "Document matters because it is linked to the case through HAS_DOCUMENT and confirms the case location through MENTIONS_LOCATION."
    elif "HAS_DOCUMENT" in rel_chain:
        message = "Document matters because it is directly linked to the case through HAS_DOCUMENT."
    elif "HAS_CONTACT" in rel_chain:
        message = "Contact matters because the case is anchored to it through HAS_CONTACT."
    elif "HAS_LOCATION" in rel_chain:
        message = "Location matters because the case is anchored to it through HAS_LOCATION."
    else:
        message = "This path matters because it stays inside the bounded case neighborhood."
    if cross_source_state.get("shared_location") and "MENTIONS_LOCATION" in rel_chain and source_kind in {"mailbox_document", "drive_document"}:
        message += " This also strengthens cross-source coherence because mailbox and Drive documents converge on the same location."
    return message


def _evidence_title(path: dict[str, Any]) -> str:
    rel_chain = list(path.get("rel_chain") or [])
    nodes = list(path.get("nodes") or [])
    document_node = next((node for node in reversed(nodes) if str(node.get("label") or "") == "Document"), {})
    source_kind = str(document_node.get("source_kind") or "")
    if "MESSAGE_HAS_DOCUMENT" in rel_chain and source_kind == "mailbox_document":
        return "Mailbox attachment tied to the case message"
    if "HAS_DOCUMENT" in rel_chain and "MENTIONS_LOCATION" in rel_chain and source_kind == "drive_document":
        return "Drive document confirms the case location"
    if "HAS_DOCUMENT" in rel_chain and "MENTIONS_LOCATION" in rel_chain:
        return "Document confirms the case location"
    if "HAS_CONTACT" in rel_chain:
        return "Case contact anchor"
    if "HAS_LOCATION" in rel_chain:
        return "Case location anchor"
    if "HAS_DOCUMENT" in rel_chain:
        return "Case document anchor"
    return "Bounded case graph evidence"


def _confidence_mode_for_path(path: dict[str, Any]) -> str:
    tags = set(path.get("operational_tags") or [])
    rel_chain = list(path.get("rel_chain") or [])
    if "cross_source_correlated" in tags:
        return "hard_relation_chain_cross_source"
    if rel_chain:
        return "hard_relation_chain"
    return "anchor_supported"


def _finding(
    *,
    code: str,
    severity: str,
    message: str,
    supporting_node_keys: list[str | None],
    supporting_relationships: list[str],
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "supporting_node_keys": [str(item) for item in supporting_node_keys if str(item or "")],
        "supporting_relationships": _unique_strings(supporting_relationships),
    }


def _supporting_node_summary(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_key": str(node.get("node_key") or ""),
        "label": str(node.get("label") or ""),
        "value": str(node.get("value") or ""),
        "source_kind": str(node.get("source_kind") or ""),
    }


def _node_anchor_type(node: dict[str, Any]) -> str:
    label = str(node.get("label") or "")
    if label == "Document":
        return "document"
    if label == "Contact":
        return "contact"
    if label == "Location":
        return "location"
    if label == "Case":
        return "case"
    if label == "Message":
        return "message"
    return "unknown"


def _anchor_id(node: dict[str, Any]) -> str:
    anchor_type = _node_anchor_type(node)
    if anchor_type == "document":
        return str(node.get("document_id") or node.get("node_key") or "")
    if anchor_type == "contact":
        return str(node.get("email") or node.get("node_key") or "")
    if anchor_type == "location":
        return str(node.get("address") or node.get("city") or node.get("node_key") or "")
    if anchor_type == "message":
        return str(node.get("message_id") or node.get("node_key") or "")
    return str(node.get("case_id") or node.get("node_key") or "")


def _node_value(node: dict[str, Any]) -> str:
    return str(
        node.get("title")
        or node.get("file_name")
        or node.get("message_id")
        or node.get("email")
        or node.get("address")
        or node.get("city")
        or node.get("name")
        or node.get("case_id")
        or ""
    )


def _node_row(label: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {"label": label, "properties": properties}


def _relationship_row(
    rel_type: str,
    *,
    src_node_key: str,
    dst_node_key: str,
    case_id: str,
    properties: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": rel_type,
        "src_node_key": src_node_key,
        "dst_node_key": dst_node_key,
        "properties": {"pilot_case_id": case_id, **properties},
    }


def _facts_by_document(
    facts: list[dict[str, Any]],
    *,
    document_key: str = "document_id",
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        key = str(fact.get(document_key) or "").strip()
        if not key:
            continue
        grouped.setdefault(key, []).append(fact)
    return grouped


def _is_live_fact(fact: dict[str, Any]) -> bool:
    """Match mailbox_memory_runtime.split_conflicting_facts live-row predicate."""
    return is_live_fact(fact)


def _document_has_location(facts: list[dict[str, Any]]) -> bool:
    location_keys = {"installation_address", "investment_address", "city"}
    return any(
        _is_live_fact(item) and str(item.get("fact_key") or "") in location_keys for item in facts
    )


def _pick_case_location(
    *,
    case_row: dict[str, Any],
    snapshot: dict[str, Any],
    case_facts: list[dict[str, Any]],
    drive_facts: list[dict[str, Any]],
) -> dict[str, str]:
    address = _best_fact_value(case_facts + drive_facts, "installation_address") or _best_fact_value(case_facts + drive_facts, "investment_address")
    city = _best_fact_value(case_facts + drive_facts, "city")
    if not address and isinstance(snapshot.get("key_facts"), list):
        address = _snapshot_fact_value(snapshot, "installation_address") or _snapshot_fact_value(snapshot, "investment_address")
    if not city and isinstance(snapshot.get("key_facts"), list):
        city = _snapshot_fact_value(snapshot, "city")
    metadata = dict(case_row.get("metadata") or {})
    if not address:
        address = str(metadata.get("installation_address") or "").strip()
    if not city:
        city = str(metadata.get("city") or "").strip()
    title = address or city
    if not title:
        return {}
    return {"address": address, "city": city, "title": title}


def _best_fact_value(facts: list[dict[str, Any]], fact_key: str) -> str:
    ranked = sorted(
        (
            fact
            for fact in facts
            if _is_live_fact(fact)
            and str(fact.get("fact_key") or "") == fact_key
            and str(fact.get("normalized_value") or "").strip()
        ),
        key=lambda item: (-float(item.get("confidence") or 0.0), str(item.get("observed_at") or "")),
    )
    return str((ranked[0] or {}).get("normalized_value") or "").strip() if ranked else ""


def _snapshot_fact_value(snapshot: dict[str, Any], fact_key: str) -> str:
    for fact in snapshot.get("key_facts") or []:
        if str((fact or {}).get("fact_key") or "") == fact_key:
            return str((fact or {}).get("value") or "").strip()
    return ""


def _collect_contacts(
    *,
    case_id: str,
    case_row: dict[str, Any],
    snapshot: dict[str, Any],
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    contacts: dict[str, dict[str, Any]] = {}

    def upsert(*, email: str, name: str, source_kind: str) -> None:
        email_norm = str(email or "").strip().lower()
        name_clean = str(name or "").strip()
        if not email_norm and not name_clean:
            return
        key_part = email_norm or _normalize_key(name_clean)
        node_key = _contact_node_key(case_id, key_part)
        row = contacts.get(node_key)
        candidate = {
            "node_key": node_key,
            "pilot_case_id": case_id,
            "case_id": case_id,
            "email": email_norm,
            "name": name_clean,
            "title": name_clean or email_norm,
            "source_kind": source_kind,
        }
        if row is None:
            contacts[node_key] = candidate
            return
        if not row.get("email") and email_norm:
            row["email"] = email_norm
        if len(str(name_clean)) > len(str(row.get("name") or "")):
            row["name"] = name_clean
            row["title"] = name_clean or row.get("email") or ""

    customer = dict(snapshot.get("customer") or {})
    upsert(
        email=str(case_row.get("customer_email") or customer.get("email") or ""),
        name=str(case_row.get("customer_name") or customer.get("name") or ""),
        source_kind="case_customer",
    )
    for message in messages:
        sender = str(message.get("sender") or "")
        upsert(
            email=str(message.get("sender_email") or "") or _extract_email(sender),
            name=_sender_display_name(sender),
            source_kind="gmail_sender",
        )
    return list(contacts.values())


def _extract_email(value: str) -> str:
    match = re.search(r"([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})", str(value or ""), re.I)
    return str(match.group(1) if match else "").strip().lower()


def _sender_display_name(sender: str) -> str:
    raw = str(sender or "").strip()
    if not raw:
        return ""
    if "<" in raw:
        return raw.split("<", 1)[0].strip().strip('"')
    return ""


def _dedupe_by_node_key(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        properties = dict(row.get("properties") or {})
        key = str(properties.get("node_key") or "")
        if key and key not in seen:
            seen[key] = row
    return list(seen.values())


def _dedupe_relationships(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        signature = (
            str(row.get("type") or ""),
            str(row.get("src_node_key") or ""),
            str(row.get("dst_node_key") or ""),
        )
        if signature not in seen:
            seen[signature] = row
    return list(seen.values())


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _case_node_key(case_id: str) -> str:
    return f"Case:{case_id}"


def _message_node_key(message_id: str) -> str:
    return f"Message:{message_id}"


def _document_node_key(source_kind: str, document_id: str) -> str:
    return f"Document:{source_kind}:{document_id}"


def _contact_node_key(case_id: str, contact_key: str) -> str:
    return f"Contact:{case_id}:{contact_key}"


def _location_node_key(case_id: str, location: dict[str, str]) -> str:
    key_part = _normalize_key(str(location.get("address") or location.get("city") or ""))
    return f"Location:{case_id}:{key_part}"


__all__ = [
    "Neo4jPilotClient",
    "Neo4jPilotConfig",
    "Neo4jPilotError",
    "Neo4jProjectionPayload",
    "anchor_document_node_keys",
    "build_case_context_neo4j_pilot_block",
    "build_case_projection_payload",
    "build_neo4j_pilot_config",
    "build_neo4j_pilot_connectivity_check",
]
