"""Load historical Cieplo workflows from Postgres (shared mailbox_memory DB)."""

from __future__ import annotations

import json
from typing import Any

try:
    from .._protocols import DatabaseConnection
except ImportError:
    from _protocols import DatabaseConnection  # type: ignore[no-redef]


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def extract_workflow_sync_fields(row: dict[str, Any]) -> dict[str, str]:
    """Map workflows table row to sync_cieplo_workflow() kwargs."""
    parsed = _as_dict(row.get("parsed_result"))
    parsed_email = _as_dict(parsed.get("parsed_email"))
    ingress = _as_dict(row.get("ingress_payload"))
    payload = _as_dict(ingress.get("payload"))
    payload_email = _as_dict(payload.get("parsed_email"))

    client_email = str(
        parsed_email.get("client_email")
        or payload_email.get("client_email")
        or ""
    ).strip()

    cieplo_url = str(
        parsed_email.get("cieplo_url")
        or parsed_email.get("url")
        or payload_email.get("cieplo_url")
        or payload_email.get("url")
        or ""
    ).strip()

    external_key = ""
    if client_email and cieplo_url:
        external_key = f"{client_email.lower()}|{cieplo_url}"

    return {
        "workflow_id": str(row.get("id") or "").strip(),
        "client_email": client_email,
        "message_id": str(row.get("message_id") or "").strip(),
        "trace_id": str(row.get("trace_id") or "").strip(),
        "external_key": external_key,
    }


def fetch_workflows_from_db(conn: DatabaseConnection) -> list[dict[str, str]]:
    """Read all rows from workflows table (orchestrator SoT in mailbox_memory Postgres)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, message_id, trace_id, ingress_payload, parsed_result
            FROM workflows
            ORDER BY created_at ASC NULLS LAST
            """
        )
        rows = cur.fetchall()
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        fields = extract_workflow_sync_fields(row)
        if fields.get("workflow_id"):
            out.append(fields)
    return out
