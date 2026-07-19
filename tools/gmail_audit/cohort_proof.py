"""Bounded Gmail + Drive cohort proof helpers.

The helpers here produce auditable projection records. They do not perform
outbound actions and do not replace the canonical Gmail/Drive runtimes.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


COHORT_SCHEMA_VERSION = "cohort_proof_run.v1"
DEFAULT_GMAIL_COHORT_QUERY = "-in:spam -in:trash"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff", ".svg"}


def is_drive_document_candidate(item: dict[str, Any]) -> bool:
    """Return true for Drive files that should enter the document cohort."""

    mime_type = str(item.get("mime_type") or item.get("mimeType") or "").strip().lower()
    file_name = str(item.get("file_name") or item.get("name") or item.get("title") or "").strip().lower()
    document_kind = str(item.get("document_kind") or "").strip().lower()
    if mime_type.startswith("image/"):
        return False
    if document_kind in {"media_asset", "media_bundle", "photo", "image"}:
        return False
    if Path(file_name).suffix.lower() in IMAGE_EXTENSIONS:
        return False
    return bool(mime_type or file_name)


def build_cohort_run_record(
    *,
    run_id: str,
    gmail_items: list[dict[str, Any]],
    drive_items: list[dict[str, Any]],
    context_packs: list[Any],
    item_statuses: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a sanitized summary for Daszek V3 and FastAPI read surfaces."""

    selected_drive = [item for item in drive_items if is_drive_document_candidate(item)]
    statuses = item_statuses or {}
    pack_dicts = [_as_dict(pack) for pack in context_packs]
    case_items = [_case_item_from_pack(pack, statuses.get(str(pack.get("case_id") or ""), {})) for pack in pack_dicts if str(pack.get("case_id") or "").strip()]
    shared = [item for item in case_items if item["has_gmail_context"] and item["has_drive_context"]]
    conflict_count = sum(int(item.get("conflict_count") or 0) for item in case_items)
    gap_count = sum(int(item.get("gap_count") or 0) for item in case_items)
    proposal_count = sum(int(item.get("proposal_count") or 0) for item in case_items)

    return {
        "schema_version": COHORT_SCHEMA_VERSION,
        "run_id": str(run_id or "").strip(),
        "generated_at": datetime.now().astimezone().isoformat(),
        "selection": {
            "gmail_query": DEFAULT_GMAIL_COHORT_QUERY,
            "gmail_limit": len(gmail_items),
            "drive_filter": "exclude image/photo/media files",
        },
        "counts": {
            "gmail_selected": len(gmail_items),
            "drive_documents_selected": len(selected_drive),
            "drive_documents_skipped": max(0, len(drive_items) - len(selected_drive)),
            "case_count": len(case_items),
            "shared_gmail_drive_case_count": len(shared),
            "conflict_count": conflict_count,
            "gap_count": gap_count,
            "proposal_count": proposal_count,
        },
        "items": case_items,
        "drive_documents": [_drive_item_summary(item) for item in selected_drive[:200]],
        "runtime_guards": {
            "outbound_actions": "disabled",
            "dashboard_projection": "allowed",
            "truth_source": "node_b_mailbox_memory",
        },
    }


def write_cohort_run_record(record: dict[str, Any], *, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    run_id = str(record.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("cohort run record requires run_id")
    path = root / f"{run_id}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_cohort_run_record(run_id: str, *, root: Path) -> dict[str, Any] | None:
    safe_run_id = str(run_id or "").strip()
    if not safe_run_id:
        return None
    path = root / f"{safe_run_id}.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _case_item_from_pack(pack: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    case_id = str(pack.get("case_id") or "").strip()
    source_refs = [item for item in pack.get("source_refs") or [] if isinstance(item, dict)]
    drive_documents = [item for item in pack.get("drive_documents_summary") or [] if isinstance(item, dict)]
    conflicts = [item for item in pack.get("conflicting_facts") or [] if isinstance(item, dict)]
    gaps = list(pack.get("completeness_gaps") or [])
    proposals = [item for item in pack.get("action_proposals") or [] if isinstance(item, dict)]
    snapshot = pack.get("snapshot") if isinstance(pack.get("snapshot"), dict) else {}
    return {
        "case_id": case_id,
        "status": str(status.get("status") or "projected"),
        "title": str(snapshot.get("title") or snapshot.get("subject") or ""),
        "summary": str(snapshot.get("summary_text") or snapshot.get("summary") or ""),
        "has_gmail_context": _has_gmail_context(source_refs),
        "has_drive_context": bool(drive_documents),
        "conflict_count": len(conflicts),
        "gap_count": len(gaps),
        "proposal_count": len(proposals),
        "latest_signal_id": str((pack.get("runtime_state") or {}).get("latest_signal_id") or ""),
    }


def _has_gmail_context(source_refs: list[dict[str, Any]]) -> bool:
    for item in source_refs:
        source_type = str(item.get("source_type") or item.get("type") or "").lower()
        if "gmail" in source_type or "message" in source_type:
            return True
        if str(item.get("message_id") or "").strip():
            return True
    return False


def _drive_item_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": str(item.get("document_id") or item.get("drive_item_id") or item.get("id") or ""),
        "file_name": str(item.get("file_name") or item.get("name") or item.get("title") or ""),
        "mime_type": str(item.get("mime_type") or item.get("mimeType") or ""),
        "document_kind": str(item.get("document_kind") or ""),
        "source_ref": str(item.get("source_ref") or item.get("webViewLink") or ""),
    }


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    return {}


__all__ = [
    "COHORT_SCHEMA_VERSION",
    "DEFAULT_GMAIL_COHORT_QUERY",
    "build_cohort_run_record",
    "is_drive_document_candidate",
    "read_cohort_run_record",
    "write_cohort_run_record",
]
