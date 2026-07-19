"""Download and parse Google Drive files for agent tools (PR-C)."""

from __future__ import annotations

from typing import Any

from agent_runtime.settings import AgentRuntimeSettings


def download_and_parse_drive_file(
    file_id: str,
    *,
    file_name: str = "",
    settings: AgentRuntimeSettings | None = None,
    force_hard_lane: bool = False,
) -> dict[str, Any]:
    """
    Download Drive bytes and run Docling → Unstructured → legacy chain.
    Returns dict with extracted_text, parser_name, extraction_status, structured_facts.
    """
    fid = str(file_id or "").strip()
    if not fid:
        raise ValueError("file_id is required")
    from config import load_settings
    from drive_client import GoogleDriveClient, GoogleDriveClientError
    from document_parse_runtime import build_parse_config_from_settings, parse_document

    app_settings = load_settings(require_groq=False, require_google=False)
    client = GoogleDriveClient(app_settings)
    metadata = client.get_file_metadata(fid)
    title = str(file_name or metadata.get("name") or "document").strip()
    downloaded = client.download_content(
        metadata,
        max_bytes=int(app_settings.google_drive_max_download_bytes),
    )
    parse_config = build_parse_config_from_settings(app_settings)
    result = parse_document(
        downloaded.data,
        mime_type=str(downloaded.mime_type or metadata.get("mimeType") or ""),
        file_name=title,
        config=parse_config,
        force_hard_lane=force_hard_lane,
    )
    extraction = result.to_extraction_dict()
    text = str(extraction.get("extracted_text") or "").strip()
    return {
        "file_id": fid,
        "file_name": title,
        "extracted_text": text,
        "parser_name": str(extraction.get("parser_name") or result.parser_id or ""),
        "extraction_status": str(extraction.get("extraction_status") or "ok"),
        "extraction_confidence": float(extraction.get("extraction_confidence") or 0.0),
        "structured_facts": list((extraction.get("metadata") or {}).get("structured_facts") or []),
        "mime_type": str(downloaded.mime_type or ""),
    }
