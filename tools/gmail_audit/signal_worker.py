"""Unified signal worker and poll loop for Gmail/Drive ingress."""

from __future__ import annotations

import signal
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any

from artifact_io import append_jsonl
from case_state_rebuilder import case_rebuild_from_journal
from config import ConfigError, Settings
from drive_change_detector import DriveChangeDetector
from drive_ingest_models import DriveIngestCandidate
from drive_lane_classifier import apply_classification
from gmail_change_detector import GmailChangeDetector
from intake_payload import build_source_snapshot
from signal_journal import SignalJournal
from signal_reconciler import SignalRuntimeContext, replay_signal
from log_config import get_logger
from agent_runtime.signal_worker_scheduler import PredictiveScheduler

logger = get_logger("signal_worker")


@dataclass(slots=True)
class SignalWorkerLoopResult:
    loop_mode: str
    runtime_mode: str
    iterations: int = 0
    gmail_event_count: int = 0
    drive_event_count: int = 0
    gmail_processed_count: int = 0
    drive_processed_count: int = 0
    replayed_signal_count: int = 0
    dry_run: bool = False
    heartbeat_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    warnings: list[str] = field(default_factory=list)
    run_id: str = ""
    run_dir: str = ""
    stop_reason: str = ""
    failed_item_count: int = 0
    last_errors: list[dict[str, Any]] = field(default_factory=list)
    run_state: dict[str, Any] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        # Avoid dataclasses.asdict(): it deep-copies and would traverse `run_state`,
        # which contains runtime objects (DB clients, HTTP sessions) and may be cyclic.
        item_failures = list(self.last_errors)
        return {
            "loop_mode": self.loop_mode,
            "runtime_mode": self.runtime_mode,
            "iterations": self.iterations,
            "gmail_event_count": self.gmail_event_count,
            "drive_event_count": self.drive_event_count,
            "gmail_processed_count": self.gmail_processed_count,
            "drive_processed_count": self.drive_processed_count,
            "replayed_signal_count": self.replayed_signal_count,
            "dry_run": self.dry_run,
            "heartbeat_at": self.heartbeat_at,
            "warnings": list(self.warnings),
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "stop_reason": self.stop_reason,
            "failed_item_count": self.failed_item_count,
            "last_errors": item_failures,
            "item_failures": item_failures,
            "last_error_summary": dict(item_failures[-1]) if item_failures else {},
        }


_FAILED_ITEMS_MAXLEN = 20
_MAX_CONSECUTIVE_SOURCE_FAILURES = 2
_MAX_CONSECUTIVE_PROJECTION_FAILURES = 2
_POLL_RETRY_ATTEMPTS = 2  # 1 initial + 1 retry
_POLL_RETRY_BASE_DELAY_SECONDS = 0.25
_SLA_WATCHER_INTERVAL_SEC = 15 * 60
_FOLLOW_UP_GUARDIAN_INTERVAL_SEC = 15 * 60


def _classify_worker_error(exc: BaseException) -> str:
    if isinstance(exc, ConfigError):
        return "config"
    message = str(exc or "").lower()
    if any(token in message for token in ("401", "403", "unauthorized", "forbidden", "invalid_grant")):
        return "auth"
    if any(token in message for token in ("429", "rate limit", "too many requests", "quota")):
        return "throttle"
    if any(token in message for token in ("timeout", "timed out", "deadline")):
        return "timeout"
    if any(token in message for token in ("connection", "network", "reset", "unreachable", "dns", "temporary failure")):
        return "network"
    if any(token in message for token in ("500", "502", "503", "504", "server error", "bad gateway", "service unavailable")):
        return "server_5xx"
    if any(token in message for token in ("400", "bad request", "validation", "invalid argument")):
        return "bad_request"
    return "unknown"


def _is_retryable_worker_error(category: str) -> bool:
    cat = str(category or "").strip().lower()
    if cat in {"timeout", "network", "server_5xx", "throttle"}:
        return True
    # Conservatively: unknown is not retried by default.
    return False


def _build_worker_error_fingerprint(*, stage: str, error_category: str, error_type: str) -> str:
    return f"{str(stage or '').strip()}|{str(error_category or '').strip()}|{str(error_type or '').strip()}"


def _rollup_run_level_error(
    run_state: dict[str, Any],
    *,
    stage: str,
    exc: BaseException,
    timestamp: str,
) -> str:
    summary = run_state.get("summary")
    if not isinstance(summary, dict):
        return ""
    error_type, error_message = _summarize_exception(exc)
    category = _classify_worker_error(exc)
    fingerprint = _build_worker_error_fingerprint(stage=stage, error_category=category, error_type=error_type)
    rollup = summary.setdefault("run_level_error_rollup", {})
    if not isinstance(rollup, dict):
        rollup = {}
        summary["run_level_error_rollup"] = rollup
    item = rollup.get(fingerprint)
    if not isinstance(item, dict):
        rollup[fingerprint] = {
            "fingerprint": fingerprint,
            "count": 1,
            "first_at": timestamp,
            "last_at": timestamp,
            "stage": stage,
            "error_category": category,
            "error_type": error_type,
            "error_message": error_message,
        }
        return fingerprint
    item["count"] = int(item.get("count") or 0) + 1
    item["last_at"] = timestamp
    # Keep original first_at. Keep original error_message to avoid churn.
    return fingerprint


def _append_bounded_failed_items(run_state: dict[str, Any], *, max_len: int = _FAILED_ITEMS_MAXLEN) -> None:
    summary = run_state.get("summary")
    if not isinstance(summary, dict):
        return
    failed_items = summary.get("failed_items")
    if not isinstance(failed_items, list):
        return
    if len(failed_items) > max(1, int(max_len)):
        summary["failed_items"] = failed_items[-max(1, int(max_len)) :]


def _summarize_exception(exc: BaseException) -> tuple[str, str]:
    error_type = exc.__class__.__name__
    message = str(exc).strip()
    if not message:
        message = "exception"
    # Avoid huge traces / accidentally dumping sensitive data. The detailed trace still exists in the Python exception,
    # but we only persist a compact summary via record_error -> sanitize_text/sanitize_for_storage.
    return error_type, message[:500]


def _record_item_failure(
    run_state: dict[str, Any],
    *,
    stage: str,
    message_id: str,
    exc: BaseException,
    details: dict[str, Any],
) -> None:
    record = run_state.get("_record_error")
    summary = run_state.get("summary")
    if not callable(record) or not isinstance(summary, dict):
        return

    error_type, error_message = _summarize_exception(exc)
    # NOTE: record_error does NOT increment summary["items_failed"] today; we increment once per item failure here.
    summary["items_failed"] = int(summary.get("items_failed") or 0) + 1
    record(
        run_state,
        stage=str(stage or "runtime"),
        message_id=str(message_id or ""),
        error=f"{error_type}: {error_message}",
        details=dict(details or {}),
    )
    _append_bounded_failed_items(run_state)


def _record_run_level_failure(
    run_state: dict[str, Any],
    *,
    stage: str,
    exc: BaseException,
    details: dict[str, Any] | None = None,
) -> None:
    """Record a run-level failure without treating it as an item failure.

    Uses run_state['_record_error'] for consistent artifact logging, but avoids populating summary['failed_items'],
    which is reserved for item-level failures.
    """
    record = run_state.get("_record_error")
    summary = run_state.get("summary")
    if not callable(record) or not isinstance(summary, dict):
        return
    error_type, error_message = _summarize_exception(exc)
    category = _classify_worker_error(exc)
    summary.setdefault("run_level_error_rollup", {})
    fingerprint = _rollup_run_level_error(run_state, stage=stage, exc=exc, timestamp=datetime.now().astimezone().isoformat())
    before_failed_items_len = len(summary.get("failed_items") or []) if isinstance(summary.get("failed_items"), list) else 0
    before_consecutive = int(summary.get("consecutive_failures") or 0)
    record(
        run_state,
        stage=str(stage or "runtime"),
        message_id="",
        error=f"{error_type}: {error_message}",
        details=dict({"error_category": category, "fingerprint": fingerprint, **(details or {})}),
    )
    # Undo item-level failure tracking side effects.
    failed_items = summary.get("failed_items")
    if isinstance(failed_items, list) and len(failed_items) > before_failed_items_len:
        # Drop entries added by record_error for this run-level incident.
        del failed_items[before_failed_items_len:]
    if int(summary.get("consecutive_failures") or 0) > before_consecutive:
        summary["consecutive_failures"] = before_consecutive


def _poll_with_retry(
    poll_fn,
    *,
    attempts: int = _POLL_RETRY_ATTEMPTS,
    base_delay_seconds: float = _POLL_RETRY_BASE_DELAY_SECONDS,
) -> dict[str, Any]:
    last_exc: BaseException | None = None
    for idx in range(max(1, int(attempts))):
        try:
            return poll_fn()
        except BaseException as exc:  # noqa: BLE001 - envelope boundary
            if _is_fatal_worker_exception(exc):
                raise
            last_exc = exc
            category = _classify_worker_error(exc)
            retryable = _is_retryable_worker_error(category)
            # bounded jitterless backoff; keep it tiny to avoid hiding issues
            if retryable and idx < max(1, int(attempts)) - 1:
                time.sleep(max(0.0, float(base_delay_seconds)) * (idx + 1))
                continue
            break
    assert last_exc is not None
    raise last_exc


def _is_fatal_worker_exception(exc: BaseException) -> bool:
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return True
    if isinstance(exc, ConfigError):
        return True
    return False


# ── Graceful shutdown ──────────────────────────────────────────────────────────
# SIGTERM handler: pozwala workerowi dokończyć bieżącą iterację i zamknąć się
# z checkpointem, zamiast ginąć w środku pętli.
_shutdown_event = threading.Event()


# ── Restart loop detection (Krok 1) ──────────────────────────────────────────
# Przechowuje hash ostatnich bledow — wykrywa petle restartu "ten sam blad 3x"
_REPEATED_ERROR_CACHE: dict[str, int] = {}
_REPEATED_ERROR_THRESHOLD = 3


def _is_repeated_error(error_text: str) -> bool:
    """Sprawdz czy ten sam blad wystapil 3+ razy z rzedu (restart loop)."""
    import hashlib
    h = hashlib.sha256((error_text or "").encode()).hexdigest()[:12]
    _REPEATED_ERROR_CACHE[h] = _REPEATED_ERROR_CACHE.get(h, 0) + 1
    return _REPEATED_ERROR_CACHE[h] >= _REPEATED_ERROR_THRESHOLD


def _handle_sigterm(signum: int, frame: object | None = None) -> None:
    """Ustaw event shutdown — pętla zakończy po bieżącej iteracji."""
    _shutdown_event.set()


def _sleep_with_abort(seconds: float) -> bool:
    """Sleep, ale przerwany przy SIGTERM. Zwraca True jeśli przerwano."""
    return _shutdown_event.wait(timeout=seconds)


def _process_gmail_message_in_worker(
    *,
    message_id: str,
    effective_settings: Settings,
    schema: Any,
    instructions: str,
    run_state: dict[str, Any],
    result: SignalWorkerLoopResult,
    verbose: bool,
    keep_going: bool,
    write_heartbeat: Any,
    event: dict[str, Any] | None = None,
) -> bool:
    """Fetch + reconcile one Gmail message. Returns process_snapshot keep_going flag."""
    from gmail_intake import fetch_context_messages, process_snapshot
    from runtime_imports import DEFAULT_GMAIL_SOURCE, read_email

    mid = str(message_id or "").strip()
    if not mid:
        return keep_going
    ev = event if isinstance(event, dict) else {}
    run_state["summary"]["items_selected"] += 1
    run_state["summary"]["items_seen"] += 1
    run_state["summary"]["items_fetched"] += 1
    try:
        source_message = read_email(
            effective_settings,
            message_id=mid,
            model=effective_settings.groq_model,
            verbose=verbose,
            gmail_source=DEFAULT_GMAIL_SOURCE,
        )
        source_message["history_id"] = str(
            source_message.get("history_id") or ev.get("history_id") or ""
        )
        context_messages = fetch_context_messages(
            effective_settings,
            source_message=source_message,
            context_limit=3,
            model=effective_settings.groq_model,
            verbose=verbose,
            gmail_source=DEFAULT_GMAIL_SOURCE,
        )
        snapshot = build_source_snapshot(
            mailbox=str(ev.get("mailbox") or "unknown"),
            source_message=source_message,
            context_messages=context_messages,
            observed_at=str(ev.get("observed_at") or source_message.get("date") or result.heartbeat_at),
        )
    except BaseException as exc:
        if _is_fatal_worker_exception(exc):
            raise
        _record_item_failure(
            run_state,
            stage="gmail_fetch",
            message_id=mid,
            exc=exc,
            details={
                "source_kind": "gmail",
                "source_id": str(ev.get("history_id") or ev.get("event_id") or ""),
                "message_id": mid,
            },
        )
        return keep_going
    try:
        should_continue = process_snapshot(
            settings=effective_settings,
            schema=schema,
            instructions=instructions,
            run_state=run_state,
            snapshot=snapshot,
            model=effective_settings.groq_model,
            verbose=verbose,
            keep_going=keep_going,
        )
    except BaseException as exc:
        if _is_fatal_worker_exception(exc):
            raise
        _record_item_failure(
            run_state,
            stage="gmail_reconcile",
            message_id=mid,
            exc=exc,
            details={
                "source_kind": "gmail",
                "source_id": str(ev.get("history_id") or ev.get("event_id") or ""),
                "message_id": mid,
            },
        )
        return keep_going
    result.gmail_processed_count += 1
    write_heartbeat(mid)
    return should_continue


def run_signal_loop(
    settings: Settings,
    *,
    loop_mode: str,
    dry_run: bool = False,
    max_iterations: int = 1,
    verbose: bool = False,
    push_daszek: bool = False,
    max_messages: int = 0,
    timebox_seconds: int = 0,
    pinned_message_id: str = "",
    projection_proof: bool = False,
    keep_going: bool = True,
) -> SignalWorkerLoopResult:
    effective_settings = _resolve_worker_settings(settings, dry_run=dry_run)
    if effective_settings.signal_runtime_mode != "active":
        raise ConfigError(
            "Signal worker requires SIGNAL_RUNTIME_MODE=active (only supported mode). "
            "Next check: python tools/gmail_audit/gmail_intake.py doctor --skip-gmail --verbose"
        )
    if not bool(effective_settings.signal_worker_enabled):
        raise ConfigError(
            "Signal worker requires SIGNAL_WORKER_ENABLED=1. "
            "Enable the flag and retry: python tools/gmail_audit/gmail_intake.py signal-run --oneshot --dry-run --verbose"
        )
    live_push_requested = bool(push_daszek) and effective_settings.signal_runtime_mode == "active"

    from drive_ingest_runtime import build_drive_ingest_runtime
    from drive_signal_adapter import build_drive_signal_runtime_context
    from gmail_intake import (
        RUNS_DIR,
        annotate_env_metadata,
        attach_daszek_client,
        attach_observability_runtime,
        fetch_context_messages,
        init_run_state,
        load_intake_schema,
        make_run_id,
        process_snapshot,
        render_system_prompt,
    )
    from runtime_imports import DEFAULT_GMAIL_SOURCE, read_email

    command_name = "signal-worker" if loop_mode == "continuous_poll" else "signal-run"
    run_id = make_run_id(command_name)
    run_dir = RUNS_DIR / run_id
    started_monotonic = time.monotonic()
    run_state = init_run_state(
        run_id=run_id,
        run_dir=run_dir,
        command=command_name,
        selector={
            "type": "signal_runtime",
            "loop_mode": loop_mode,
            "gmail_change_detection_enabled": bool(effective_settings.gmail_change_detection_enabled),
            "drive_change_detection_enabled": bool(effective_settings.drive_change_detection_enabled),
            "pinned_message_id": str(pinned_message_id or "").strip(),
        },
        mailbox="signal-runtime",
        model=effective_settings.groq_model,
        schema_path=None,
        source_run=None,
        push_daszek=live_push_requested,
        runtime_controls={
            "keep_going": bool(keep_going),
            "timebox_seconds": int(timebox_seconds or 0),
            "max_failures": 0,
            "max_consecutive_failures": 0,
            "max_messages": int(max_messages or 0),
            "projection_proof": bool(projection_proof),
        },
    )
    run_state["manifest"]["gmail_source"] = DEFAULT_GMAIL_SOURCE
    run_state["manifest"]["daszek_v2_push_enabled"] = bool(effective_settings.daszek_v2_push_enabled)
    run_state["manifest"]["daszek_operational_feed_auto_push_enabled"] = bool(
        getattr(effective_settings, "daszek_operational_feed_auto_push_enabled", False)
    )
    run_state["manifest"]["daszek_v2_readback_enabled"] = bool(effective_settings.daszek_v2_readback_enabled)
    run_state["manifest"]["daszek_v2_desk_relax_rejected"] = bool(
        getattr(effective_settings, "daszek_v2_desk_relax_rejected", False)
    )
    run_state["manifest"]["daszek_v2_desk_include_ignore"] = bool(
        getattr(effective_settings, "daszek_v2_desk_include_ignore", False)
    )
    run_state["manifest"]["signal_runtime_mode"] = effective_settings.signal_runtime_mode
    run_state["manifest"]["gmail_ingress_owner"] = str(getattr(effective_settings, "gmail_ingress_owner", "") or "")
    from agent_runtime.manifest import attach_agent_runtime_manifest

    attach_agent_runtime_manifest(run_state, effective_settings)
    run_state.setdefault("projection_route_overlays", {})
    from gmail_ingress_guard import ingress_owner_warnings

    for warning in ingress_owner_warnings(effective_settings):
        run_state.setdefault("warnings", []).append(warning)
    attach_observability_runtime(run_state, effective_settings, command_name=command_name)
    annotate_env_metadata(run_state, effective_settings)

    mailbox_runtime = _require_worker_mailbox_runtime(effective_settings)
    mailbox_runtime.bootstrap()
    run_state["mailbox_memory_runtime"] = mailbox_runtime
    drive_runtime = build_drive_ingest_runtime(
        effective_settings,
        store=mailbox_runtime.store,
        graph_store=getattr(mailbox_runtime, "graph_store", None),
    )
    if bool(effective_settings.drive_change_detection_enabled) and drive_runtime is None:
        raise ConfigError("Drive change detection requires GOOGLE_DRIVE_ENABLED=1 and shared mailbox memory storage.")
    if drive_runtime is not None:
        drive_runtime.bootstrap()

    if (
        live_push_requested
        or bool(effective_settings.daszek_v2_push_enabled)
        or bool(getattr(effective_settings, "daszek_operational_feed_auto_push_enabled", False))
    ):
        attach_daszek_client(run_state, effective_settings)

    result = SignalWorkerLoopResult(
        loop_mode=loop_mode,
        runtime_mode=effective_settings.signal_runtime_mode,
        dry_run=bool(dry_run),
        run_id=run_id,
        run_dir=str(run_dir),
        run_state=run_state,
    )

    try:
        schema = load_intake_schema(None)
        instructions = render_system_prompt()
        gmail_detector = GmailChangeDetector(effective_settings, store=mailbox_runtime.store)
        drive_detector = (
            DriveChangeDetector(effective_settings, store=mailbox_runtime.store, client=drive_runtime.client)
            if drive_runtime is not None
            else None
        )
        # Rejestruj SIGTERM handler dla graceful shutdown
        signal.signal(signal.SIGTERM, _handle_sigterm)

        # trace_id dla distributed tracing przez cały flow
        import uuid
        run_state.setdefault("trace_id", f"sig_{uuid.uuid4().hex[:16]}")
        from log_config import set_trace_id
        set_trace_id(str(run_state.get("trace_id") or ""))

        # Faza 0b: worker heartbeat — helpery do checkpoint/restart
        def _write_worker_heartbeat(message_id: str = "", last_error: str = "") -> None:
            try:
                db_url = str(getattr(effective_settings, "mailbox_memory_database_url", "") or "").strip()
                if not db_url:
                    return
                import psycopg
                with psycopg.connect(db_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO worker_heartbeat (worker_id, last_seen, iteration_count, loop_mode, last_message_id, last_error)
                            VALUES (%s, NOW(), %s, %s, %s, %s)
                            ON CONFLICT (worker_id) DO UPDATE SET
                                last_seen = NOW(),
                                iteration_count = EXCLUDED.iteration_count,
                                loop_mode = EXCLUDED.loop_mode,
                                last_message_id = COALESCE(NULLIF(EXCLUDED.last_message_id, ''), worker_heartbeat.last_message_id),
                                last_error = COALESCE(NULLIF(EXCLUDED.last_error, ''), worker_heartbeat.last_error)
                        """, ("gmail-worker", result.iterations, loop_mode, message_id, last_error or None))
            except Exception as exc:
                logger.warning("worker_heartbeat write failed: %s", exc)  # heartbeat best-effort

        def _read_worker_checkpoint() -> dict[str, str]:
            """Odczytaj ostatni checkpoint workera (ostatni przetworzony message_id + blad)."""
            try:
                db_url = str(getattr(effective_settings, "mailbox_memory_database_url", "") or "").strip()
                if not db_url:
                    return {}
                import psycopg
                with psycopg.connect(db_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT last_message_id, last_replayed_signal_id, iteration_count, last_error
                            FROM worker_heartbeat WHERE worker_id = 'gmail-worker'
                        """)
                        row = cur.fetchone()
                        if row:
                            return {"last_message_id": row[0] or "", "last_replayed_signal_id": row[1] or "", "iteration_count": int(row[2] or 0), "last_error": row[3] or ""}
            except Exception as exc:
                logger.warning("worker_checkpoint read failed: %s", exc)
            return {}

        # Faza checkpoint: odczytaj ostatni przetworzony message_id
        checkpoint = _read_worker_checkpoint()
        checkpoint_resume_id = str(checkpoint.get("last_message_id") or "").strip()
        past_checkpoint = not checkpoint_resume_id
        if checkpoint_resume_id:
            logger.info("WORKER_CHECKPOINT_RESTORE", extra={"x": {
                "last_message_id": checkpoint_resume_id,
                "previous_iterations": checkpoint.get("iteration_count", 0),
            }})
            summary = run_state.get("summary") if isinstance(run_state, dict) else None
            if isinstance(summary, dict):
                summary["worker_checkpoint_resume_from"] = checkpoint_resume_id

        pinned_mid = str(pinned_message_id or "").strip()
        if pinned_mid and loop_mode == "oneshot":
            result.gmail_event_count = 1
            should_continue = _process_gmail_message_in_worker(
                message_id=pinned_mid,
                effective_settings=effective_settings,
                schema=schema,
                instructions=instructions,
                run_state=run_state,
                result=result,
                verbose=verbose,
                keep_going=keep_going,
                write_heartbeat=_write_worker_heartbeat,
                event={"message_id": pinned_mid, "mailbox": "unknown"},
            )
            result.stop_reason = (
                "gmail_process_requested_stop" if not should_continue else "pinned_message_complete"
            )
            return result

        # Krok 1: wykryj petle restartu (ten sam blad 3+ razy)
        last_err = checkpoint.get("last_error", "")
        if last_err and _is_repeated_error(last_err):
            logger.critical("WORKER_RESTART_LOOP_DETECTED: %s — worker wstrzymany do recznej interwencji", last_err[:200])
            result.stop_reason = "restart_loop_detected"
            return result

        # Krok 2: PredictiveScheduler — dostosowuje czestotliwosc pollowania
        _scheduler: PredictiveScheduler | None = None
        if bool(effective_settings.gmail_change_detection_enabled):
            _scheduler = PredictiveScheduler(base_interval=_poll_sleep_seconds(effective_settings))

        # Glowna petla workera
        while True:
            result.iterations += 1
            # Faza 0b: heartbeat worker przy każdej iteracji
            _write_worker_heartbeat()
            try:
                from agent_runtime.agent_chat_worker import process_agent_chat_jobs_tick

                process_agent_chat_jobs_tick(effective_settings, max_jobs=1)
            except Exception as exc:  # noqa: BLE001
                logger.debug("agent_chat_jobs_tick skipped: %s", exc)
            result.heartbeat_at = datetime.now().astimezone().isoformat()
            if loop_mode != "continuous_poll":
                if int(timebox_seconds or 0) > 0 and (time.monotonic() - started_monotonic) >= float(timebox_seconds):
                    result.stop_reason = "timebox_reached"
                    break
                if int(max_messages or 0) > 0 and int(result.gmail_processed_count) >= int(max_messages):
                    result.stop_reason = "max_messages_reached"
                    break
            if bool(effective_settings.gmail_change_detection_enabled):
                summary = run_state.get("summary") if isinstance(run_state, dict) else None
                if isinstance(summary, dict):
                    summary.setdefault("source_failure_count", 0)
                    summary.setdefault("last_source_error_summary", {})
                    summary.setdefault("consecutive_source_failures", 0)
                try:
                    gmail_poll = _poll_with_retry(
                        lambda: gmail_detector.poll_changes(
                            cursor_scope="default",
                            max_results=100,
                            verbose=verbose,
                            bootstrap_if_missing=True,
                        )
                    )
                    if isinstance(summary, dict):
                        summary["consecutive_source_failures"] = 0
                except BaseException as exc:
                    if _is_fatal_worker_exception(exc):
                        raise
                    if isinstance(summary, dict):
                        summary["source_failure_count"] = int(summary.get("source_failure_count") or 0) + 1
                        summary["consecutive_source_failures"] = int(summary.get("consecutive_source_failures") or 0) + 1
                        error_type, error_message = _summarize_exception(exc)
                        error_category = _classify_worker_error(exc)
                        fingerprint = _build_worker_error_fingerprint(
                            stage="gmail_poll_changes",
                            error_category=error_category,
                            error_type=error_type,
                        )
                        summary["last_source_error_summary"] = {
                            "source_kind": "gmail",
                            "stage": "gmail_poll_changes",
                            "error_category": error_category,
                            "fingerprint": fingerprint,
                            "error_type": error_type,
                            "error_message": error_message,
                            "timestamp": result.heartbeat_at,
                        }
                    _record_run_level_failure(
                        run_state,
                        stage="gmail_poll_changes",
                        exc=exc,
                        details={"source_kind": "gmail"},
                    )
                    if isinstance(summary, dict) and int(summary.get("consecutive_source_failures") or 0) >= _MAX_CONSECUTIVE_SOURCE_FAILURES:
                        result.stop_reason = "max_consecutive_source_failures"
                        break
                    # For oneshot runs: stop cleanly; for continuous, continue to next loop iteration.
                    if loop_mode != "continuous_poll":
                        result.stop_reason = "gmail_poll_failed"
                        break
                    continue
                gmail_events = gmail_poll.get("events") or []
                result.gmail_event_count += len(gmail_events)
                if _scheduler:
                    _scheduler.record_volume(len(gmail_events))
                for raw_event in gmail_events:
                    if loop_mode != "continuous_poll":
                        if int(timebox_seconds or 0) > 0 and (time.monotonic() - started_monotonic) >= float(timebox_seconds):
                            result.stop_reason = "timebox_reached"
                            break
                        if int(max_messages or 0) > 0 and int(result.gmail_processed_count) >= int(max_messages):
                            result.stop_reason = "max_messages_reached"
                            break
                    event = raw_event if isinstance(raw_event, dict) else {}
                    message_id = str(event.get("message_id") or "").strip()
                    if not message_id:
                        continue
                    if not past_checkpoint:
                        if message_id == checkpoint_resume_id:
                            past_checkpoint = True
                        continue
                    run_state["summary"]["items_selected"] += 1
                    run_state["summary"]["items_seen"] += 1
                    run_state["summary"]["items_fetched"] += 1
                    try:
                        source_message = read_email(
                            effective_settings,
                            message_id=message_id,
                            model=effective_settings.groq_model,
                            verbose=verbose,
                            gmail_source=DEFAULT_GMAIL_SOURCE,
                        )
                        source_message["history_id"] = str(
                            source_message.get("history_id") or event.get("history_id") or ""
                        )
                        context_messages = fetch_context_messages(
                            effective_settings,
                            source_message=source_message,
                            context_limit=3,
                            model=effective_settings.groq_model,
                            verbose=verbose,
                            gmail_source=DEFAULT_GMAIL_SOURCE,
                        )
                        snapshot = build_source_snapshot(
                            mailbox=str(event.get("mailbox") or "unknown"),
                            source_message=source_message,
                            context_messages=context_messages,
                            observed_at=str(event.get("observed_at") or source_message.get("date") or result.heartbeat_at),
                        )
                    except BaseException as exc:
                        if _is_fatal_worker_exception(exc):
                            raise
                        _record_item_failure(
                            run_state,
                            stage="gmail_fetch",
                            message_id=message_id,
                            exc=exc,
                            details={
                                "source_kind": "gmail",
                                "source_id": str(event.get("history_id") or event.get("event_id") or ""),
                                "message_id": message_id,
                            },
                        )
                        continue
                    try:
                        should_continue = process_snapshot(
                            settings=effective_settings,
                            schema=schema,
                            instructions=instructions,
                            run_state=run_state,
                            snapshot=snapshot,
                            model=effective_settings.groq_model,
                            verbose=verbose,
                            keep_going=True,
                        )
                    except BaseException as exc:
                        if _is_fatal_worker_exception(exc):
                            raise
                        _record_item_failure(
                            run_state,
                            stage="gmail_reconcile",
                            message_id=message_id,
                            exc=exc,
                            details={
                                "source_kind": "gmail",
                                "source_id": str(event.get("history_id") or event.get("event_id") or ""),
                                "message_id": message_id,
                            },
                        )
                        continue
                    result.gmail_processed_count += 1
                    # Faza checkpoint: zapisz ostatni przetworzony message_id
                    _write_worker_heartbeat(message_id)
                    if not should_continue:
                        result.stop_reason = "gmail_process_requested_stop"
                        break
                if result.stop_reason:
                    break

            if bool(effective_settings.drive_change_detection_enabled) and drive_detector is not None and drive_runtime is not None:
                summary = run_state.get("summary") if isinstance(run_state, dict) else None
                if isinstance(summary, dict):
                    summary.setdefault("source_failure_count", 0)
                    summary.setdefault("last_source_error_summary", {})
                    summary.setdefault("consecutive_source_failures", 0)
                try:
                    drive_poll = _poll_with_retry(
                        lambda: drive_detector.poll_changes(
                            cursor_scope="default",
                            max_results=100,
                            bootstrap_if_missing=True,
                        )
                    )
                    if isinstance(summary, dict):
                        summary["consecutive_source_failures"] = 0
                except BaseException as exc:
                    if _is_fatal_worker_exception(exc):
                        raise
                    if isinstance(summary, dict):
                        summary["source_failure_count"] = int(summary.get("source_failure_count") or 0) + 1
                        summary["consecutive_source_failures"] = int(summary.get("consecutive_source_failures") or 0) + 1
                        error_type, error_message = _summarize_exception(exc)
                        error_category = _classify_worker_error(exc)
                        fingerprint = _build_worker_error_fingerprint(
                            stage="drive_poll_changes",
                            error_category=error_category,
                            error_type=error_type,
                        )
                        summary["last_source_error_summary"] = {
                            "source_kind": "drive",
                            "stage": "drive_poll_changes",
                            "error_category": error_category,
                            "fingerprint": fingerprint,
                            "error_type": error_type,
                            "error_message": error_message,
                            "timestamp": result.heartbeat_at,
                        }
                    _record_run_level_failure(
                        run_state,
                        stage="drive_poll_changes",
                        exc=exc,
                        details={"source_kind": "drive"},
                    )
                    if isinstance(summary, dict) and int(summary.get("consecutive_source_failures") or 0) >= _MAX_CONSECUTIVE_SOURCE_FAILURES:
                        result.stop_reason = "max_consecutive_source_failures"
                        break
                    if loop_mode != "continuous_poll":
                        result.stop_reason = "drive_poll_failed"
                        break
                    continue
                drive_events = drive_poll.get("events") or []
                result.drive_event_count += len(drive_events)
                signal_runtime_context = build_drive_signal_runtime_context(
                    settings=effective_settings,
                    store=mailbox_runtime.store,
                    graph_store=getattr(mailbox_runtime, "graph_store", None),
                    run_state=run_state,
                )
                for raw_event in drive_events:
                    event = raw_event if isinstance(raw_event, dict) else {}
                    file_id = str(event.get("file_id") or "").strip()
                    if not file_id:
                        continue
                    run_state["summary"]["items_selected"] += 1
                    run_state["summary"]["items_seen"] += 1
                    run_state["summary"]["items_fetched"] += 1
                    try:
                        if bool(event.get("removed")):
                            processed = drive_runtime.process_removed_item(
                                drive_item_id=file_id,
                                change_id=str(event.get("change_id") or ""),
                                observed_at=str(event.get("observed_at") or result.heartbeat_at),
                                signal_runtime_context=signal_runtime_context,
                                signal_runtime_mode=effective_settings.signal_runtime_mode,
                            )
                        else:
                            metadata = dict(event.get("metadata") or {})
                            if not metadata:
                                metadata = drive_runtime.client.get_file_metadata(file_id)
                            descriptor = drive_runtime.client.describe_item(metadata, folder_path="")
                            candidate = DriveIngestCandidate(**descriptor)
                            apply_classification(candidate)
                            processed = drive_runtime.process_candidate(
                                candidate,
                                observed_at=str(
                                    event.get("observed_at")
                                    or descriptor.get("modified_time")
                                    or result.heartbeat_at
                                ),
                                signal_runtime_context=signal_runtime_context,
                                signal_runtime_mode=effective_settings.signal_runtime_mode,
                            )
                    except BaseException as exc:
                        if _is_fatal_worker_exception(exc):
                            raise
                        _record_item_failure(
                            run_state,
                            stage="drive_process",
                            message_id=file_id,
                            exc=exc,
                            details={
                                "source_kind": "drive",
                                "source_id": str(event.get("change_id") or ""),
                                "file_id": file_id,
                                "removed": bool(event.get("removed")),
                            },
                        )
                        continue
                    try:
                        _record_drive_result(run_state, processed=processed)
                    except BaseException as exc:
                        if _is_fatal_worker_exception(exc):
                            raise
                        _record_item_failure(
                            run_state,
                            stage="drive_record_result",
                            message_id=file_id,
                            exc=exc,
                            details={
                                "source_kind": "drive",
                                "source_id": str(event.get("change_id") or ""),
                                "file_id": file_id,
                            },
                        )
                        continue
                    try:
                        summary = run_state.get("summary") if isinstance(run_state, dict) else None
                        if isinstance(summary, dict):
                            summary.setdefault("projection_failure_count", 0)
                            summary.setdefault("last_projection_error_summary", {})
                            summary.setdefault("consecutive_projection_failures", 0)
                            summary.setdefault("projection_circuit_open", False)
                            summary.setdefault("projection_skipped_count", 0)
                            summary.setdefault("projection_circuit_fingerprint", "")
                            summary.setdefault("projection_disabled_for_run_reason", "")
                            summary.setdefault("run_level_error_rollup", {})

                        if isinstance(summary, dict) and bool(summary.get("projection_circuit_open")):
                            summary["projection_skipped_count"] = int(summary.get("projection_skipped_count") or 0) + 1
                        else:
                            _apply_projection_refresh(
                                run_state=run_state,
                                processed=processed,
                            )
                            if isinstance(summary, dict):
                                summary["consecutive_projection_failures"] = 0
                    except BaseException as exc:
                        if _is_fatal_worker_exception(exc):
                            raise
                        # Projection can fail systemically even when message_key exists (Daszek offline, network, 5xx).
                        # We still record item-level context when available, but we open the circuit by fingerprint.
                        error_type, error_message = _summarize_exception(exc)
                        error_category = _classify_worker_error(exc)
                        fingerprint = _build_worker_error_fingerprint(
                            stage="projection_v2_push",
                            error_category=error_category,
                            error_type=error_type,
                        )
                        if isinstance(summary, dict):
                            _rollup_run_level_error(run_state, stage="projection_v2_push", exc=exc, timestamp=result.heartbeat_at)
                            summary["projection_failure_count"] = int(summary.get("projection_failure_count") or 0) + 1
                            summary["consecutive_projection_failures"] = int(summary.get("consecutive_projection_failures") or 0) + 1
                            summary["last_projection_error_summary"] = {
                                "stage": "projection_v2_push",
                                "error_category": error_category,
                                "fingerprint": fingerprint,
                                "error_type": error_type,
                                "error_message": error_message,
                                "timestamp": result.heartbeat_at,
                            }
                            rollup = summary.get("run_level_error_rollup")
                            fp_count = 0
                            if isinstance(rollup, dict):
                                fp_row = rollup.get(fingerprint)
                                if isinstance(fp_row, dict):
                                    fp_count = int(fp_row.get("count") or 0)
                            if fp_count >= _MAX_CONSECUTIVE_PROJECTION_FAILURES:
                                summary["projection_circuit_open"] = True
                                summary["projection_circuit_fingerprint"] = fingerprint
                                summary["projection_disabled_for_run_reason"] = error_category
                        message_key = ""
                        try:
                            message_key = _drive_message_key(processed)
                        except Exception:
                            message_key = ""
                        if message_key:
                            _record_item_failure(
                                run_state,
                                stage="projection_v2_push",
                                message_id=message_key,
                                exc=exc,
                                details={
                                    "source_kind": "projection",
                                    "source_id": str(event.get("change_id") or ""),
                                    "file_id": file_id,
                                    "message_id": message_key,
                                    "scope": "item",
                                    "error_category": error_category,
                                    "fingerprint": fingerprint,
                                },
                            )
                        else:
                            _record_run_level_failure(
                                run_state,
                                stage="projection_v2_push",
                                exc=exc,
                                details={
                                    "source_kind": "projection",
                                    "scope": "run",
                                    "error_category": error_category,
                                    "fingerprint": fingerprint,
                                },
                            )
                        # Continue to next Drive event even when projection push fails.
                        result.drive_processed_count += 1
                        continue
                    result.drive_processed_count += 1

            if loop_mode != "continuous_poll":
                break
            if max_iterations > 0 and result.iterations >= max_iterations:
                result.stop_reason = "max_iterations_reached"
                break
            _run_worker_idle_maintenance(
                run_state=run_state,
                settings=effective_settings,
                mailbox_runtime=mailbox_runtime,
                iteration=result.iterations,
            )
            # Sleep z możliwością przerwania przez SIGTERM (graceful shutdown)
            if _sleep_with_abort(_poll_sleep_seconds(effective_settings, scheduler=_scheduler)):
                result.stop_reason = "sigterm"
                break
    except (KeyboardInterrupt, SystemExit):
        result.stop_reason = "keyboard_interrupt"
    except BaseException as exc:  # noqa: BLE001 - envelope boundary
        if _is_fatal_worker_exception(exc):
            # Krok 1: ConfigError -> cooldown zamiast raise (petla restartu)
            if isinstance(exc, ConfigError):
                result.stop_reason = "config_error_cooldown"
                _record_run_level_failure(
                    run_state,
                    stage="worker_fatal",
                    exc=exc,
                    details={"scope": "run", "cooldown": True},
                )
                summary = run_state.get("summary") if isinstance(run_state, dict) else None
                if isinstance(summary, dict):
                    summary["last_worker_error"] = str(exc)[:500]
                    summary["worker_cooldown"] = True
                    summary["aborted"] = True
                logger.critical("WORKER_COOLDOWN: %s — worker czeka na reczna interwencje", exc)
            else:
                raise
        else:
            _record_run_level_failure(
                run_state,
                stage="worker_fatal",
                exc=exc,
                details={"scope": "run"},
            )
            summary = run_state.get("summary") if isinstance(run_state, dict) else None
            if isinstance(summary, dict):
                summary["aborted"] = True
            result.stop_reason = "fatal_exception"

    summary = run_state.get("summary") if isinstance(run_state, dict) else None
    if isinstance(summary, dict):
        result.failed_item_count = int(summary.get("items_failed") or 0)
        failed_items = summary.get("failed_items")
        result.last_errors = list(failed_items) if isinstance(failed_items, list) else []
        if result.stop_reason and not str(summary.get("stop_reason") or "").strip():
            summary["stop_reason"] = result.stop_reason
        if result.stop_reason == "keyboard_interrupt":
            summary["aborted"] = True

    return result


def replay_signal_from_journal(settings: Settings, *, signal_id: str) -> dict[str, Any]:
    runtime_context = _build_runtime_context(settings)
    result = replay_signal(signal_id, runtime_context=runtime_context)
    return result.to_dict()


def rebuild_case_from_signal_journal(settings: Settings, *, case_id: str, case_key_hint: str = "") -> dict[str, Any]:
    runtime_context = _build_runtime_context(settings)
    result = case_rebuild_from_journal(
        case_id=case_id,
        case_key_hint=case_key_hint,
        runtime_context=runtime_context,
    )
    return result.to_dict()


def _build_runtime_context(settings: Settings) -> SignalRuntimeContext:
    mailbox_runtime = _require_worker_mailbox_runtime(settings)
    mailbox_runtime.bootstrap()
    journal = SignalJournal(
        mailbox_runtime.store,
        jsonl_mirror_enabled=bool(settings.signal_journal_jsonl_mirror_enabled),
    )
    return SignalRuntimeContext(
        settings=settings,
        journal=journal,
        mailbox_memory_runtime=mailbox_runtime,
        graph_store=getattr(mailbox_runtime, "graph_store", None),
        model=settings.groq_model,
        verbose=False,
        mode=settings.signal_runtime_mode,
    )


def _require_worker_mailbox_runtime(settings: Settings):
    from mailbox_memory_runtime import build_mailbox_memory_runtime

    runtime = build_mailbox_memory_runtime(settings)
    if runtime is None:
        raise ConfigError(
            "Signal worker requires durable mailbox/shared memory storage. "
            "Configure MAILBOX_MEMORY_DATABASE_URL and MAILBOX_MEMORY_STAGE_MODE first. "
            "Next check: python tools/gmail_audit/gmail_intake.py doctor --skip-gmail --verbose"
        )
    return runtime


def _resolve_worker_settings(settings: Settings, *, dry_run: bool) -> Settings:
    if dry_run:
        return replace(settings, daszek_v2_push_enabled=False)
    return settings


def _poll_sleep_seconds(settings: Settings, scheduler: PredictiveScheduler | None = None) -> int:
    if scheduler:
        return scheduler.get_sleep_seconds()
    candidates = []
    if bool(settings.gmail_change_detection_enabled):
        candidates.append(int(settings.gmail_history_poll_interval_sec))
    if bool(settings.drive_change_detection_enabled):
        candidates.append(int(settings.drive_changes_poll_interval_sec))
    return max(1, min(candidates or [30]))


def _record_drive_result(*, run_state: dict[str, Any], processed: dict[str, Any]) -> None:
    signal_runtime_result = processed.get("signal_runtime_result")
    reconcile_result = getattr(signal_runtime_result, "reconcile_result", None)
    if reconcile_result is None:
        return
    triage_result = dict(processed.get("triage_result") or {})
    stage_outputs = dict(reconcile_result.stage_outputs or {})
    intake_result_final = dict(stage_outputs.get("intake_result_final") or {})
    duplicate = _is_duplicate_reconcile_result(reconcile_result)
    primary_action = str((stage_outputs.get("action_plan_result") or {}).get("primary_action") or "").strip()
    message_key = _drive_message_key(processed)
    run_state["summary"]["items_processed"] += 1
    if not duplicate:
        run_state["summary"]["items_valid"] += 1
    run_state["summary"]["processed_message_ids"].append(message_key)
    if primary_action:
        run_state["summary"]["decision_distribution"][primary_action] += 1
    append_jsonl(
        run_state["artifacts"]["execution_metadata"],
        {
            "message_id": message_key,
            "signal_runtime_mode": run_state["manifest"].get("signal_runtime_mode"),
            "signal_id": reconcile_result.signal_id,
            "signal_kind": reconcile_result.signal_kind,
            "source_kind": reconcile_result.source_kind,
            "processing_state": reconcile_result.processing_state,
        },
    )
    if triage_result:
        append_jsonl(
            run_state["artifacts"]["triage_results"],
            {
                "message_id": message_key,
                "observation_id": str(triage_result.get("observation_id") or ""),
                "source_kind": str(triage_result.get("source_kind") or "drive"),
                "triage_class": triage_result.get("triage_class"),
                "routing_decision": triage_result.get("routing_decision"),
                "reasoning_budget": triage_result.get("reasoning_budget") or {},
                "batching": triage_result.get("batching") or {},
                "preclassification": triage_result.get("preclassification") or {},
            },
        )
    stage_record = {
        "message_id": message_key,
        "signal_id": reconcile_result.signal_id,
        "signal_kind": reconcile_result.signal_kind,
        "source_kind": reconcile_result.source_kind,
        "processing_state": reconcile_result.processing_state,
        "duplicate_suppressed": duplicate,
        "triage_result": triage_result,
        "entity_link_result": getattr(reconcile_result, "entity_link", None) or {},
        "intake_result_final": intake_result_final,
        "case_link_result": stage_outputs.get("case_link_result") or {},
        "business_reasoning_result": stage_outputs.get("business_reasoning_result") or {},
        "reply_draft_result": stage_outputs.get("reply_draft_result") or {},
        "action_plan_result": stage_outputs.get("action_plan_result") or {},
        "case_intelligence_result": stage_outputs.get("case_intelligence_result") or {},
        "mailbox_memory_result": stage_outputs.get("mailbox_memory_result") or {},
    }
    if isinstance(reconcile_result.v2_projection, dict) and reconcile_result.v2_projection:
        stage_record.update(reconcile_result.v2_projection)
    append_jsonl(run_state["stage_records_path"], stage_record)


def _resolve_run_settings(run_state: dict[str, Any]) -> Settings | None:
    runtime = run_state.get("mailbox_memory_runtime")
    if runtime is not None:
        settings = getattr(runtime, "settings", None)
        if settings is not None:
            return settings
    return None


def _run_worker_idle_maintenance(
    *,
    run_state: dict[str, Any],
    settings: Settings,
    mailbox_runtime: Any,
    iteration: int,
) -> None:
    """Heartbeat feed push + periodic remote bridge drain when worker poll loop is idle."""

    try:
        from daszek_v3_feed_runtime import maybe_heartbeat_operational_feed

        maybe_heartbeat_operational_feed(run_state=run_state, settings=settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("worker_idle_feed_heartbeat_failed: %s", exc)
    _maybe_run_sla_watcher_tick(run_state=run_state, settings=settings)
    _maybe_run_follow_up_guardian_tick(run_state=run_state, settings=settings)
    bridge_interval = max(1, int(getattr(settings, "daszek_bridge_drain_interval_iterations", 5) or 5))
    if iteration % bridge_interval != 0:
        return
    try:
        from daszek_bridge_queue_drain import maybe_worker_bridge_drain_tick

        maybe_worker_bridge_drain_tick(
            run_state=run_state,
            settings=settings,
            runtime=mailbox_runtime,
            max_items=int(getattr(settings, "daszek_bridge_drain_max_items", 10) or 10),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("worker_idle_bridge_drain_failed: %s", exc)


def _maybe_run_sla_watcher_tick(*, run_state: dict[str, Any], settings: Settings) -> None:
    summary = run_state.setdefault("summary", {})
    now_mono = time.monotonic()
    last_mono = float(summary.get("last_sla_watcher_monotonic") or 0.0)
    if last_mono and (now_mono - last_mono) < _SLA_WATCHER_INTERVAL_SEC:
        return
    summary["last_sla_watcher_monotonic"] = now_mono
    try:
        from sla_watcher import sla_watcher_oneshot

        result = sla_watcher_oneshot(settings)
        summary["sla_watcher_tick_count"] = int(summary.get("sla_watcher_tick_count") or 0) + 1
        violations = result.get("violations") if isinstance(result, dict) else {}
        summary["last_sla_watcher_result"] = {
            "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
            "checked_at": str((violations or {}).get("checked_at") or ""),
            "total_pending": int((violations or {}).get("total_pending") or 0),
            "escalated": int(result.get("escalated") or 0) if isinstance(result, dict) else 0,
        }
        if not bool(result.get("ok")):
            summary["sla_watcher_error_count"] = int(summary.get("sla_watcher_error_count") or 0) + 1
            summary["last_sla_watcher_error"] = str(result.get("error") or "unknown")[:500]
            logger.warning("worker_idle_sla_watcher_failed: %s", summary["last_sla_watcher_error"])
    except Exception as exc:  # noqa: BLE001
        summary["sla_watcher_error_count"] = int(summary.get("sla_watcher_error_count") or 0) + 1
        summary["last_sla_watcher_error"] = str(exc)[:500]
        logger.warning("worker_idle_sla_watcher_failed: %s", exc)


def _maybe_run_follow_up_guardian_tick(*, run_state: dict[str, Any], settings: Settings) -> None:
    """Roadmap 3.1 -- sibling tick to `_maybe_run_sla_watcher_tick`, same throttle pattern.

    Turns silent, SLA-breached cases into a new operator-facing proposal (see
    `follow_up_guardian.py`), the temporal signal `sla_watcher` itself deliberately does not
    produce (it only ages already-existing decisions)."""
    summary = run_state.setdefault("summary", {})
    now_mono = time.monotonic()
    last_mono = float(summary.get("last_follow_up_guardian_monotonic") or 0.0)
    if last_mono and (now_mono - last_mono) < _FOLLOW_UP_GUARDIAN_INTERVAL_SEC:
        return
    summary["last_follow_up_guardian_monotonic"] = now_mono
    try:
        from follow_up_guardian import follow_up_guardian_oneshot

        result = follow_up_guardian_oneshot(settings)
        summary["follow_up_guardian_tick_count"] = (
            int(summary.get("follow_up_guardian_tick_count") or 0) + 1
        )
        summary["last_follow_up_guardian_result"] = {
            "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
            "checked": int(result.get("checked") or 0) if isinstance(result, dict) else 0,
            "proposed_count": int(result.get("proposed_count") or 0) if isinstance(result, dict) else 0,
        }
        if not bool(result.get("ok")):
            summary["follow_up_guardian_error_count"] = (
                int(summary.get("follow_up_guardian_error_count") or 0) + 1
            )
            summary["last_follow_up_guardian_error"] = str(result.get("error") or "unknown")[:500]
            logger.warning(
                "worker_idle_follow_up_guardian_failed: %s", summary["last_follow_up_guardian_error"]
            )
    except Exception as exc:  # noqa: BLE001
        summary["follow_up_guardian_error_count"] = (
            int(summary.get("follow_up_guardian_error_count") or 0) + 1
        )
        summary["last_follow_up_guardian_error"] = str(exc)[:500]
        logger.warning("worker_idle_follow_up_guardian_failed: %s", exc)


def _apply_projection_refresh(*, run_state: dict[str, Any], processed: dict[str, Any]) -> None:
    from daszek_v3_feed_runtime import maybe_push_operational_feed_after_reconcile
    from gmail_intake import daszek_legacy_v2_push_allowed

    signal_runtime_result = processed.get("signal_runtime_result")
    reconcile_result = getattr(signal_runtime_result, "reconcile_result", None)
    if reconcile_result is None:
        return
    if _is_duplicate_reconcile_result(reconcile_result):
        return
    projection_decision = getattr(reconcile_result, "projection_refresh_decision", None)
    if projection_decision is not None and not bool(getattr(projection_decision, "should_refresh", False)):
        return

    message_key = _drive_message_key(processed)
    stage_outputs = dict(reconcile_result.stage_outputs or {})
    settings = _resolve_run_settings(run_state)
    if (
        settings is not None
        and daszek_legacy_v2_push_allowed(settings, run_state)
        and isinstance(reconcile_result.v2_projection, dict)
        and reconcile_result.v2_projection
    ):
        from v2_runtime import push_v2_projection_to_daszek

        push_v2_projection_to_daszek(
            run_state=run_state,
            message_id=message_key,
            v2_projection=reconcile_result.v2_projection,
            case_intelligence_result=stage_outputs.get("case_intelligence_result"),
            event_log=None,
            action_plan_result=stage_outputs.get("action_plan_result"),
            intake_result_final=stage_outputs.get("intake_result_final"),
        )

    if settings is not None:
        from event_spine.gmail_telemetry import publish_gmail_reconcile_completed

        publish_gmail_reconcile_completed(
            settings,
            reconcile_result,
            trigger_message_id=message_key,
        )
        maybe_push_operational_feed_after_reconcile(
            run_state=run_state,
            settings=settings,
            reconcile_result=reconcile_result,
            trigger_message_id=message_key,
        )


def _drive_message_key(processed: dict[str, Any]) -> str:
    signal_runtime_result = processed.get("signal_runtime_result")
    reconcile_result = getattr(signal_runtime_result, "reconcile_result", None)
    signal_id = str(getattr(reconcile_result, "signal_id", "") or "").strip()
    if reconcile_result is not None and signal_id:
        return signal_id
    document_row = dict(processed.get("document_row") or {})
    return str(document_row.get("drive_item_id") or document_row.get("document_id") or "drive_signal")


def _is_duplicate_reconcile_result(reconcile_result: Any) -> bool:
    return str(getattr(reconcile_result, "processing_state", "") or "").strip() == "skipped_duplicate"


__all__ = [
    "SignalWorkerLoopResult",
    "rebuild_case_from_signal_journal",
    "replay_signal_from_journal",
    "run_signal_loop",
]
