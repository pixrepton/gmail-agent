"""Roadmap 2.4 — operator feed-visibility override via Node B single-writer gateway."""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from agent_runtime.agent_reconcile import build_operator_engagement_store
from agent_runtime.store import AgentConcurrencyError, OperatorEngagementStore
from agent_hitl_bridge import best_effort_push_engagement_feed_after_hitl
from config import Settings, load_settings
from event_spine.emitter import publish_os_event
from feed_visibility import (
    apply_operator_visibility_override,
    clear_operator_visibility_override,
    effective_visibility_mode,
)
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2, FeedVisibility

logger = logging.getLogger(__name__)

_OPERATOR_OVERRIDE_MODES = frozenset({"hidden", "case_timeline_only", "main_feed"})


def _stable_decision_key(engagement_id: str, mode: str, operator_id: str, *, clear: bool) -> str:
    raw = f"feed_visibility_override|{engagement_id}|{mode}|{operator_id}|clear={clear}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _stored_base_mode(snapshot: EngagementSnapshotV2) -> str | None:
    stored = snapshot.feed_visibility
    if stored is None:
        return None
    return str(getattr(stored, "mode", "") or "") or None


def _projection_receipt(
    snapshot: EngagementSnapshotV2,
    *,
    requested_override_mode: str | None,
    clear: bool = False,
) -> dict[str, Any]:
    """Build a projection receipt with requested vs effective visibility separated.

    Operator override modes (request / stored base) are only:
    ``hidden | case_timeline_only | main_feed``.

    ``attention_required`` is never a requestable override — it is a read-time
    effective classification when executive work is outstanding (HITL, etc.).
    """
    from daszek_engagement_feed.case import (
        snapshot_to_feed_case,
        why_on_desk_reason_codes_from_snapshot,
    )
    from daszek_engagement_feed.desk import snapshot_to_desk_item

    case_row = snapshot_to_feed_case(snapshot)
    desk_row = snapshot_to_desk_item(snapshot)
    effective_mode, reasons = effective_visibility_mode(snapshot)
    stored = snapshot.feed_visibility
    stored_mode = _stored_base_mode(snapshot)
    return {
        "case_id": snapshot.case_id,
        "engagement_id": snapshot.engagement_id,
        "version": snapshot.version,
        "feed_visibility": stored.model_dump(mode="python") if stored is not None else None,
        # Canonical split (Roadmap 2.4 contract hardening):
        # requested_override_mode — operator decision in this request (null on clear)
        # stored_feed_visibility_mode — base mode currently on snapshot (may be domain-derived)
        # effective_feed_visibility_mode — projection after executive rules (may be attention_required)
        "requested_override_mode": requested_override_mode,
        "stored_feed_visibility_mode": stored_mode,
        "effective_feed_visibility_mode": effective_mode,
        "cleared": bool(clear),
        # Compatibility alias — historically meant effective projection mode:
        "feed_visibility_mode": effective_mode,
        "why_on_desk_reason_codes": why_on_desk_reason_codes_from_snapshot(snapshot),
        "feed_case": case_row,
        "desk_item": desk_row,
        "case_understanding_status": (
            snapshot.case_understanding_status.model_dump(mode="python")
            if snapshot.case_understanding_status is not None
            else None
        ),
        "case_readiness_unchanged": True,
        "effective_reason_codes": reasons,
    }


def _compute_visibility_patch(
    snapshot: EngagementSnapshotV2,
    *,
    mode: str | None,
    clear: bool,
    reason: str,
) -> dict[str, Any] | None:
    stored = snapshot.feed_visibility
    if clear:
        return clear_operator_visibility_override(stored)
    target = str(mode or "").strip().lower()
    if not target:
        raise ValueError("mode is required unless clear=true")
    if target not in _OPERATOR_OVERRIDE_MODES:
        raise ValueError(f"invalid operator override mode: {target!r}")
    return apply_operator_visibility_override(stored, mode=target, reason=reason)


def _visibility_dict_unchanged(current: Any, patch: dict[str, Any] | None) -> bool:
    if patch is None:
        return True
    if current is None:
        return False
    current_mode = str(getattr(current, "mode", "") or "")
    current_override = bool(getattr(current, "operator_override", False))
    return current_mode == str(patch.get("mode") or "") and current_override == bool(patch.get("operator_override"))


def apply_operator_feed_visibility_override(
    *,
    engagement_id: str,
    operator_id: str,
    settings: Settings | None = None,
    mode: str | None = None,
    clear: bool = False,
    reason: str = "",
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Persist an operator visibility override on the engagement snapshot (Node B SoT).

    Request contract:
    - ``clear=false`` (default): ``mode`` required; one of ``hidden|case_timeline_only|main_feed``.
    - ``clear=true``: ``mode`` must be absent/null/empty; conflicting mode → ``ambiguous_request``.
    - ``attention_required`` is never accepted as ``mode`` (effective-only classification).
    """
    settings = settings or load_settings(require_groq=False, require_google=False)
    eid = str(engagement_id or "").strip()
    if not eid:
        return {"ok": False, "error": "engagement_id is required"}

    mode_normalized = str(mode).strip().lower() if mode is not None and str(mode).strip() else None
    if clear and mode_normalized:
        return {
            "ok": False,
            "error": "ambiguous_request: clear=true cannot be combined with mode",
            "status": "ambiguous_request",
        }
    if not clear and not mode_normalized:
        return {
            "ok": False,
            "error": "mode is required unless clear=true",
            "status": "invalid_mode",
        }

    requested_override_mode: str | None = None if clear else mode_normalized

    operator_store = build_operator_engagement_store(settings)
    if operator_store is None:
        return {"ok": False, "error": "operator engagement store is not configured"}

    snapshot = operator_store.load_snapshot(eid)
    if snapshot is None:
        return {"ok": False, "error": "engagement not found", "status": "not_found"}

    before_understanding = (
        snapshot.case_understanding_status.model_dump(mode="python")
        if snapshot.case_understanding_status is not None
        else None
    )
    before_ops = snapshot.operational_status.model_dump(mode="python")

    try:
        patch = _compute_visibility_patch(
            snapshot, mode=mode_normalized, clear=clear, reason=reason
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "status": "invalid_mode"}

    if patch is None and clear:
        receipt = _projection_receipt(snapshot, requested_override_mode=None, clear=True)
        return {
            "ok": True,
            "idempotent_replay": True,
            "decision_key": _stable_decision_key(eid, "", operator_id, clear=True),
            "receipt": receipt,
            **receipt,
        }

    if _visibility_dict_unchanged(snapshot.feed_visibility, patch):
        receipt = _projection_receipt(
            snapshot,
            requested_override_mode=requested_override_mode,
            clear=clear,
        )
        return {
            "ok": True,
            "idempotent_replay": True,
            "decision_key": _stable_decision_key(
                eid,
                str(patch.get("mode") or "") if patch else "",
                operator_id,
                clear=clear,
            ),
            "receipt": receipt,
            **receipt,
        }

    patched = snapshot.model_copy(update={"feed_visibility": FeedVisibility(**patch)})
    if (
        patched.case_understanding_status.model_dump(mode="python")
        if patched.case_understanding_status is not None
        else None
    ) != before_understanding:
        return {"ok": False, "error": "case_understanding_status must not change", "status": "forbidden"}
    if patched.operational_status.model_dump(mode="python") != before_ops:
        return {"ok": False, "error": "operational_status must not change", "status": "forbidden"}

    version_expected = int(expected_version if expected_version is not None else snapshot.version)
    try:
        new_version = operator_store.save_snapshot(patched, expected_version=version_expected)
    except AgentConcurrencyError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "status": "version_conflict",
            "expected_version": version_expected,
        }

    saved = patched.model_copy(update={"version": new_version})
    decision_key = _stable_decision_key(
        eid,
        str(patch.get("mode") or ""),
        operator_id,
        clear=clear,
    )

    db_url = str(
        getattr(settings, "mailbox_memory_database_url", "")
        or ""
    ).strip()
    os_event_id = None
    if db_url:
        try:
            os_event_id = publish_os_event(
                database_url=db_url,
                event_type="gmail.operator.feed_visibility_override",
                engagement_id=eid,
                source_repo="gmail-agent",
                payload={
                    "schema_version": "topinstal.os_event.v1",
                    "summary_pl": "Operator zmienil klasyfikacje widocznosci feedu",
                    "status": "ok",
                    "requested_override_mode": requested_override_mode,
                    "mode": patch.get("mode"),
                    "clear": clear,
                    "reason": str(reason or "")[:80],
                    "operator_id": operator_id,
                    "decision_key": decision_key,
                },
                correlation={
                    "case_id": str(saved.case_id or ""),
                    "adjudication_kind": "operator_feed_visibility_override",
                    "approve_key": decision_key,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("FEED_VISIBILITY_OVERRIDE_OS_EVENT_FAILED: %s", exc)

    feed_push = best_effort_push_engagement_feed_after_hitl(
        settings=settings,
        operator_store=operator_store,
        engagement_id=eid,
        case_id=str(saved.case_id or ""),
    )
    receipt = _projection_receipt(
        saved,
        requested_override_mode=requested_override_mode,
        clear=clear,
    )
    return {
        "ok": True,
        "idempotent_replay": False,
        "decision_key": decision_key,
        "os_event_id": os_event_id,
        "feed_push": feed_push,
        "receipt": receipt,
        **receipt,
    }


def build_operational_feed_preview(
    settings: Settings | None = None,
    *,
    exceptions_only: bool = False,
    case_limit: int = 50,
) -> dict[str, Any]:
    """Read-only operational feed build for Daszek view toggles."""
    settings = settings or load_settings(require_groq=False, require_google=False)
    operator_store = build_operator_engagement_store(settings)
    if operator_store is None:
        return {"ok": False, "error": "operator engagement store is not configured", "feed": None}

    from daszek_engagement_feed.build import build_operational_feed_from_engagement_store

    envelope = build_operational_feed_from_engagement_store(
        operator_store,
        case_limit=max(1, int(case_limit)),
        exceptions_only=bool(exceptions_only),
        source={"trigger": "operator_feed_preview", "exceptions_only": bool(exceptions_only)},
    )
    return {"ok": True, "snapshot": envelope, "exceptions_only": bool(exceptions_only)}


__all__ = [
    "apply_operator_feed_visibility_override",
    "build_operational_feed_preview",
]
