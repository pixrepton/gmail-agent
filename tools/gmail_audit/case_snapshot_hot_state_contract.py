"""Formal V2.1 CaseSnapshotHotState contract (compact operational truth, cold pointers)."""

from __future__ import annotations

from typing import Any

# Bump when adding required fields or changing semantics.
CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION = "case_snapshot_hot_state.v1"


def validate_case_snapshot_hot_state(payload: dict[str, Any]) -> list[str]:
    """Return human-readable validation errors; empty list means structurally valid."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload must be an object"]
    sv = str(payload.get("schema_version") or "")
    if sv != CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION!r}, got {sv!r}")
    for key in ("snapshot_id", "case"):
        if key not in payload:
            errors.append(f"missing required key: {key}")
    case_block = payload.get("case")
    if case_block is not None and not isinstance(case_block, dict):
        errors.append("case must be an object")
    elif isinstance(case_block, dict):
        if not str(case_block.get("case_id") or "").strip():
            errors.append("case.case_id must be non-empty")
    kf = payload.get("key_facts")
    if kf is not None:
        if not isinstance(kf, list):
            errors.append("key_facts must be a list")
        else:
            for i, row in enumerate(kf):
                if not isinstance(row, dict):
                    errors.append(f"key_facts[{i}] must be an object")
                    continue
                prov = row.get("provenance")
                if prov is not None and not isinstance(prov, dict):
                    errors.append(f"key_facts[{i}].provenance must be an object")
                elif isinstance(prov, dict):
                    if not str(prov.get("kind") or "").strip():
                        errors.append(f"key_facts[{i}].provenance.kind required for evidence-backed facts")
    ac = payload.get("active_conflicts")
    if ac is not None and not isinstance(ac, list):
        errors.append("active_conflicts must be a list")
    cep = payload.get("cold_evidence_pointers")
    if cep is not None and not isinstance(cep, dict):
        errors.append("cold_evidence_pointers must be an object")
    sm = payload.get("snapshot_meta")
    if sm is not None and not isinstance(sm, dict):
        errors.append("snapshot_meta must be an object")
    return errors


def is_evidence_backed_fact(row: dict[str, Any]) -> bool:
    """A key fact is evidence-backed if it carries provenance or a non-empty source_ref."""
    if str(row.get("source_ref") or "").strip():
        return True
    prov = row.get("provenance")
    if isinstance(prov, dict) and str(prov.get("kind") or "").strip() and str(prov.get("ref") or prov.get("source_ref") or "").strip():
        return True
    return False


__all__ = [
    "CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION",
    "is_evidence_backed_fact",
    "validate_case_snapshot_hot_state",
]
