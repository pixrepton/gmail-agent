"""Drain Daszek v2 `bridge_queue.jsonl` pending adjudication items via the Python bridge."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from config import Settings

SCHEMA_VERSION = "daszek_bridge_queue.v1"
_BRIDGE_TERMINAL_STATUSES = frozenset({"completed", "failed", "skipped", "dead_letter"})
_BRIDGE_ACTIONABLE_STATUSES = frozenset({"pending", "retry"})
_BRIDGE_RETRYABLE_TOKENS = (
    "timeout",
    "timed out",
    "connection",
    "network",
    "temporary failure",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "too many requests",
    "429",
    "502",
    "503",
    "504",
)
_BRIDGE_RETRY_BASE_DELAY_SEC = 30
_BRIDGE_RETRY_MAX_ATTEMPTS = 3


def filter_bridge_rows_by_domain(rows: list[dict[str, Any]], domain_filter: str | None) -> list[dict[str, Any]]:
    """When draining for Gate B / operator truth-loop, skip unrelated domains (e.g. action_decision)."""
    norm = str(domain_filter or "").strip().lower()
    if norm in {"", "any"}:
        return list(rows)
    return [r for r in rows if isinstance(r, dict) and str(r.get("domain") or "").strip().lower() == norm]


def format_bridge_error(
    *,
    error_type: str,
    error_message: str,
    stage: str,
    queue_id: str = "",
    source_signal_ids: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Bounded JSON string for Daszek ``bridge_error`` (no secrets / mail bodies)."""
    payload: dict[str, Any] = {
        "error_type": error_type[:120],
        "error_message": (error_message or "")[:2000],
        "stage": stage[:120],
        "queue_id": (queue_id or "")[:200],
    }
    if source_signal_ids:
        payload["source_signal_ids"] = [str(s)[:128] for s in source_signal_ids[:24]]
    if isinstance(extra, dict):
        for key, value in extra.items():
            if key in payload or value in (None, ""):
                continue
            payload[str(key)[:120]] = value
    raw = json.dumps(payload, ensure_ascii=False)
    return raw[:4000]


def _source_signal_ids_from_row(row: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for value in [row.get("source_signal_id"), *(row.get("source_signal_ids") if isinstance(row.get("source_signal_ids"), list) else [])]:
        sid = str(value or "").strip()
        if sid and sid not in ids:
            ids.append(sid)
    return ids


def _source_message_id_from_row(row: dict[str, Any]) -> str:
    for key in ("source_message_id", "message_id", "gmail_message_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _signal_source_message_id(signal: Any) -> str:
    source_ref = getattr(signal, "source_ref", None)
    if isinstance(source_ref, dict):
        value = str(source_ref.get("message_id") or source_ref.get("gmail_message_id") or "").strip()
        if value:
            return value
    payload = getattr(signal, "payload", None)
    if isinstance(payload, dict):
        for path in (
            ("snapshot", "source_message", "message_id"),
            ("source_message", "message_id"),
            ("message", "id"),
            ("gmail", "message_id"),
        ):
            current: Any = payload
            for key in path:
                current = current.get(key) if isinstance(current, dict) else None
            value = str(current or "").strip()
            if value:
                return value
    return ""


def _message_id_from_note_detail(detail: dict[str, Any]) -> str:
    note = detail.get("note") if isinstance(detail, dict) else {}
    if isinstance(note, dict):
        value = str(note.get("source_message_id") or note.get("message_id") or "").strip()
        if value:
            return value
    signals = detail.get("signals") if isinstance(detail, dict) else []
    if isinstance(signals, list):
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            for key in ("source_message_id", "message_id", "gmail_message_id"):
                value = str(signal.get(key) or "").strip()
                if value:
                    return value
            source_ref = signal.get("source_ref")
            if isinstance(source_ref, dict):
                value = str(source_ref.get("message_id") or source_ref.get("gmail_message_id") or "").strip()
                if value:
                    return value
    return ""


def _bridge_signal_rank(signal: Any) -> int:
    kind = str(getattr(signal, "signal_kind", "") or "").strip()
    order = {
        "gmail_message_observed": 0,
        "gmail_thread_update_observed": 1,
        "gmail_attachment_observed": 2,
    }
    return order.get(kind, 99)


def resolve_bridge_signal_id(row: dict[str, Any], journal: Any) -> tuple[str, str]:
    """Resolve bridge_queue source signal to a canonical signal journal id.

    Older Daszek notes may carry v2 shadow ids. If the remote note detail exposes
    source_message_id, Node B can recover the canonical Gmail signal without
    mutating Daszek or weakening the adjudication contract.
    """
    for sid in _source_signal_ids_from_row(row):
        try:
            if journal.fetch_signal(sid):
                return sid, "direct"
        except Exception:  # noqa: BLE001
            continue

    message_id = _source_message_id_from_row(row)
    if not message_id:
        return "", "missing_source_message_id"
    try:
        candidates = journal.fetch_signals_for_source("gmail", limit=500)
    except Exception:  # noqa: BLE001
        return "", "journal_lookup_failed"
    matches = [signal for signal in candidates if _signal_source_message_id(signal) == message_id]
    if not matches:
        return "", "source_signal_id_not_in_journal"
    matches.sort(key=_bridge_signal_rank)
    resolved = str(getattr(matches[0], "signal_id", "") or "").strip()
    return (resolved, "message_id_fallback") if resolved else ("", "source_signal_id_not_in_journal")


@dataclass(frozen=True, slots=True)
class BridgeQueuePaths:
    queue_path: Path


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def _parse_bridge_error_payload(error: str) -> dict[str, Any]:
    try:
        data = json.loads(str(error or ""))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _status_retry_due(row: dict[str, Any]) -> bool:
    next_retry_at = str(row.get("next_retry_at") or "").strip()
    if not next_retry_at:
        return True
    try:
        due_ts = __import__("datetime").datetime.fromisoformat(next_retry_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return True
    return due_ts <= __import__("time").time()


def _merge_bridge_status(base_row: dict[str, Any], status_row: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base_row)
    merged["bridge_status"] = str(status_row.get("bridge_status") or merged.get("bridge_status") or "pending").strip().lower()
    for key in ("bridge_error", "retry_count", "next_retry_at", "retryable"):
        value = status_row.get(key)
        if value not in (None, ""):
            merged[key] = value
    return merged


def _actionable_bridge_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    creation_rows: list[dict[str, Any]] = []
    latest_by_queue: dict[str, dict[str, Any]] = {}
    seen_creations: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        queue_id = str(row.get("queue_id") or "").strip()
        if not queue_id:
            continue
        status = str(row.get("bridge_status") or "pending").strip().lower()
        if str(row.get("schema_version") or "") != SCHEMA_VERSION:
            continue
        domain = str(row.get("domain") or "")
        if domain in {"adjudication", "action_decision", "agent_hitl"} and queue_id not in seen_creations:
            if domain == "adjudication":
                kind = str(row.get("adjudication_kind") or "").strip()
                if kind != "reject_same_case":
                    continue
            if domain == "agent_hitl":
                kind = str(row.get("adjudication_kind") or "").strip()
                if kind != "hitl_action_execute":
                    continue
            creation_rows.append(row)
            seen_creations.add(queue_id)
        latest_by_queue[queue_id] = row if status else latest_by_queue.get(queue_id, row)

    actionable: list[dict[str, Any]] = []
    for base_row in creation_rows:
        queue_id = str(base_row.get("queue_id") or "").strip()
        latest = latest_by_queue.get(queue_id) or base_row
        status = str(latest.get("bridge_status") or "pending").strip().lower()
        if status in _BRIDGE_TERMINAL_STATUSES:
            continue
        if status not in _BRIDGE_ACTIONABLE_STATUSES:
            continue
        if status == "retry" and not _status_retry_due(latest):
            continue
        actionable.append(_merge_bridge_status(base_row, latest))
    return actionable


def load_completion_ids(path: Path) -> set[str]:
    done: set[str] = set()
    for row in iter_jsonl(path):
        qid = str(row.get("queue_id") or "").strip()
        if not qid:
            continue
        st = str(row.get("bridge_status") or "").strip().lower()
        if st in _BRIDGE_TERMINAL_STATUSES:
            done.add(qid)
    return done


def pending_bridge_rows(path: Path) -> list[dict[str, Any]]:
    """Return actionable creation rows with latest retry metadata merged in."""
    return _actionable_bridge_rows(list(iter_jsonl(path)))


def pending_adjudication_rows(path: Path) -> list[dict[str, Any]]:
    return [row for row in pending_bridge_rows(path) if str(row.get("domain") or "") == "adjudication"]


def operator_payload_from_row(row: dict[str, Any]) -> dict[str, Any]:
    case_id = str(row.get("case_id") or "").strip()
    signal_id = str(row.get("source_signal_id") or "").strip()
    return {
        "event_domain": "adjudication",
        "adjudication_kind": "reject_same_case",
        "case_id": case_id,
        "detail": "Daszek bridge_queue drain",
        "target_refs": {"signal_id": signal_id, "rejected_case_id": case_id},
        "source_surface": "daszek_bridge_queue",
    }


def action_decision_payload_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": str(row.get("proposal_id") or "").strip(),
        "decision": str(row.get("decision") or "").strip(),
        "actor_id": str(row.get("actor_id") or "").strip(),
        "reason": str(row.get("reason") or "").strip(),
        "decision_key": str(row.get("queue_id") or "").strip(),
    }


def bridge_payload_from_row(row: dict[str, Any]) -> dict[str, Any]:
    domain = str(row.get("domain") or "")
    if domain == "action_decision":
        return action_decision_payload_from_row(row)
    if domain == "agent_hitl":
        from agent_hitl_bridge import agent_hitl_payload_from_row

        return agent_hitl_payload_from_row(row)
    return operator_payload_from_row(row)


def append_bridge_completion(path: Path, *, queue_id: str, status: str, error: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    error_payload = _parse_bridge_error_payload(error)
    row = {
        "queue_id": queue_id,
        "schema_version": SCHEMA_VERSION,
        "bridge_status": status,
        "bridge_error": error[:4000] if error else "",
        "bridge_completed_at": __import__("datetime").datetime.now().astimezone().isoformat(),
    }
    if "retry_count" in error_payload:
        try:
            row["retry_count"] = max(0, int(error_payload["retry_count"]))
        except (TypeError, ValueError):
            pass
    next_retry_at = str(error_payload.get("next_retry_at") or "").strip()
    if next_retry_at:
        row["next_retry_at"] = next_retry_at[:64]
    if "retryable" in error_payload:
        row["retryable"] = bool(error_payload["retryable"])
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _append_completion_result(
    append_completion: Any,
    *,
    queue_id: str,
    status: str,
    error: str = "",
) -> tuple[bool, str]:
    try:
        append_completion(queue_id, status, error)
        return True, ""
    except Exception as exc:  # noqa: BLE001 - completion channel must not stop bounded drain
        return False, str(exc)


def _is_retryable_bridge_error(exc: BaseException) -> bool:
    message = str(exc or "").lower()
    return any(token in message for token in _BRIDGE_RETRYABLE_TOKENS)


def _bridge_retry_error(
    *,
    exc: BaseException,
    queue_id: str,
    row: dict[str, Any],
    attempt: int,
) -> str:
    delay = min(5 * 60, _BRIDGE_RETRY_BASE_DELAY_SEC * (2 ** max(0, attempt - 1)))
    next_retry_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc) + __import__("datetime").timedelta(seconds=delay)
    return format_bridge_error(
        error_type=type(exc).__name__,
        error_message=str(exc),
        stage="process_item",
        queue_id=queue_id,
        source_signal_ids=_source_signal_ids_from_row(row) or None,
        extra={
            "retryable": True,
            "retry_count": attempt,
            "next_retry_at": next_retry_at.isoformat(),
        },
    )


def validate_reject_same_case_bridge_result(out: Any) -> None:
    """A reject_same_case bridge row is complete only after truth-loop reconcile."""
    if not isinstance(out, dict):
        raise ValueError("reject_same_case bridge output must be an object")
    if out.get("truth_loop_executed") is not True:
        raise ValueError("reject_same_case truth_loop_executed was not true")
    if out.get("reconcile_signal_ran") is not True:
        raise ValueError("reject_same_case reconcile_signal_ran was not true")
    summary = out.get("reconcile_summary")
    processing_state = ""
    if isinstance(summary, dict):
        processing_state = str(summary.get("processing_state") or "").strip()
    if processing_state != "reconciled":
        state_label = processing_state or "missing"
        raise ValueError(f"reject_same_case reconcile processing_state was not reconciled: {state_label}")


def run_daszek_bridge_drain(args: argparse.Namespace) -> int:
    use_remote = bool(getattr(args, "remote", False) or not str(getattr(args, "queue_path", "") or "").strip())
    if args.dry_run:
        try:
            pending = load_pending_bridge_rows_for_args(args, use_remote=use_remote)
        except Exception as exc:  # noqa: BLE001
            print(
                json.dumps(
                    {"ok": False, "dry_run": True, "stage": "fetch_queue", "error": str(exc), "error_class": type(exc).__name__},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        out = [
            {
                "queue_id": r.get("queue_id"),
                "domain": r.get("domain"),
                "payload": bridge_payload_from_row(r),
            }
            for r in pending
        ]
        print(json.dumps({"ok": True, "dry_run": True, "source": "remote" if use_remote else "file", "items": out}, ensure_ascii=False, indent=2))
        return 0

    from config import ConfigError, load_settings
    from mailbox_memory_runtime import build_mailbox_memory_runtime
    from signal_journal import SignalJournal
    from signal_reconciler import SignalRuntimeContext
    from adjudication_executioner import bridge_operator_feedback

    settings = load_settings(require_groq=False, require_google=False)
    runtime = build_mailbox_memory_runtime(settings)
    if runtime is None:
        raise ConfigError(
            "Mailbox memory is disabled or missing MAILBOX_MEMORY_DATABASE_URL. "
            "Set MAILBOX_MEMORY_STAGE_MODE=shadow|live and configure Postgres first."
        )
    runtime.bootstrap()
    journal = SignalJournal(
        runtime.store,
        jsonl_mirror_enabled=bool(getattr(settings, "signal_journal_jsonl_mirror_enabled", False)),
    )
    from gmail_intake import attach_daszek_v2_manifest_from_settings, init_run_state

    run_state = init_run_state(
        run_id=str(args.run_id or "daszek-bridge-drain"),
        run_dir=Path("tools/gmail_audit/runs") / str(args.run_id or "daszek-bridge-drain"),
        command="daszek-bridge-drain",
        selector={"type": "daszek_bridge_drain"},
        mailbox="signal-runtime",
        model=getattr(settings, "groq_model", ""),
        schema_path=None,
        source_run=None,
        push_daszek=False,
        runtime_controls={},
    )
    attach_daszek_v2_manifest_from_settings(run_state, settings)
    run_state["mailbox_memory_runtime"] = runtime
    if getattr(settings, "daszek_operational_feed_auto_push_enabled", False):
        from gmail_intake import attach_daszek_client

        attach_daszek_client(run_state, settings)
    ctx = SignalRuntimeContext(
        settings=settings,
        journal=journal,
        mailbox_memory_runtime=runtime,
        graph_store=getattr(runtime, "graph_store", None),
        run_state=run_state,
        model=getattr(settings, "groq_model", None),
        verbose=False,
        mode=str(getattr(settings, "signal_runtime_mode", "active") or "active"),
        persist_entity_links=True,
    )
    if use_remote:
        from daszek_client import DaszekClient

        client = DaszekClient(settings)
        try:
            rows = fetch_remote_pending_bridge_rows(
                client,
                max_items=int(args.max_items),
                domain_filter=str(getattr(args, "domain", "") or "") or None,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                json.dumps(
                    {
                        "ok": False,
                        "source": "remote",
                        "stage": "fetch_queue",
                        "error": str(exc),
                        "error_class": type(exc).__name__,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1

        def complete_remote(queue_id: str, status: str, error: str = "") -> None:
            client.complete_v2_bridge_queue_item(queue_id, status=status, error=error)

        results = drain_bridge_rows(
            pending=rows,
            append_completion=complete_remote,
            bridge_operator_feedback=bridge_operator_feedback,
            store=runtime.store,
            journal=journal,
            runtime_context=ctx,
            max_items=int(args.max_items),
            dry_run=False,
        )
        _accumulate_bridge_reconcile_overlays(run_state, results)
        _maybe_push_feed_after_bridge_drain(run_state, settings)
        print(json.dumps({"ok": True, "source": "remote", "results": results}, ensure_ascii=False, indent=2))
    else:
        path = Path(str(args.queue_path))
        results = drain_bridge_queue(
            queue_path=path,
            bridge_operator_feedback=bridge_operator_feedback,
            store=runtime.store,
            journal=journal,
            runtime_context=ctx,
            max_items=int(args.max_items),
            dry_run=False,
        )
        _accumulate_bridge_reconcile_overlays(run_state, results)
        _maybe_push_feed_after_bridge_drain(run_state, settings)
        print(json.dumps({"ok": True, "source": "file", "results": results}, ensure_ascii=False, indent=2))
    failures = [r for r in results if r.get("ok") is False]
    return 1 if failures else 0


def _reconcile_result_from_bridge_row(row: dict[str, Any]) -> Any:
    from types import SimpleNamespace

    out = row.get("bridge_out") if isinstance(row.get("bridge_out"), dict) else {}
    summary = out.get("reconcile_summary") if isinstance(out.get("reconcile_summary"), dict) else {}
    stage_outputs = summary.get("stage_outputs") if isinstance(summary.get("stage_outputs"), dict) else {}
    if not stage_outputs and isinstance(summary, dict) and summary.get("operator_projection_snapshot"):
        stage_outputs = {"operator_projection_snapshot": summary.get("operator_projection_snapshot")}
    projection_refresh = summary.get("projection_refresh_decision")
    if projection_refresh is None:
        projection_refresh = SimpleNamespace(should_refresh=True)
    elif isinstance(projection_refresh, dict):
        projection_refresh = SimpleNamespace(should_refresh=bool(projection_refresh.get("should_refresh")))
    return SimpleNamespace(
        case_id=str(summary.get("case_id") or ""),
        processing_state=str(summary.get("processing_state") or "reconciled"),
        projection_refresh_decision=projection_refresh,
        stage_outputs=stage_outputs,
    )


def _accumulate_bridge_reconcile_overlays(run_state: dict[str, Any], results: list[dict[str, Any]]) -> None:
    from daszek_v3_feed_runtime import accumulate_projection_route_overlay

    for row in results:
        if not isinstance(row, dict) or row.get("ok") is not True:
            continue
        reconcile_like = _reconcile_result_from_bridge_row(row)
        accumulate_projection_route_overlay(run_state, reconcile_like)


def _maybe_push_feed_after_bridge_drain(run_state: dict[str, Any], settings: Settings) -> None:
    from daszek_v3_feed_runtime import maybe_push_operational_feed_from_run_state

    maybe_push_operational_feed_from_run_state(
        run_state=run_state,
        settings=settings,
        trigger_message_id="daszek-bridge-drain",
    )


def maybe_worker_bridge_drain_tick(
    *,
    run_state: dict[str, Any],
    settings: Settings,
    runtime: Any,
    max_items: int = 10,
) -> list[dict[str, Any]]:
    """Drain remote bridge queue from worker loop (best-effort, non-fatal)."""

    client = run_state.get("daszek_client")
    if client is None:
        return []
    try:
        from adjudication_executioner import bridge_operator_feedback
        from signal_journal import SignalJournal
        from signal_reconciler import SignalRuntimeContext

        journal = SignalJournal(
            runtime.store,
            jsonl_mirror_enabled=bool(getattr(settings, "signal_journal_jsonl_mirror_enabled", False)),
        )
        ctx = SignalRuntimeContext(
            settings=settings,
            journal=journal,
            mailbox_memory_runtime=runtime,
            graph_store=getattr(runtime, "graph_store", None),
            run_state=run_state,
            model=getattr(settings, "groq_model", None),
            verbose=False,
            mode=str(getattr(settings, "signal_runtime_mode", "active") or "active"),
            persist_entity_links=True,
        )
        rows = fetch_remote_pending_bridge_rows(client, max_items=max(1, int(max_items)))

        def complete_remote(queue_id: str, status: str, error: str = "") -> None:
            client.complete_v2_bridge_queue_item(queue_id, status=status, error=error)

        results = drain_bridge_rows(
            pending=rows,
            append_completion=complete_remote,
            bridge_operator_feedback=bridge_operator_feedback,
            store=runtime.store,
            journal=journal,
            runtime_context=ctx,
            max_items=max(1, int(max_items)),
            dry_run=False,
        )
        _accumulate_bridge_reconcile_overlays(run_state, results)
        _maybe_push_feed_after_bridge_drain(run_state, settings)
        summary = run_state.setdefault("summary", {})
        summary["bridge_drain_tick_count"] = int(summary.get("bridge_drain_tick_count") or 0) + 1
        summary["last_bridge_drain_processed"] = len(results)
        return results
    except Exception as exc:  # noqa: BLE001
        summary = run_state.setdefault("summary", {})
        summary["bridge_drain_tick_errors"] = int(summary.get("bridge_drain_tick_errors") or 0) + 1
        summary["last_bridge_drain_error"] = str(exc)[:500]
        return []


def load_pending_bridge_rows_for_args(args: argparse.Namespace, *, use_remote: bool) -> list[dict[str, Any]]:
    domain_filter = str(getattr(args, "domain", "") or "") or None
    if use_remote:
        from config import load_settings
        from daszek_client import DaszekClient

        settings = load_settings(require_groq=False, require_google=False)
        client = DaszekClient(settings)
        return fetch_remote_pending_bridge_rows(
            client,
            max_items=int(args.max_items),
            domain_filter=domain_filter,
        )
    rows = pending_bridge_rows(Path(str(args.queue_path)))
    rows = filter_bridge_rows_by_domain(rows, domain_filter)
    return rows[: int(args.max_items)]


def fetch_remote_pending_bridge_rows(
    client: Any,
    *,
    max_items: int,
    domain_filter: str | None = None,
) -> list[dict[str, Any]]:
    fetch_limit = max(1, int(max_items))
    norm_domain = str(domain_filter or "").strip().lower()
    if norm_domain not in {"", "any"}:
        # Pending queue is FIFO; fetch a wider window so the first matching-domain rows appear after filtering.
        fetch_limit = min(100, max(fetch_limit * 25, 50))

    data = client.get_v2_bridge_queue(limit=fetch_limit, status="pending")
    rows = data.get("items") if isinstance(data, dict) else []
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        enriched = dict(row)
        note_id = str(enriched.get("desk_note_id") or "").strip()
        if note_id and (not _source_message_id_from_row(enriched) or not enriched.get("source_signal_ids")):
            try:
                detail = client.get_v2_note_detail(note_id)
            except Exception:  # noqa: BLE001
                try:
                    login = getattr(client, "login", None)
                    if callable(login):
                        login()
                        detail = client.get_v2_note_detail(note_id)
                    else:
                        detail = {}
                except Exception:  # noqa: BLE001
                    detail = {}
            note = detail.get("note") if isinstance(detail, dict) else {}
            if isinstance(note, dict):
                source_message_id = _message_id_from_note_detail(detail)
                if source_message_id and not _source_message_id_from_row(enriched):
                    enriched["source_message_id"] = source_message_id
                source_signal_ids = note.get("source_signal_ids")
                if isinstance(source_signal_ids, list) and not enriched.get("source_signal_ids"):
                    enriched["source_signal_ids"] = [str(s).strip() for s in source_signal_ids if str(s or "").strip()]
                note_case_id = str(note.get("case_id") or "").strip()
                if note_case_id and not str(enriched.get("case_id") or "").strip():
                    enriched["case_id"] = note_case_id
                enriched["note_detail_ids_enriched"] = bool(source_message_id or source_signal_ids)
        out.append(enriched)
        if len(out) >= fetch_limit:
            break

    filtered = filter_bridge_rows_by_domain(out, domain_filter)
    return filtered[: max(0, int(max_items))]


def drain_bridge_queue(
    *,
    queue_path: Path,
    bridge_operator_feedback: Any,
    store: Any,
    journal: Any,
    runtime_context: Any,
    max_items: int,
    dry_run: bool,
) -> list[dict[str, Any]]:
    return drain_bridge_rows(
        pending=pending_bridge_rows(queue_path),
        append_completion=lambda queue_id, status, error="": append_bridge_completion(
            queue_path,
            queue_id=queue_id,
            status=status,
            error=error,
        ),
        bridge_operator_feedback=bridge_operator_feedback,
        store=store,
        journal=journal,
        runtime_context=runtime_context,
        max_items=max_items,
        dry_run=dry_run,
    )


def drain_bridge_rows(
    *,
    pending: list[dict[str, Any]],
    append_completion: Any,
    bridge_operator_feedback: Any,
    store: Any,
    journal: Any,
    runtime_context: Any,
    max_items: int,
    dry_run: bool,
) -> list[dict[str, Any]]:
    pending = list(pending or [])[: max(0, int(max_items))]
    results: list[dict[str, Any]] = []
    for row in pending:
        qid = str(row.get("queue_id") or "").strip()
        domain = str(row.get("domain") or "")
        payload = bridge_payload_from_row(row)
        retry_count = max(0, int(row.get("retry_count") or 0))
        if dry_run:
            results.append({"queue_id": qid, "dry_run": True, "would_payload": payload})
            continue
        if not qid:
            ber = format_bridge_error(
                error_type="validation",
                error_message="bridge row missing queue_id",
                stage="validate_row",
                queue_id="",
                source_signal_ids=_source_signal_ids_from_row(row) or None,
            )
            results.append({"queue_id": "", "ok": False, "error": "missing_queue_id", "bridge_error": ber})
            continue
        try:
            if domain == "action_decision":
                from execution_runtime import approve_action_proposal, reject_action_proposal

                if payload["decision"] == "approve":
                    out = approve_action_proposal(
                        store,
                        payload["proposal_id"],
                        approved_by=payload["actor_id"],
                        reason=payload["reason"],
                    ).to_dict()
                elif payload["decision"] == "reject":
                    out = reject_action_proposal(
                        store,
                        payload["proposal_id"],
                        rejected_by=payload["actor_id"],
                        reason=payload["reason"],
                        decision_key=payload["decision_key"],
                    ).to_dict()
                else:
                    raise ValueError(f"unsupported action decision: {payload['decision']}")
            elif domain == "agent_hitl":
                from agent_hitl_bridge import execute_hitl_send_from_bridge_row

                settings = getattr(runtime_context, "settings", None)
                if settings is None:
                    from config import load_settings

                    settings = load_settings(require_groq=False, require_google=False)
                out = execute_hitl_send_from_bridge_row(row=row, settings=settings)
            else:
                resolved_signal_id, resolution = resolve_bridge_signal_id(row, journal)
                target_refs = payload.setdefault("target_refs", {})
                if isinstance(target_refs, dict):
                    original_signal_id = str(target_refs.get("signal_id") or "").strip()
                    if original_signal_id and resolved_signal_id and resolved_signal_id != original_signal_id:
                        target_refs["original_source_signal_id"] = original_signal_id
                    target_refs["signal_id"] = resolved_signal_id
                    target_refs["signal_resolution"] = resolution
                    source_message_id = _source_message_id_from_row(row)
                    if source_message_id:
                        target_refs["source_message_id"] = source_message_id
                if not resolved_signal_id:
                    raise ValueError(f"source_signal_id_not_in_journal: {resolution}")
                out = bridge_operator_feedback(
                    store=store,
                    journal=journal,
                    runtime_context=runtime_context,
                    raw_operator_payload=payload,
                )
                if str(row.get("adjudication_kind") or "").strip() == "reject_same_case":
                    validate_reject_same_case_bridge_result(out)
            completion_ok, completion_error = _append_completion_result(
                append_completion,
                queue_id=qid,
                status="completed",
                error="",
            )
            if not completion_ok:
                ber = format_bridge_error(
                    error_type="CompletionError",
                    error_message=completion_error,
                    stage="complete_item",
                    queue_id=qid,
                    source_signal_ids=_source_signal_ids_from_row(row) or None,
                )
                results.append(
                    {
                        "queue_id": qid,
                        "ok": False,
                        "error": completion_error,
                        "bridge_error": ber,
                        "bridge_completion_status": "completion_failed",
                        "bridge_out": out,
                    }
                )
                continue
            results.append({"queue_id": qid, "ok": True, "bridge_out": out})
        except Exception as exc:  # noqa: BLE001
            retryable = _is_retryable_bridge_error(exc)
            if retryable:
                next_attempt = retry_count + 1
                if next_attempt <= _BRIDGE_RETRY_MAX_ATTEMPTS:
                    status = "retry"
                    ber = _bridge_retry_error(exc=exc, queue_id=qid, row=row, attempt=next_attempt)
                else:
                    status = "dead_letter"
                    ber = format_bridge_error(
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        stage="process_item",
                        queue_id=qid,
                        source_signal_ids=_source_signal_ids_from_row(row) or None,
                        extra={"retryable": True, "retry_count": next_attempt},
                    )
            else:
                status = "failed"
                ber = format_bridge_error(
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    stage="process_item",
                    queue_id=qid,
                    source_signal_ids=_source_signal_ids_from_row(row) or None,
                )
            completion_ok, completion_error = _append_completion_result(
                append_completion,
                queue_id=qid,
                status=status,
                error=ber,
            )
            row_result = {
                "queue_id": qid,
                "ok": False,
                "error": str(exc),
                "bridge_error": ber,
                "bridge_status": status,
                "retryable": retryable,
            }
            if not completion_ok:
                row_result["bridge_completion_status"] = "failure_completion_failed"
                row_result["bridge_completion_error"] = completion_error
            results.append(row_result)
    return results


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Drain Daszek bridge_queue.jsonl through the Python adjudication bridge.")
    p.add_argument("--queue-path", help="Optional local Daszek v2 bridge_queue.jsonl path. If omitted, the REST bridge API is used.")
    p.add_argument("--remote", action="store_true", help="Use Daszek v2 REST bridge queue API instead of a local file.")
    p.add_argument("--max-items", type=int, default=25, help="Maximum pending rows to process.")
    p.add_argument("--dry-run", action="store_true", help="Print payloads without calling the bridge.")
    p.add_argument("--run-id", default="daszek-bridge-drain", help="Run id for SignalRuntimeContext.")
    p.add_argument(
        "--domain",
        choices=("any", "adjudication", "action_decision", "agent_hitl"),
        default="any",
        help="Optional filter: only drain pending rows with this domain (Gate B uses adjudication for truth-loop proof).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return run_daszek_bridge_drain(args)


if __name__ == "__main__":
    raise SystemExit(main())
