"""Drive-specific normalization into the canonical signal contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from config import Settings

from raw_observation_contract import RawObservation, build_raw_observation
from signal_contract import CanonicalSignal, build_canonical_signal
from signal_journal import SignalJournal, SignalJournalAppendResult
from signal_reconciler import ReconcileResult, SignalRuntimeContext, reconcile_signal


@dataclass(slots=True)
class DriveSignalRuntimeResult:
    primary_signal: CanonicalSignal
    raw_observation: RawObservation | None = None
    signals: list[CanonicalSignal] = field(default_factory=list)
    append_results: list[SignalJournalAppendResult] = field(default_factory=list)
    reconcile_result: ReconcileResult | None = None


def build_drive_raw_observation(
    *,
    source_ref: dict[str, Any],
    observed_at: str,
    payload: dict[str, Any],
    created_by_runtime: str,
    observation_kind: str = "drive_candidate_observed",
) -> RawObservation:
    occurred_at = str(source_ref.get("modified_time") or observed_at) or None
    source_marker = str(source_ref.get("revision_id") or source_ref.get("change_id") or source_ref.get("file_id") or "")
    return build_raw_observation(
        observation_kind=observation_kind,
        source_kind="drive",
        source_ref=source_ref,
        occurred_at=occurred_at,
        observed_at=observed_at,
        payload=payload,
        source_marker=source_marker,
        created_by_runtime=created_by_runtime,
    )


def build_drive_signals(
    *,
    change_kind: str,
    source_ref: dict[str, Any],
    observed_at: str,
    signal_summary_pl: str,
    payload: dict[str, Any],
    raw_observation: RawObservation | None = None,
    triage_result: dict[str, Any] | None = None,
    created_by_runtime: str,
) -> list[CanonicalSignal]:
    observation = raw_observation or build_drive_raw_observation(
        source_ref=source_ref,
        observed_at=observed_at,
        payload=payload,
        created_by_runtime=created_by_runtime,
    )
    triage = dict(triage_result or {})
    document_row = dict(payload.get("document_row") or {})
    batching = dict(triage.get("batching") or {})
    batched_source_ref = dict(batching.get("source_ref_override") or {})
    effective_source_ref = batched_source_ref or observation.source_ref
    effective_signal_kind = str(batching.get("signal_kind") or change_kind or "")
    revision_marker = str(
        batching.get("revision_marker")
        or source_ref.get("revision_id")
        or source_ref.get("change_id")
        or source_ref.get("file_id")
        or ""
    )
    base_artifacts = {
        "source": "drive_ingest_runtime",
        "raw_observation_id": observation.observation_id,
        "triage_result": triage,
    }
    primary = build_canonical_signal(
        signal_kind=effective_signal_kind,
        source_kind="drive",
        source_ref=effective_source_ref,
        observed_at=observed_at,
        effective_at=str(source_ref.get("modified_time") or observed_at),
        case_key_hint=str(payload.get("case_key") or document_row.get("probable_case_key") or "") or None,
        thread_key_hint=str(payload.get("case_key") or document_row.get("probable_case_key") or "") or None,
        business_lane=str(document_row.get("lane") or ""),
        signal_summary_pl=signal_summary_pl,
        payload=payload,
        artifacts=base_artifacts,
        revision_marker=revision_marker,
        created_by_runtime=created_by_runtime,
    )
    signals = [primary]
    if bool(batching.get("enabled")):
        return signals

    if payload.get("document_row"):
        signals.append(
            build_canonical_signal(
                signal_kind="drive_extraction_completed",
                source_kind="drive",
                source_ref=source_ref,
                observed_at=observed_at,
                effective_at=str(source_ref.get("modified_time") or observed_at),
                case_key_hint=str(payload.get("case_key") or document_row.get("probable_case_key") or "") or None,
                thread_key_hint=str(payload.get("case_key") or document_row.get("probable_case_key") or "") or None,
                business_lane=str(document_row.get("lane") or ""),
                signal_summary_pl=f"Ekstrakcja Drive: {str(document_row.get('file_name') or source_ref.get('file_id') or '')}",
                payload={
                    "document_row": document_row,
                    "fact_rows": list(payload.get("fact_rows") or []),
                    "case_id": str(payload.get("case_id") or ""),
                    "case_key": str(payload.get("case_key") or ""),
                },
                artifacts=base_artifacts,
                revision_marker=f"{revision_marker}:extract",
                created_by_runtime=created_by_runtime,
            )
        )
    if payload.get("linkage_status"):
        signals.append(
            build_canonical_signal(
                signal_kind="drive_document_link_candidate",
                source_kind="drive",
                source_ref=source_ref,
                observed_at=observed_at,
                effective_at=str(source_ref.get("modified_time") or observed_at),
                case_key_hint=str(payload.get("case_key") or document_row.get("probable_case_key") or "") or None,
                thread_key_hint=str(payload.get("case_key") or document_row.get("probable_case_key") or "") or None,
                business_lane=str(document_row.get("lane") or ""),
                signal_summary_pl=f"Linkowanie Drive: {signal_summary_pl}",
                payload={
                    "case_id": str(payload.get("case_id") or ""),
                    "case_key": str(payload.get("case_key") or ""),
                    "linkage_status": str(payload.get("linkage_status") or ""),
                    "link_reasons": list(payload.get("link_reasons") or []),
                },
                artifacts=base_artifacts,
                revision_marker=f"{revision_marker}:link",
                created_by_runtime=created_by_runtime,
            )
        )
    for conflict in payload.get("conflicts") or []:
        signals.append(
            build_canonical_signal(
                signal_kind="drive_conflict_detected",
                source_kind="drive",
                source_ref=source_ref,
                observed_at=observed_at,
                effective_at=str(source_ref.get("modified_time") or observed_at),
                case_key_hint=str(payload.get("case_key") or document_row.get("probable_case_key") or "") or None,
                thread_key_hint=str(payload.get("case_key") or document_row.get("probable_case_key") or "") or None,
                business_lane=str(document_row.get("lane") or ""),
                signal_summary_pl=str(conflict),
                payload={
                    "case_id": str(payload.get("case_id") or ""),
                    "case_key": str(payload.get("case_key") or ""),
                    "document_row": document_row,
                    "conflict": str(conflict),
                },
                artifacts=base_artifacts,
                revision_marker=f"{revision_marker}:conflict:{conflict}",
                created_by_runtime=created_by_runtime,
            )
        )
    return signals


def run_drive_signal_runtime(
    *,
    settings: Settings,
    runtime_context: SignalRuntimeContext,
    change_kind: str,
    source_ref: dict[str, Any],
    observed_at: str,
    signal_summary_pl: str,
    payload: dict[str, Any],
    raw_observation: RawObservation | None = None,
    triage_result: dict[str, Any] | None = None,
    dry_run: bool,
) -> DriveSignalRuntimeResult:
    signals = build_drive_signals(
        change_kind=change_kind,
        source_ref=source_ref,
        observed_at=observed_at,
        signal_summary_pl=signal_summary_pl,
        payload=payload,
        raw_observation=raw_observation,
        triage_result=triage_result,
        created_by_runtime="drive_ingest_runtime",
    )
    append_results = [runtime_context.journal.append(signal) for signal in signals]
    primary_signal = signals[0]
    if append_results[0].inserted:
        reconcile_result = reconcile_signal(primary_signal, runtime_context=runtime_context, dry_run=dry_run)
    else:
        reconcile_result = ReconcileResult(
            signal_id=append_results[0].signal.signal_id,
            source_kind=append_results[0].signal.source_kind,
            signal_kind=append_results[0].signal.signal_kind,
            processing_state="skipped_duplicate",
        )
    return DriveSignalRuntimeResult(
        primary_signal=primary_signal,
        raw_observation=raw_observation,
        signals=signals,
        append_results=append_results,
        reconcile_result=reconcile_result,
    )


def build_drive_signal_runtime_context(
    *,
    settings: Settings,
    store: Any,
    graph_store: Any | None,
    run_state: dict[str, Any] | None = None,
) -> SignalRuntimeContext:
    journal = SignalJournal(
        store,
        jsonl_mirror_enabled=bool(getattr(settings, "signal_journal_jsonl_mirror_enabled", False)),
        jsonl_mirror_path=_drive_signal_jsonl_path(settings),
    )
    return SignalRuntimeContext(
        settings=settings,
        journal=journal,
        store=store,
        graph_store=graph_store,
        run_state=run_state,
        model=getattr(settings, "groq_model", ""),
        verbose=False,
        mode=str(getattr(settings, "signal_runtime_mode", "legacy") or "legacy"),
    )


def _drive_signal_jsonl_path(settings: Settings) -> Path:
    blob_root = Path(getattr(settings, "mailbox_memory_blob_root")).resolve()
    target = blob_root.parent / "signal_runtime" / "drive_signals.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


__all__ = [
    "DriveSignalRuntimeResult",
    "build_drive_raw_observation",
    "build_drive_signal_runtime_context",
    "build_drive_signals",
    "run_drive_signal_runtime",
]
