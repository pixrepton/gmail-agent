"""Daszek V3 operational feed snapshot — single contract surface for Node B.

Mirror any FORBIDDEN_* changes with Daszek/includes/api-v2.php
(`daszek_v3_validate_operational_feed_snapshot_payload` + recursive walker).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

OPERATIONAL_FEED_SCHEMA_NAME = "daszek_operational_feed_snapshot"
OPERATIONAL_FEED_SCHEMA_VERSION_CANONICAL = "1"
OPERATIONAL_FEED_SCHEMA_VERSION_1_2 = "1.2"
OPERATIONAL_FEED_SCHEMA_VERSION_1_3 = "1.3"
OPERATIONAL_FEED_SCHEMA_VERSION_LATEST = OPERATIONAL_FEED_SCHEMA_VERSION_1_3

_LEGACY_SCHEMA_VERSIONS = frozenset({"1", "1.0", "1.1"})
_FEED_LIST_KEYS = ("desk", "cases", "tasks", "action_items")

# Keys that must never appear anywhere in the POST payload (projection privacy).
FORBIDDEN_KEYS_ANYWHERE: frozenset[str] = frozenset(
    {
        "email_body",
        "body",
        "snippet",
        "subject",
        "raw_llm",
        "raw_response",
        "raw_body",
        "message_body",
        "prompt",
        "prompt_text",
        "attachment_bytes",
    }
)

_STRIP_MAX_DEPTH = 10
_WALK_MAX_NODES = 50_000


@dataclass
class OperationalFeedValidationReport:
    """Result of `validate_operational_feed_snapshot`."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def strip_forbidden_nested(obj: Any, *, depth: int = 0) -> Any:
    """Remove forbidden keys recursively (same semantics as legacy _strip_forbidden)."""

    if depth > _STRIP_MAX_DEPTH:
        return obj
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in FORBIDDEN_KEYS_ANYWHERE:
                continue
            out[k] = strip_forbidden_nested(v, depth=depth + 1)
        return out
    if isinstance(obj, list):
        return [strip_forbidden_nested(x, depth=depth + 1) for x in obj]
    return obj


def _walk_keys(
    obj: Any,
    *,
    path: str,
    depth: int,
    nodes: list[int],
    errors: list[str],
) -> None:
    if depth > _STRIP_MAX_DEPTH:
        errors.append(f"{path or '/'}: przekroczono maksymalną głębokość skanu")
        return
    nodes[0] += 1
    if nodes[0] > _WALK_MAX_NODES:
        errors.append("/: przekroczono limit węzłów skanu payloadu")
        return
    if isinstance(obj, dict):
        for key, val in obj.items():
            seg = str(key)
            p = f"{path}/{seg}" if path else f"/{seg}"
            if seg in FORBIDDEN_KEYS_ANYWHERE:
                errors.append(f"zabroniony klucz {seg} pod ścieżką {p}")
            _walk_keys(val, path=p, depth=depth + 1, nodes=nodes, errors=errors)
    elif isinstance(obj, list):
        for i, val in enumerate(obj):
            _walk_keys(val, path=f"{path}[{i}]", depth=depth + 1, nodes=nodes, errors=errors)


def validate_operational_feed_snapshot(obj: Any) -> OperationalFeedValidationReport:
    """Structural + privacy validation for an operational feed snapshot dict."""

    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(obj, dict):
        return OperationalFeedValidationReport(ok=False, errors=["payload musi być obiektem"], warnings=warnings)

    if str(obj.get("schema_name") or "") != OPERATIONAL_FEED_SCHEMA_NAME:
        errors.append("schema_name musi być daszek_operational_feed_snapshot")

    sv = str(obj.get("schema_version") or "").strip()
    if not sv:
        errors.append("brak schema_version")
    elif sv not in _LEGACY_SCHEMA_VERSIONS | {OPERATIONAL_FEED_SCHEMA_VERSION_1_2, OPERATIONAL_FEED_SCHEMA_VERSION_1_3}:
        warnings.append(
            f"schema_version '{sv}' — najnowsza wartość to '{OPERATIONAL_FEED_SCHEMA_VERSION_LATEST}'"
        )
    if sv == OPERATIONAL_FEED_SCHEMA_VERSION_1_2:
        warnings.append("feed.tasks jest deprecated; używaj feed.action_items (shim tasks utrzymany do 1.3)")

    if not str(obj.get("snapshot_id") or "").strip():
        errors.append("brak snapshot_id")

    if obj.get("read_only") is not True:
        errors.append("read_only musi być true")

    if obj.get("creates_cases") is not False:
        errors.append("creates_cases musi być false")

    if obj.get("executes_actions") is not False:
        errors.append("executes_actions musi być false")

    feed = obj.get("feed")
    if not isinstance(feed, dict):
        errors.append("brak obiektu feed")
    else:
        for list_key in _FEED_LIST_KEYS:
            if list_key in feed and feed[list_key] is not None and not isinstance(feed[list_key], list):
                errors.append(f"feed.{list_key} musi być listą lub absent/null")
        if sv == OPERATIONAL_FEED_SCHEMA_VERSION_1_2:
            if "action_items" not in feed or feed.get("action_items") is None:
                errors.append("feed.action_items jest wymagane dla schema_version 1.2")
        elif sv == OPERATIONAL_FEED_SCHEMA_VERSION_1_3:
            if "action_items" not in feed or feed.get("action_items") is None:
                errors.append("feed.action_items jest wymagane dla schema_version 1.3")
            if "tasks" in feed:
                warnings.append("feed.tasks usunięte w schema 1.3; używaj feed.action_items")
        elif sv in _LEGACY_SCHEMA_VERSIONS:
            if "tasks" not in feed and "action_items" not in feed:
                warnings.append("feed.tasks lub feed.action_items zalecane (legacy schema)")
        if "case_details" in feed and feed["case_details"] is not None and not isinstance(feed["case_details"], dict):
            errors.append("feed.case_details musi być obiektem mapy")
        if "day" in feed and feed["day"] is not None and not isinstance(feed["day"], dict):
            errors.append("feed.day musi być obiektem")
        qr = feed.get("quality_readonly")
        if qr is not None:
            if not isinstance(qr, dict):
                errors.append("feed.quality_readonly musi być obiektem")
            else:
                if qr.get("read_only") is not True:
                    errors.append("feed.quality_readonly.read_only musi być true")
                if str(qr.get("projection_type") or "") != "quality_readonly":
                    errors.append("feed.quality_readonly.projection_type musi być quality_readonly")

    nodes = [0]
    _walk_keys(obj, path="", depth=0, nodes=nodes, errors=errors)

    uniq = list(dict.fromkeys(errors))
    return OperationalFeedValidationReport(ok=len(uniq) == 0, errors=uniq, warnings=warnings)


def desk_note_ref_warnings(feed: Any, existing_note_ids: set[str] | frozenset[str]) -> list[str]:
    """Same semantics as PHP daszek_v3_validate_operational_feed_desk_note_refs (non-strict).

    Skips synthetic projection-only note_ids (prefix ``desk-``) from mailbox exporter.
    """

    ids = existing_note_ids if isinstance(existing_note_ids, frozenset) else frozenset(existing_note_ids)
    if not isinstance(feed, dict):
        return []
    desk = feed.get("desk")
    if not isinstance(desk, list):
        return []
    missing: list[str] = []
    for item in desk:
        if not isinstance(item, dict):
            continue
        nid = str(item.get("note_id") or "").strip()
        if not nid:
            continue
        if nid.lower().startswith("desk-"):
            continue
        if nid not in ids:
            missing.append(nid)
    missing = list(dict.fromkeys(missing))
    return [f"Kartka z biurka (note_id) nie istnieje w magazynie v2: {mid}" for mid in missing]


def assert_operational_feed_valid(obj: Any) -> None:
    """Fail-fast for exporters / tests."""

    rep = validate_operational_feed_snapshot(obj)
    if not rep.ok:
        raise ValueError("operational feed invalid: " + "; ".join(rep.errors))
