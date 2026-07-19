"""Canonical write path for mailbox_memory_cases rows."""

from __future__ import annotations

from typing import Any

from case_routing import CaseRouting, enrich_case_row_before_upsert, operator_priority_to_label


def write_case_row(
    row: dict[str, Any],
    *,
    mailbox_store: Any,
    source_kind: str,
    classification: dict[str, Any] | None = None,
    orchestrator_status: str | None = None,
) -> tuple[dict[str, Any], CaseRouting]:
    """Enrich routing metadata and upsert via mailbox_store.upsert_case."""
    enriched, routing = enrich_case_row_before_upsert(
        row,
        source_kind=source_kind,
        classification=classification,
        orchestrator_status=orchestrator_status,
    )
    if not routing.upsert_allowed:
        raise ValueError(f"upsert not allowed for case {row.get('case_id')}")
    upsert = getattr(mailbox_store, "upsert_case", None)
    if not callable(upsert):
        raise RuntimeError("mailbox_store missing upsert_case")
    upsert(enriched)
    return enriched, routing


def patch_case_row(
    case_id: str,
    metadata_patch: dict[str, Any],
    *,
    mailbox_store: Any,
    updated_at: str | None = None,
) -> tuple[dict[str, Any], CaseRouting]:
    """Merge metadata on an existing case and upsert via the canonical gateway."""
    mutate = getattr(type(mailbox_store), "mutate_case", None)
    if callable(mutate):
        routing_box: dict[str, CaseRouting] = {}

        def _mutate(row: dict[str, Any]) -> dict[str, Any]:
            current = dict(row)
            meta = dict(current.get("metadata") or {})
            meta.update(metadata_patch)
            current["metadata"] = meta
            if updated_at:
                current["updated_at"] = updated_at
            source_kind = str(meta.get("source_kind") or "manual").strip() or "manual"
            enriched, routing = enrich_case_row_before_upsert(current, source_kind=source_kind)
            if not routing.upsert_allowed:
                raise ValueError(f"upsert not allowed for case {case_id}")
            routing_box["routing"] = routing
            return enriched

        enriched = mailbox_store.mutate_case(case_id, _mutate)
        routing = routing_box.get("routing")
        if routing is None:
            raise RuntimeError("mailbox_store mutate_case did not provide routing")
        return enriched, routing

    fetch = getattr(mailbox_store, "fetch_case", None)
    if not callable(fetch):
        raise RuntimeError("mailbox_store missing fetch_case")
    existing = fetch(case_id)
    if not isinstance(existing, dict) or not existing:
        raise LookupError(f"case not found: {case_id}")
    row = dict(existing)
    meta = dict(row.get("metadata") or {})
    meta.update(metadata_patch)
    row["metadata"] = meta
    if updated_at:
        row["updated_at"] = updated_at
    source_kind = str(meta.get("source_kind") or "manual").strip() or "manual"
    return write_case_row(row, mailbox_store=mailbox_store, source_kind=source_kind)


__all__ = ["operator_priority_to_label", "patch_case_row", "write_case_row"]
