"""Operational feed V3 push after reconcile (CEL: envelope → router → feed → Daszek)."""

from __future__ import annotations
from log_config import get_logger

import time
import concurrent.futures
from typing import Any

log = get_logger(__name__)

from artifact_io import append_jsonl
from daszek_client import DaszekClientError
from event_spine.gmail_telemetry import publish_gmail_feed_push_event
from redaction import sanitize_for_storage
from config import Settings

# ── Async push pool (Krok 4) ──────────────────────────────────────────────────
# Osobny ThreadPoolExecutor dla pushy do Daszka — nie blokuje workera.
_push_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="daszek_push",
)

# ── Feed cache (Faza 6b) ──────────────────────────────────────────────────────
_feed_cache: dict[str, Any] = {}
_feed_cache_ts: float = 0.0
_FEED_CACHE_TTL: float = 30.0  # sekund


def _store_version_hash(store: Any) -> str:
    """Zwraca hash stanu store — używany do unieważnienia cache feeda."""
    try:
        stats = store.get_stats()
        return f"{stats.get('case_count', 0)}:{stats.get('max_updated_at', '')}"
    except Exception:
        return str(time.time())


def _get_cached_feed(store: Any) -> dict[str, Any] | None:
    global _feed_cache, _feed_cache_ts
    if _feed_cache and (time.monotonic() - _feed_cache_ts) < _FEED_CACHE_TTL:
        cached_version = _feed_cache.get("_store_version", "")
        if cached_version and cached_version == _store_version_hash(store):
            return _feed_cache
    return None


def _set_cached_feed(feed: dict[str, Any], store: Any) -> None:
    global _feed_cache, _feed_cache_ts
    _feed_cache = dict(feed)
    _feed_cache["_store_version"] = _store_version_hash(store)
    _feed_cache_ts = time.monotonic()


def _feed_push_log_path(run_state: dict[str, Any]) -> Any:
    path = run_state.get("daszek_v3_feed_push_path")
    if path is not None:
        return path
    artifacts = run_state.get("artifacts") or {}
    return artifacts.get("daszek_v3_feed_push_results")


def _use_engagement_feed_builder(settings: Settings) -> bool:
    from daszek_engagement_feed import engagement_feed_source_enabled

    return engagement_feed_source_enabled(settings)


def _projection_proof_mode(run_state: dict[str, Any]) -> bool:
    controls = run_state.get("runtime_controls") or {}
    if bool(controls.get("projection_proof")):
        return True
    manifest_controls = (run_state.get("manifest") or {}).get("runtime_controls") or {}
    return bool(manifest_controls.get("projection_proof"))


def flush_feed_push_pool(*, timeout_sec: float = 120.0) -> None:
    """Block until in-flight async feed pushes complete (recreates the shared pool)."""
    global _push_pool
    _ = timeout_sec  # reserved for future timed wait
    _push_pool.shutdown(wait=True, cancel_futures=False)
    _push_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="daszek_push",
    )


def _dispatch_feed_push(
    *,
    run_state: dict[str, Any],
    settings: Settings,
    snapshot: dict[str, Any],
    trigger_message_id: str,
) -> None:
    if _projection_proof_mode(run_state):
        _push_feed_snapshot_sync(
            run_state=run_state,
            settings=settings,
            snapshot=snapshot,
            trigger_message_id=trigger_message_id,
        )
        return
    _push_feed_snapshot_async(
        run_state=run_state,
        settings=settings,
        snapshot=snapshot,
        trigger_message_id=trigger_message_id,
    )


def _resolve_mailbox_store(run_state: dict[str, Any]) -> Any:
    runtime = run_state.get("mailbox_memory_runtime")
    if runtime is not None:
        return getattr(runtime, "store", None)
    return None


def _reconcile_engagement_id(reconcile_result: Any) -> str:
    mm = getattr(reconcile_result, "mailbox_memory_result", None)
    if isinstance(mm, dict):
        engagement_id = str(mm.get("engagement_id") or "").strip()
        if engagement_id:
            return engagement_id
    rebuild = getattr(reconcile_result, "rebuild_result", None)
    if isinstance(rebuild, dict):
        engagement_id = str(rebuild.get("engagement_id") or "").strip()
        if engagement_id:
            return engagement_id
    preview = getattr(reconcile_result, "preview", None)
    if isinstance(preview, dict):
        engagement_id = str(preview.get("engagement_id") or "").strip()
        if engagement_id:
            return engagement_id
    return ""


def _reconcile_signal_id(reconcile_result: Any) -> str:
    direct = str(getattr(reconcile_result, "signal_id", "") or "").strip()
    if direct:
        return direct
    for attr in ("mailbox_memory_result", "rebuild_result", "preview"):
        block = getattr(reconcile_result, attr, None)
        if isinstance(block, dict):
            signal_id = str(block.get("signal_id") or "").strip()
            if signal_id:
                return signal_id
    stage_outputs = getattr(reconcile_result, "stage_outputs", None)
    if isinstance(stage_outputs, dict):
        signal_id = str(stage_outputs.get("canonical_signal_id") or "").strip()
        if signal_id:
            return signal_id
        for key in ("signal_projection", "projection_preview", "agent_engagement_snapshot"):
            block = stage_outputs.get(key)
            if isinstance(block, dict):
                signal_id = str(block.get("signal_id") or "").strip()
                if signal_id:
                    return signal_id
    return ""


def _reconcile_source_ref(reconcile_result: Any) -> dict[str, Any]:
    for attr in ("signal_projection", "preview"):
        block = getattr(reconcile_result, attr, None)
        if isinstance(block, dict):
            source_ref = block.get("source_ref")
            if isinstance(source_ref, dict):
                return source_ref
    stage_outputs = getattr(reconcile_result, "stage_outputs", None)
    if isinstance(stage_outputs, dict):
        for key in ("signal_projection", "projection_preview"):
            block = stage_outputs.get(key)
            if isinstance(block, dict):
                source_ref = block.get("source_ref")
                if isinstance(source_ref, dict):
                    return source_ref
    return {}


def accumulate_engagement_feed_runtime_hint(
    run_state: dict[str, Any],
    reconcile_result: Any,
    *,
    trigger_message_id: str = "",
) -> None:
    hint = {
        "message_id": str(trigger_message_id or "").strip(),
        "signal_id": _reconcile_signal_id(reconcile_result),
        "engagement_id": _reconcile_engagement_id(reconcile_result),
        "thread_id": "",
    }
    source_ref = _reconcile_source_ref(reconcile_result)
    if isinstance(source_ref, dict):
        if not hint["message_id"]:
            hint["message_id"] = str(source_ref.get("message_id") or "").strip()
        hint["thread_id"] = str(source_ref.get("thread_id") or "").strip()
    if not hint["message_id"]:
        return
    hint["run_id"] = str(run_state.get("run_id") or "").strip()
    hints = run_state.setdefault("engagement_feed_runtime_hints", [])
    if not isinstance(hints, list):
        hints = []
        run_state["engagement_feed_runtime_hints"] = hints
    for idx, existing in enumerate(hints):
        if not isinstance(existing, dict):
            continue
        same_engagement = hint["engagement_id"] and str(existing.get("engagement_id") or "").strip() == hint["engagement_id"]
        same_signal = hint["signal_id"] and str(existing.get("signal_id") or "").strip() == hint["signal_id"]
        if same_engagement or same_signal:
            hints[idx] = hint
            break
    else:
        hints.append(hint)


def _apply_engagement_feed_runtime_hint(run_state: dict[str, Any], snapshot: dict[str, Any]) -> None:
    hints = run_state.get("engagement_feed_runtime_hints")
    if not isinstance(hints, list):
        return
    feed = snapshot.get("feed") if isinstance(snapshot.get("feed"), dict) else {}
    desk = feed.get("desk") if isinstance(feed.get("desk"), list) else []
    consumed: list[dict[str, Any]] = []
    for row in desk:
        if not isinstance(row, dict):
            continue
        row_engagement_id = str(row.get("engagement_id") or "").strip()
        row_signal_ids = [str(item or "").strip() for item in (row.get("source_signal_ids") or []) if str(item or "").strip()]
        for hint in hints:
            if not isinstance(hint, dict):
                continue
            message_id = str(hint.get("message_id") or "").strip()
            signal_id = str(hint.get("signal_id") or "").strip()
            engagement_id = str(hint.get("engagement_id") or "").strip()
            thread_id = str(hint.get("thread_id") or "").strip()
            if not message_id:
                continue
            if engagement_id and row_engagement_id == engagement_id:
                matched = True
            elif signal_id and signal_id in row_signal_ids:
                matched = True
            else:
                matched = False
            if not matched:
                continue
            if not str(row.get("source_message_id") or "").strip():
                row["source_message_id"] = message_id
            if thread_id and not str(row.get("thread_id") or "").strip():
                row["thread_id"] = thread_id
            consumed.append(hint)
            break
    if consumed:
        run_state["engagement_feed_runtime_hints"] = [
            hint for hint in hints if isinstance(hint, dict) and hint not in consumed
        ]


def _should_push_feed_for_reconcile(
    reconcile_result: Any,
    *,
    settings: Settings | None = None,
) -> bool:
    if reconcile_result is None:
        return False
    if str(getattr(reconcile_result, "processing_state", "") or "").strip() == "skipped_duplicate":
        return False
    case_id = str(getattr(reconcile_result, "case_id", "") or "").strip()
    if settings is not None and _use_engagement_feed_builder(settings):
        if case_id or _reconcile_engagement_id(reconcile_result):
            return True
    decision = getattr(reconcile_result, "projection_refresh_decision", None)
    if decision is not None and not bool(getattr(decision, "should_refresh", False)):
        return False
    return True


def operator_snapshot_from_reconcile(reconcile_result: Any) -> dict[str, Any] | None:
    """Return operator projection snapshot carried on reconcile stage_outputs."""

    stage_outputs = getattr(reconcile_result, "stage_outputs", None)
    if not isinstance(stage_outputs, dict):
        return None
    snap = stage_outputs.get("operator_projection_snapshot")
    return snap if isinstance(snap, dict) else None


def projection_routes_from_snapshot(operator_snapshot: dict[str, Any]) -> dict[str, Any] | None:
    routes = operator_snapshot.get("daszek_routes")
    if isinstance(routes, dict) and routes:
        return routes
    envelope = operator_snapshot.get("projection_envelope")
    if isinstance(envelope, dict) and envelope:
        from daszek_projection_router import route_projection_envelope

        return route_projection_envelope(envelope)
    return None


def case_id_and_routes_from_reconcile(reconcile_result: Any) -> tuple[str, dict[str, Any] | None]:
    snap = operator_snapshot_from_reconcile(reconcile_result)
    case_id = str(getattr(reconcile_result, "case_id", "") or "").strip()
    routes = projection_routes_from_snapshot(snap) if snap else None
    if snap and not case_id:
        envelope = snap.get("projection_envelope")
        if isinstance(envelope, dict):
            case_id = str(envelope.get("case_id") or "").strip()
    if not case_id and isinstance(routes, dict):
        case_id = str(routes.get("case_id") or "").strip()
    return case_id, routes if isinstance(routes, dict) else None


def _engagement_extra_case_ids(run_state: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    hints = run_state.get("engagement_feed_case_hints")
    if isinstance(hints, list):
        for cid in hints:
            cid_s = str(cid or "").strip()
            if cid_s and cid_s not in ids:
                ids.append(cid_s)
    overlays = run_state.get("projection_route_overlays")
    if isinstance(overlays, dict):
        for cid in overlays:
            cid_s = str(cid or "").strip()
            if cid_s and cid_s not in ids:
                ids.append(cid_s)
    return ids


def accumulate_engagement_feed_case_hint(
    run_state: dict[str, Any],
    reconcile_result: Any,
    *,
    settings: Settings | None = None,
) -> None:
    """Pin reconciled case_id for engagement feed (agent reconcile has no router overlays)."""

    if reconcile_result is None or not _should_push_feed_for_reconcile(reconcile_result, settings=settings):
        return
    from daszek_engagement_feed import resolve_reconcile_case_id_for_feed

    case_id = resolve_reconcile_case_id_for_feed(reconcile_result)
    if not case_id:
        return
    hints = run_state.setdefault("engagement_feed_case_hints", [])
    if not isinstance(hints, list):
        hints = []
        run_state["engagement_feed_case_hints"] = hints
    if case_id not in hints:
        hints.append(case_id)


def accumulate_projection_route_overlay(
    run_state: dict[str, Any],
    reconcile_result: Any,
    *,
    settings: Settings | None = None,
) -> None:
    """Store latest router surfaces per case for the debounced feed push in this run."""

    if reconcile_result is None or not _should_push_feed_for_reconcile(reconcile_result, settings=settings):
        return
    case_id, routes = case_id_and_routes_from_reconcile(reconcile_result)
    if not case_id or not routes:
        return
    overlays = run_state.setdefault("projection_route_overlays", {})
    if not isinstance(overlays, dict):
        overlays = {}
        run_state["projection_route_overlays"] = overlays
    overlays[case_id] = routes


def _log_feed_skip(run_state: dict[str, Any], *, message_id: str, reason: str, **extra: Any) -> None:
    summary = run_state.setdefault("summary", {})
    summary["operational_feed_push_skipped"] = int(summary.get("operational_feed_push_skipped") or 0) + 1
    log_path = _feed_push_log_path(run_state)
    if log_path is None:
        return
    row = {"record_type": "feed_skip", "surface": "v3_operational_feed", "message_id": message_id, "reason": reason}
    row.update(extra)
    append_jsonl(log_path, sanitize_for_storage(row))


def _push_feed_snapshot_async(
    *,
    run_state: dict[str, Any],
    settings: Settings,
    snapshot: dict[str, Any],
    trigger_message_id: str,
) -> None:
    """Asynchroniczny push do Daszek — nie blokuje workera. Bledy logowane w tle."""
    _push_pool.submit(
        _push_feed_snapshot_sync,
        run_state=run_state,
        settings=settings,
        snapshot=snapshot,
        trigger_message_id=trigger_message_id,
    )


def _push_feed_snapshot_sync(
    *,
    run_state: dict[str, Any],
    settings: Settings,
    snapshot: dict[str, Any],
    trigger_message_id: str,
) -> None:
    """Synchroniczny push do Daszek — uruchamiany w tle przez _push_feed_snapshot_async."""
    log_path = _feed_push_log_path(run_state)
    summary = run_state.setdefault("summary", {})
    client = run_state.get("daszek_client")
    if client is None:
        _log_feed_skip(
            run_state,
            message_id=trigger_message_id,
            reason="skipped_no_daszek_client",
            push_policy_detail="attach_daszek_client was not run; enable feed auto-push and Daszek credentials.",
        )
        return

    try:
        response = client.post_v3_operational_feed_snapshot(snapshot)
    except (DaszekClientError, ValueError) as exc:
        summary["operational_feed_push_failed"] = int(summary.get("operational_feed_push_failed") or 0) + 1
        publish_gmail_feed_push_event(
            settings,
            ok=False,
            error=str(exc),
            message_id=trigger_message_id,
            trigger="cel_reconcile",
        )
        if log_path is not None:
            append_jsonl(
                log_path,
                sanitize_for_storage(
                    {
                        "record_type": "feed_failure",
                        "surface": "v3_operational_feed",
                        "message_id": trigger_message_id,
                        "error": str(exc),
                        "snapshot_id": snapshot.get("snapshot_id"),
                    }
                ),
            )
        return  # Krok 4: nie propaguj bledu do wątku tła — log i kontynuuj

    snapshot_id = str(response.get("snapshot_id") or snapshot.get("snapshot_id") or "")
    publish_gmail_feed_push_event(
        settings,
        ok=True,
        snapshot_id=snapshot_id,
        message_id=trigger_message_id,
        trigger="cel_reconcile",
    )
    summary["last_operational_feed_push_monotonic"] = time.monotonic()
    summary["operational_feed_push_count"] = int(summary.get("operational_feed_push_count") or 0) + 1
    summary["last_operational_feed_snapshot_id"] = snapshot_id
    feed = snapshot.get("feed") if isinstance(snapshot.get("feed"), dict) else {}
    summary["last_operational_feed_counts"] = {
        "desk": len(feed.get("desk") or []),
        "cases": len(feed.get("cases") or []),
        "tasks": len(feed.get("tasks") or []),
    }
    overlays = run_state.get("projection_route_overlays")
    if isinstance(overlays, dict):
        summary["last_operational_feed_overlay_cases"] = len(overlays)
    if log_path is not None:
        row: dict[str, Any] = {
            "record_type": "feed_success",
            "surface": "v3_operational_feed",
            "message_id": trigger_message_id,
            "snapshot_id": snapshot_id,
            "ok": response.get("ok", True),
            "counts": summary.get("last_operational_feed_counts"),
            "overlay_cases": summary.get("last_operational_feed_overlay_cases"),
        }
        if _projection_proof_mode(run_state):
            row["snapshot_payload"] = snapshot
        append_jsonl(
            log_path,
            sanitize_for_storage(
                row
            ),
        )


def maybe_push_operational_feed_from_run_state(
    *,
    run_state: dict[str, Any],
    settings: Settings,
    trigger_message_id: str = "",
) -> None:
    """Build CEL feed (memory + router overlays) and POST when auto-push is enabled."""

    manifest = run_state.get("manifest") or {}
    if not bool(manifest.get("daszek_operational_feed_auto_push_enabled")):
        log.warning("feed_push_skip_auto_push_disabled trigger_message_id=%s", trigger_message_id)
        return

    summary = run_state.setdefault("summary", {})
    min_interval = max(
        0,
        int(getattr(settings, "daszek_operational_feed_push_min_interval_sec", 60) or 60),
    )
    if _projection_proof_mode(run_state):
        min_interval = 0
    now = time.monotonic()
    last_mono = float(summary.get("last_operational_feed_push_monotonic") or 0.0)
    if min_interval > 0 and last_mono and (now - last_mono) < min_interval:
        summary["operational_feed_push_debounced"] = int(summary.get("operational_feed_push_debounced") or 0) + 1
        log_path = _feed_push_log_path(run_state)
        if log_path is not None:
            append_jsonl(
                log_path,
                sanitize_for_storage(
                    {
                        "record_type": "feed_skip",
                        "surface": "v3_operational_feed",
                        "message_id": trigger_message_id,
                        "reason": "debounced_min_interval",
                        "min_interval_sec": min_interval,
                        "seconds_since_last": round(now - last_mono, 3),
                    }
                ),
            )
        return

    store = _resolve_mailbox_store(run_state)
    if store is None:
        _log_feed_skip(run_state, message_id=trigger_message_id, reason="skipped_no_mailbox_store")
        return

    if _use_engagement_feed_builder(settings):
        from daszek_engagement_feed import build_engagement_feed_for_cel

        case_limit = max(1, int(getattr(settings, "daszek_operational_feed_case_limit", 50) or 50))
        extra_case_ids = _engagement_extra_case_ids(run_state)
        snapshot = build_engagement_feed_for_cel(
            store,
            settings,
            case_limit=case_limit,
            trigger_message_id=trigger_message_id,
            run_id=str(run_state.get("run_id") or ""),
            extra_case_ids=extra_case_ids,
        )
        source = dict(snapshot.get("source") or {})
        source["source_run_id"] = str(run_state.get("run_id") or "")
        source["trigger_message_id"] = trigger_message_id
        source["cel_path"] = "engagement_snapshot_v2"
        snapshot["source"] = source
        _apply_engagement_feed_runtime_hint(run_state, snapshot)
        _dispatch_feed_push(
            run_state=run_state,
            settings=settings,
            snapshot=snapshot,
            trigger_message_id=trigger_message_id,
        )
        return

    from agent_runtime.primary_cutover import agent_runtime_primary_active, legacy_feed_explicitly_requested

    if agent_runtime_primary_active() and legacy_feed_explicitly_requested():
        log.warning("feed_push_skip_legacy_blocked_in_primary_mode trigger_message_id=%s", trigger_message_id)
        _log_feed_skip(
            run_state,
            message_id=trigger_message_id,
            reason="skipped_legacy_feed_in_primary_mode",
            push_policy_detail="Use DASZEK_FEED_SOURCE=engagement_snapshot_v2 or unset with AGENT_RUNTIME_MODE=primary.",
        )
        return

    from daszek_v3_operational_feed import build_operational_feed_for_cel

    case_limit = max(1, int(getattr(settings, "daszek_operational_feed_case_limit", 50) or 50))
    task_limit = max(1, int(getattr(settings, "daszek_operational_feed_task_limit", 80) or 80))
    overlays = run_state.get("projection_route_overlays")
    overlay_map = overlays if isinstance(overlays, dict) else {}
    # Krok 6: Incremental feed — jesli ostatni push byl <24h temu, filter tylko zmienione case'y
    last_push = float(summary.get("last_operational_feed_push_monotonic") or 0.0)
    since_days: int | None = None
    if last_push:
        elapsed_hours = (time.monotonic() - last_push) / 3600
        since_days = max(1, int(elapsed_hours / 24) + 1)
    # Faza 6b: Cache feeda z TTL — unikaj full rebuildu jeśli store się nie zmienił
    snapshot = _get_cached_feed(store)
    if snapshot is None:
        snapshot = build_operational_feed_for_cel(
            store,
            case_limit=case_limit,
            task_limit=task_limit,
            snapshot_id=None,
            route_overlays_by_case=overlay_map,
            since_days=since_days,  # Krok 6: filtr czasowy
        )
        _set_cached_feed(snapshot, store)
    else:
        log.info("feed_push_using_cached_snapshot trigger_message_id=%s", trigger_message_id)
    source = dict(snapshot.get("source") or {})
    source["source_run_id"] = str(run_state.get("run_id") or "")
    source["trigger_message_id"] = trigger_message_id
    source["cel_path"] = "mailbox_memory+projection_router_overlays"
    snapshot["source"] = source
    _dispatch_feed_push(
        run_state=run_state,
        settings=settings,
        snapshot=snapshot,
        trigger_message_id=trigger_message_id,
    )


def maybe_push_operational_feed_after_reconcile(
    *,
    run_state: dict[str, Any],
    settings: Settings,
    reconcile_result: Any,
    trigger_message_id: str = "",
) -> None:
    """Accumulate router overlay for this reconcile, then debounced push of the full CEL feed."""

    manifest = run_state.get("manifest") or {}
    if not bool(manifest.get("daszek_operational_feed_auto_push_enabled")):
        log.warning("feed_push_after_reconcile_skip_auto_push_disabled trigger_message_id=%s reconcile_result=%s", trigger_message_id, reconcile_result)
        return
    if reconcile_result is None or not _should_push_feed_for_reconcile(reconcile_result, settings=settings):
        log.warning("feed_push_after_reconcile_skip_should_not_push trigger_message_id=%s", trigger_message_id)
        if _projection_proof_mode(run_state):
            decision = getattr(reconcile_result, "projection_refresh_decision", None)
            detail = ""
            if decision is not None and not bool(getattr(decision, "should_refresh", False)):
                detail = "projection_refresh_decision.should_refresh=false"
            _log_feed_skip(
                run_state,
                message_id=trigger_message_id,
                reason="skipped_projection_refresh_not_needed",
                push_policy_detail=detail or "reconcile did not require feed refresh",
            )
        return

    accumulate_projection_route_overlay(run_state, reconcile_result, settings=settings)
    accumulate_engagement_feed_case_hint(run_state, reconcile_result, settings=settings)
    accumulate_engagement_feed_runtime_hint(
        run_state,
        reconcile_result,
        trigger_message_id=trigger_message_id,
    )
    maybe_push_operational_feed_from_run_state(
        run_state=run_state,
        settings=settings,
        trigger_message_id=trigger_message_id,
    )


def maybe_heartbeat_operational_feed(
    *,
    run_state: dict[str, Any],
    settings: Settings,
) -> None:
    """Periodic feed push when worker is idle (no reconcile). Respects debounce min_interval."""

    manifest = run_state.get("manifest") or {}
    if not bool(manifest.get("daszek_operational_feed_auto_push_enabled")):
        return
    maybe_push_operational_feed_from_run_state(
        run_state=run_state,
        settings=settings,
        trigger_message_id="worker-heartbeat",
    )


__all__ = [
    "accumulate_engagement_feed_case_hint",
    "accumulate_projection_route_overlay",
    "case_id_and_routes_from_reconcile",
    "flush_feed_push_pool",
    "maybe_heartbeat_operational_feed",
    "maybe_push_operational_feed_after_reconcile",
    "maybe_push_operational_feed_from_run_state",
    "operator_snapshot_from_reconcile",
    "projection_routes_from_snapshot",
]
