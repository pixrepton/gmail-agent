"""Gmail-specific normalization into the canonical signal contract."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from config import Settings

from raw_observation_contract import RawObservation, build_raw_observation
from signal_contract import CanonicalSignal, build_canonical_signal
from signal_journal import SignalJournal, SignalJournalAppendResult
from signal_reconciler import ReconcileResult, SignalRuntimeContext, reconcile_signal
from log_config import get_logger, get_trace_id

logger = get_logger(__name__)


@dataclass(slots=True)
class GmailSignalRuntimeResult:
    primary_signal: CanonicalSignal
    raw_observation: RawObservation | None = None
    signals: list[CanonicalSignal] = field(default_factory=list)
    append_results: list[SignalJournalAppendResult] = field(default_factory=list)
    reconcile_result: ReconcileResult | None = None


def build_gmail_raw_observation(
    *,
    snapshot: dict[str, Any],
    created_by_runtime: str,
) -> RawObservation:
    source_message = snapshot.get("source_message") or {}
    mailbox = str(snapshot.get("mailbox") or "")
    message_id = str(source_message.get("message_id") or "").strip()
    thread_id = str(source_message.get("thread_id") or "").strip()
    history_id = str(source_message.get("history_id") or "").strip()
    observed_at = str(snapshot.get("observed_at") or source_message.get("date") or "")
    occurred_at = str(source_message.get("date") or observed_at) or None
    return build_raw_observation(
        observation_kind="gmail_source_snapshot",
        source_kind="gmail",
        source_ref={
            "mailbox": mailbox,
            "message_id": message_id,
            "thread_id": thread_id,
            "history_id": history_id,
        },
        occurred_at=occurred_at,
        observed_at=observed_at,
        payload={"snapshot": snapshot},
        source_marker=history_id or message_id,
        created_by_runtime=created_by_runtime,
    )


def build_gmail_signals(
    *,
    snapshot: dict[str, Any],
    intake_result_final: dict[str, Any],
    preclassification_result: dict[str, Any],
    lane_stage_plan: dict[str, Any],
    context_bundle: dict[str, Any],
    raw_observation: RawObservation | None = None,
    triage_result: dict[str, Any] | None = None,
    created_by_runtime: str,
) -> list[CanonicalSignal]:
    observation = raw_observation or build_gmail_raw_observation(
        snapshot=snapshot,
        created_by_runtime=created_by_runtime,
    )
    triage = dict(triage_result or {})
    raw_snapshot = dict((observation.payload or {}).get("snapshot") or snapshot)
    source_message = raw_snapshot.get("source_message") or {}
    mailbox = str(snapshot.get("mailbox") or "")
    message_id = str(source_message.get("message_id") or "").strip()
    thread_id = str(source_message.get("thread_id") or "").strip()
    observed_at = str(observation.observed_at or raw_snapshot.get("observed_at") or source_message.get("date") or "")
    history_id = str(source_message.get("history_id") or "")
    subject = str(source_message.get("subject") or "").strip()
    attachments = list(source_message.get("attachment_parts") or [])
    historical_bootstrap = (
        str(raw_snapshot.get("ingest_mode") or "") == "historical_bootstrap"
        or str(source_message.get("ingest_mode") or "") == "historical_bootstrap"
        or bool(raw_snapshot.get("bootstrap_provenance") or source_message.get("bootstrap_provenance"))
    )
    if historical_bootstrap:
        message_source_ref = {
            "mailbox": mailbox,
            "message_id": message_id,
            "thread_id": thread_id,
            "ingest_mode": "historical_bootstrap",
        }
        message_revision_marker = message_id
    else:
        message_source_ref = observation.source_ref
        message_revision_marker = history_id or message_id

    base_payload = {
        "snapshot": raw_snapshot,
        "intake_result_final": intake_result_final,
        "preclassification_result": preclassification_result,
        "lane_stage_plan": lane_stage_plan,
        "context_bundle": context_bundle,
        "case_id": "",
    }
    try:
        from outbound_receipt import infer_live_direction

        base_payload["direction"] = infer_live_direction(
            source_message if isinstance(source_message, dict) else {},
            mailbox=mailbox,
        )
    except Exception:  # noqa: BLE001
        base_payload["direction"] = "unknown"
    base_artifacts = {
        "source": "process_snapshot",
        "raw_observation_id": observation.observation_id,
        "triage_result": triage,
    }
    primary = build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref=message_source_ref,
        observed_at=observed_at,
        effective_at=str(source_message.get("date") or "") or None,
        case_key_hint="",
        thread_key_hint=thread_id or None,
        business_lane=str(preclassification_result.get("lane") or ""),
        signal_summary_pl=f"Wiadomosc Gmail: {subject or message_id}",
        payload=base_payload,
        artifacts=base_artifacts,
        revision_marker=message_revision_marker,
        created_by_runtime=created_by_runtime,
    )
    signals = [primary]

    if thread_id:
        signals.append(
            build_canonical_signal(
                signal_kind="gmail_thread_update_observed",
                source_kind="gmail",
                source_ref={
                    "mailbox": mailbox,
                    "thread_id": thread_id,
                    "message_id": message_id,
                    **({"ingest_mode": "historical_bootstrap"} if historical_bootstrap else {"history_id": history_id}),
                },
                observed_at=observed_at,
                effective_at=str(source_message.get("date") or "") or None,
                case_key_hint="",
                thread_key_hint=thread_id,
                business_lane=str(preclassification_result.get("lane") or ""),
                signal_summary_pl=f"Aktualizacja watku Gmail: {subject or thread_id}",
                payload={
                    "thread_id": thread_id,
                    "message_id": message_id,
                    "context_message_ids": [str((item or {}).get("message_id") or "") for item in context_bundle.get("context_messages") or []],
                },
                artifacts=base_artifacts,
                revision_marker=f"{thread_id}:{message_id}" if historical_bootstrap else (f"{history_id}:{thread_id}" if history_id else thread_id),
                created_by_runtime=created_by_runtime,
            )
        )

    for attachment_index, attachment in enumerate(attachments):
        attachment_id = str(attachment.get("attachment_id") or attachment.get("storage_ref") or "").strip()
        attachment_name = str(attachment.get("name") or attachment.get("file_name") or attachment.get("filename") or "").strip()
        attachment_mime = str(attachment.get("mime_type") or attachment.get("mimeType") or "").strip()
        attachment_size = str(attachment.get("size_bytes") or attachment.get("size") or "").strip()
        if not attachment_id and not historical_bootstrap:
            continue
        if historical_bootstrap:
            attachment_source_ref = {
                "mailbox": mailbox,
                "message_id": message_id,
                "thread_id": thread_id,
                "attachment_index": attachment_index,
                "filename": attachment_name,
                "mime_type": attachment_mime,
                "size_bytes": attachment_size,
                "ingest_mode": "historical_bootstrap",
            }
            attachment_revision_marker = f"{message_id}:{attachment_index}:{attachment_name}:{attachment_mime}:{attachment_size}"
        else:
            attachment_source_ref = {
                "mailbox": mailbox,
                "message_id": message_id,
                "thread_id": thread_id,
                "attachment_id": attachment_id,
                "history_id": history_id,
            }
            attachment_revision_marker = f"{history_id}:{attachment_id}" if history_id else attachment_id
        signals.append(
            build_canonical_signal(
                signal_kind="gmail_attachment_observed",
                source_kind="gmail",
                source_ref=attachment_source_ref,
                observed_at=observed_at,
                effective_at=str(source_message.get("date") or "") or None,
                case_key_hint="",
                thread_key_hint=thread_id or None,
                business_lane=str(preclassification_result.get("lane") or ""),
                signal_summary_pl=f"Zalacznik Gmail: {attachment_name or attachment_id or f'attachment-{attachment_index + 1}'}",
                payload={
                    "message_id": message_id,
                    "thread_id": thread_id,
                    "attachment": dict(attachment),
                    "snapshot_ref": {"message_id": message_id, "thread_id": thread_id},
                },
                artifacts=base_artifacts,
                revision_marker=attachment_revision_marker,
                created_by_runtime=created_by_runtime,
            )
        )
    return signals


def proof_force_v2_reprocess(run_state: dict[str, Any] | None) -> bool:
    """Gate B / projection proof: run full reconcile + v2 ingest even when journal dedupes."""
    if not isinstance(run_state, dict):
        return False
    controls = run_state.get("runtime_controls")
    if isinstance(controls, dict) and bool(controls.get("projection_proof")):
        return True
    raw = os.getenv("GATE_B_PROOF_FORCE_V2_REPROCESS", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def run_gmail_signal_runtime(
    *,
    settings: Settings,
    run_state: dict[str, Any],
    snapshot: dict[str, Any],
    intake_result_final: dict[str, Any],
    preclassification_result: dict[str, Any],
    lane_stage_plan: dict[str, Any],
    context_bundle: dict[str, Any],
    raw_observation: RawObservation | None = None,
    triage_result: dict[str, Any] | None = None,
    model: str | None,
    verbose: bool,
    dry_run: bool,
) -> GmailSignalRuntimeResult:
    context = build_signal_runtime_context(settings=settings, run_state=run_state, model=model, verbose=verbose)
    signals = build_gmail_signals(
        snapshot=snapshot,
        intake_result_final=intake_result_final,
        preclassification_result=preclassification_result,
        lane_stage_plan=lane_stage_plan,
        context_bundle=context_bundle,
        raw_observation=raw_observation,
        triage_result=triage_result,
        created_by_runtime="gmail_intake.process_snapshot",
    )
    append_results = [context.journal.append(signal) for signal in signals]
    primary_signal = signals[0]
    force_reprocess = proof_force_v2_reprocess(run_state)
    if append_results[0].inserted or force_reprocess:
        if force_reprocess and not append_results[0].inserted:
            run_state.setdefault("warnings", []).append(
                "proof_force_v2_reprocess: signal journal duplicate; running full reconcile for projection proof"
            )
        from agent_runtime.agent_reconcile import agent_runtime_reconcile_active

        if agent_runtime_reconcile_active():
            run_state["reconcile_path"] = "agent_runtime"
        reconcile_result = reconcile_signal(primary_signal, runtime_context=context, dry_run=dry_run)
        if reconcile_result is not None and run_state.get("reconcile_path") == "agent_runtime":
            run_state["agent_engagement_id"] = str(
                (reconcile_result.mailbox_memory_result or {}).get("engagement_id") or ""
            )
    else:
        reconcile_result = ReconcileResult(
            signal_id=append_results[0].signal.signal_id,
            source_kind=append_results[0].signal.source_kind,
            signal_kind=append_results[0].signal.signal_kind,
            processing_state="skipped_duplicate",
        )
    return GmailSignalRuntimeResult(
        primary_signal=primary_signal,
        raw_observation=raw_observation,
        signals=signals,
        append_results=append_results,
        reconcile_result=reconcile_result,
    )


def build_signal_runtime_context(
    *,
    settings: Settings,
    run_state: dict[str, Any],
    model: str | None,
    verbose: bool,
) -> SignalRuntimeContext:
    existing = run_state.get("signal_runtime_context")
    if isinstance(existing, SignalRuntimeContext):
        return existing

    mailbox_runtime = run_state.get("mailbox_memory_runtime")
    journal = SignalJournal(
        mailbox_runtime.store if mailbox_runtime is not None else run_state.get("signal_store"),
        jsonl_mirror_enabled=bool(getattr(settings, "signal_journal_jsonl_mirror_enabled", False)),
        jsonl_mirror_path=_signal_jsonl_path(settings),
    )
    context = SignalRuntimeContext(
        settings=settings,
        journal=journal,
        mailbox_memory_runtime=mailbox_runtime,
        graph_store=getattr(mailbox_runtime, "graph_store", None) if mailbox_runtime is not None else None,
        run_state=run_state,
        model=model,
        verbose=verbose,
        mode=str(getattr(settings, "signal_runtime_mode", "legacy") or "legacy"),
        trace_id=str(run_state.get("trace_id") or get_trace_id() or ""),  # Faza 0c: propaguj trace_id z workera
    )
    run_state["signal_runtime_context"] = context
    return context


def _signal_jsonl_path(settings: Settings) -> Path:
    blob_root = Path(getattr(settings, "mailbox_memory_blob_root")).resolve()
    target = blob_root.parent / "signal_runtime" / "signals.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


__all__ = [
    "GmailSignalRuntimeResult",
    "build_gmail_raw_observation",
    "build_gmail_signals",
    "build_signal_runtime_context",
    "proof_force_v2_reprocess",
    "run_gmail_signal_runtime",
]
