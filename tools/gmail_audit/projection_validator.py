"""ProjectionEnvelope safety validation."""

from __future__ import annotations

from typing import Any

from context_tray_set import FORBIDDEN_RAW_KEYS


def _walk(value: Any, path: str = "$") -> list[tuple[str, Any]]:
    rows = [(path, value)]
    if isinstance(value, dict):
        for key, item in value.items():
            rows.extend(_walk(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            rows.extend(_walk(item, f"{path}[{idx}]"))
    return rows


def validate_projection_envelope(
    envelope: dict[str, Any],
    *,
    context_tray_set: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a read-only validation report for ProjectionEnvelope."""

    env = envelope if isinstance(envelope, dict) else {}
    errors: list[str] = []
    warnings: list[str] = []
    if env.get("schema_version") != "projection_envelope.v1":
        errors.append("schema_version must be projection_envelope.v1")
    if env.get("read_only") is not True:
        errors.append("projection envelope must be read_only=true")
    if env.get("action_allowed") is not False:
        errors.append("projection envelope must have action_allowed=false")

    for path, value in _walk(env):
        key = path.split(".")[-1].split("[")[0]
        if key in FORBIDDEN_RAW_KEYS:
            errors.append(f"forbidden raw field at {path}")
        if key == "action_allowed" and value is True:
            errors.append(f"action_allowed true at {path}")
        if key == "read_only" and value is False:
            errors.append(f"read_only false at {path}")

    for idx, row in enumerate(env.get("task_candidates") or []):
        if isinstance(row, dict) and row.get("action_allowed") is not False:
            errors.append(f"task_candidates[{idx}].action_allowed must be false")

    ctx = context_tray_set if isinstance(context_tray_set, dict) else {}
    if ctx.get("conflicts_tray") and not env.get("conflict_blocks"):
        errors.append("context conflicts must remain visible in conflict_blocks")
    if not env.get("evidence_used") and env.get("case_detail_blocks"):
        warnings.append("projection has detail blocks without evidence_used")

    return {
        "schema_version": "projection_validation.v1",
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
    }


__all__ = ["validate_projection_envelope"]
