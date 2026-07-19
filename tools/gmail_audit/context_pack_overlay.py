"""Merge case_context_pack from context_bundle into AssembledContext."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from context_assembler import AssembledContext, _facts_dict_from_active_facts


def _facts_from_vnext_rows(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if not isinstance(rows, list):
        return facts
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("fact_key") or row.get("predicate") or row.get("key") or "").strip()
        if not key:
            continue
        value = row.get("value")
        if value is None:
            value = row.get("normalized_value")
        facts[key] = value
    return facts


def facts_and_chunks_from_pack(pack: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(pack, dict) or not pack:
        return {}, []
    active = pack.get("active_facts")
    facts: dict[str, Any] = {}
    if isinstance(active, list):
        facts = _facts_dict_from_active_facts(active)
    elif isinstance(active, dict):
        facts = dict(active)
    if not facts:
        facts = _facts_from_vnext_rows(pack.get("facts") if isinstance(pack.get("facts"), list) else None)
    if not facts and isinstance(pack.get("hot_state"), dict):
        snapshot = pack["hot_state"].get("snapshot")
        if isinstance(snapshot, dict):
            facts = _facts_from_vnext_rows(snapshot.get("key_facts"))
    chunks_raw = pack.get("relevant_chunks")
    chunks = [c for c in chunks_raw if isinstance(c, dict)] if isinstance(chunks_raw, list) else []
    return facts, chunks


def overlay_pack_onto_assembled(
    assembled: AssembledContext,
    context_bundle: dict[str, Any] | None,
) -> AssembledContext:
    if not isinstance(context_bundle, dict):
        return assembled
    pack = context_bundle.get("case_context_pack")
    if not isinstance(pack, dict) or not pack:
        return assembled
    pack_facts, pack_chunks = facts_and_chunks_from_pack(pack)
    if not pack_facts and not pack_chunks:
        return assembled

    merged_facts = dict(assembled.case_facts)
    merged_facts.update(pack_facts)

    seen: set[str] = set()
    merged_chunks: list[dict[str, Any]] = []
    for chunk in [*assembled.relevant_chunks, *pack_chunks]:
        if not isinstance(chunk, dict):
            continue
        key = str(chunk.get("chunk_id") or chunk.get("id") or id(chunk))
        if key in seen:
            continue
        seen.add(key)
        merged_chunks.append(chunk)

    case_id_used = assembled.case_id_used or str(pack.get("case_id") or "").strip()
    engagement_id = assembled.engagement_id or str(pack.get("engagement_id") or "").strip()

    return replace(
        assembled,
        case_facts=merged_facts,
        relevant_chunks=merged_chunks,
        facts_count=len(merged_facts),
        chunks_count=len(merged_chunks),
        case_id_used=case_id_used,
        engagement_id=engagement_id,
    )
