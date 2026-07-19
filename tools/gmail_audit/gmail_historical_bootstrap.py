"""Controlled Gmail historical bootstrap orchestration.

This module is intentionally small and dependency-injected at the Gmail API
edge. Importing it must not perform OAuth work or contact Gmail.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import time
from typing import Any, Callable
from uuid import uuid4
from config import Settings

from artifact_io import write_json, write_jsonl, write_text
from case_linker import link_case as run_case_linker
from gmail_signal_adapter import build_gmail_signals
from intake_payload import build_source_snapshot
from raw_observation_contract import build_raw_observation
from raw_observation_journal import RawObservationJournal
from redaction import sanitize_for_storage, sanitize_text
from signal_journal import SignalJournal


BOOTSTRAP_RUNTIME = "gmail_historical_bootstrap"
SOURCE_KIND = "gmail"
INGEST_MODE = "historical_bootstrap"
DEFAULT_BOOTSTRAP_QUERY = "to:me -in:spam -in:trash"
SOURCE_CURSOR_NOT_FINALIZED = "source cursor not finalized; delta-ingest must not start automatically"
LIVE_BOOTSTRAP_CONFIRMATION_ERROR = (
    "Refusing live Gmail historical bootstrap without --confirm-vps-node-b.\n"
    "Use --dry-run locally, or run on confirmed VPS/Node B with explicit confirmation."
)
BOOTSTRAP_ARTIFACT_AUDIT_SIGNAL_LIMIT = 10000
LOGISTICS_NOISE_TERMS = (
    "allegro",
    "inpost",
    "paczkomat",
    "poczta polska",
    "kurier",
    "dhl",
    "dpd",
    "ups",
    "fedex",
    "gls",
    "tracking",
    "trackingu",
    "przesylka",
    "przesyłka",
    "paczka",
    "potwierdzenie nadania",
    "potwierdzenie odbioru",
    "status przesylki",
    "status przesyłki",
    "odebrana",
    "nadana",
)
MARKETING_NOISE_TERMS = (
    "newsletter",
    "unsubscribe",
    "wypisz",
    "promocja",
    "promocyjna",
    "webinar",
    "outlet",
    "black friday",
    "rabat",
    "kampania",
    "marketing",
    "google ads",
    "adwords",
    "ads.google",
)
SYSTEM_NOISE_TERMS = (
    "social notification",
    "security alert",
    "verification code",
    "kod weryfikacyjny",
)
DOCUMENT_REVIEW_TERMS = (
    "faktura",
    "fv",
    "ksef",
    "kse-f",
    "invoice",
    "rachunek",
    "nota",
    "platnosc",
    "płatność",
)
OPERATIONAL_OVERRIDE_TERMS = (
    "lead",
    "zapytanie",
    "prosze o oferte",
    "proszę o ofertę",
    "wycena",
    "dobor",
    "dobór",
    "oferta",
    "zamowienie",
    "zamówienie",
    "serwis",
    "awaria",
    "usterka",
    "naprawa",
    "przeglad",
    "przegląd",
    "reklamacja",
    "gwarancja",
    "pompa ciepla",
    "pompa ciepła",
    "projekt",
    "rzut",
    "audyt",
)
SUPPLIER_HINT_TERMS = (
    "tadmar",
    "onninen",
    "hydrosolar",
    "ims",
    "beretta",
    "panasonic",
    "stiebel",
    "bims",
    "atum",
)
SOCIAL_SENDER_TERMS = ("facebookmail", "linkedin", "instagram", "twitter", "x.com")
SYSTEM_SENDER_TERMS = ("no-reply", "noreply", "donotreply", "mailer-daemon", "postmaster")


MetadataSearchFn = Callable[..., dict[str, Any]]
ProfileFetchFn = Callable[..., dict[str, Any]]
BodyFetchFn = Callable[..., dict[str, Any]]
AttachmentFetcherFactory = Callable[[], Callable[[str, str], bytes] | None]
LlmEnricherFn = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass(slots=True)
class GmailHistoricalBootstrapOptions:
    run_id: str = ""
    query: str = DEFAULT_BOOTSTRAP_QUERY
    after: str = ""
    before: str = ""
    days_back: int = 0
    limit: int = 100
    page_size: int = 100
    max_threads: int = 0
    max_messages_per_thread: int = 0
    include_label: tuple[str, ...] = ()
    exclude_label: tuple[str, ...] = ()
    metadata_only: bool = False
    fetch_body: bool = False
    fetch_attachments_metadata: bool = False
    fetch_attachments_content: bool = False
    max_attachment_bytes: int = 0
    dry_run: bool = True
    no_llm: bool = True
    selective_llm: bool = False
    max_llm_calls: int = 0
    max_llm_calls_per_thread: int = 1
    max_consecutive_failures: int = 0
    timebox_seconds: int = 0
    no_daszek_push: bool = True
    proof_dir: Path | None = None
    write_source_cursor: bool = False
    finalize_source_cursor: bool = False
    confirm_vps_node_b: bool = False
    bootstrap_run_id: str = ""
    runtime_profile: str = ""
    cursor_scope: str = "default"
    gmail_source: str = "google_api"
    model: str | None = None
    verbose: bool = False

    def normalized_run_id(self) -> str:
        if self.run_id.strip():
            return self.run_id.strip()
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        return f"gmail-bootstrap-history-{stamp}-{uuid4().hex[:8]}"


@dataclass(slots=True)
class BootstrapClock:
    started_at: float = field(default_factory=time.monotonic)

    def timed_out(self, options: GmailHistoricalBootstrapOptions) -> bool:
        return bool(options.timebox_seconds and time.monotonic() - self.started_at >= options.timebox_seconds)


def run_gmail_historical_bootstrap(
    *,
    settings: Settings,
    runtime: Any | None,
    options: GmailHistoricalBootstrapOptions,
    profile_fetcher: ProfileFetchFn,
    metadata_searcher: MetadataSearchFn,
    body_fetcher: BodyFetchFn | None = None,
    attachment_fetcher_factory: AttachmentFetcherFactory | None = None,
    llm_enricher: LlmEnricherFn | None = None,
) -> dict[str, Any]:
    """Run metadata scan / bounded backfill and write a redacted proof pack."""
    _validate_options(options)
    if options.finalize_source_cursor:
        return finalize_gmail_bootstrap_source_cursor(
            settings=settings,
            runtime=runtime,
            options=options,
            profile_fetcher=profile_fetcher,
        )

    run_id = options.normalized_run_id()
    observed_at = datetime.now().astimezone().isoformat()
    effective_query = build_bootstrap_query(options)
    clock = BootstrapClock()
    errors: list[dict[str, Any]] = []
    consecutive_failures = 0

    profile = _fetch_profile(settings, options=options, profile_fetcher=profile_fetcher)
    mailbox = infer_mailbox(profile)
    preflight = build_preflight_summary(settings=settings, runtime=runtime, options=options, profile=profile)
    if not bool(preflight.get("runtime_profile_ok", True)):
        raise RuntimeError(
            "Runtime profile mismatch for gmail-bootstrap-history: "
            f"expected {preflight.get('runtime_profile_expected')!r}, "
            f"actual {preflight.get('runtime_profile_actual')!r}."
        )
    previous_cursor = _safe_fetch_source_cursor(runtime, options.cursor_scope) if runtime is not None else None

    scan = scan_gmail_metadata(
        settings=settings,
        options=options,
        metadata_searcher=metadata_searcher,
        query=effective_query,
        mailbox=mailbox,
        clock=clock,
        errors=errors,
    )
    candidate_report = select_bootstrap_candidates(scan["messages"], options=options)
    next_batch = select_recommended_batch(candidate_report["candidates"], options=options)

    should_persist = bool(options.fetch_body and not options.metadata_only and not options.dry_run)
    raw_journal = RawObservationJournal(runtime.store) if runtime is not None and should_persist else None
    signal_journal = SignalJournal(runtime.store) if runtime is not None and should_persist else None
    attachment_fetcher = None
    if should_persist and options.fetch_attachments_content and options.max_attachment_bytes > 0 and attachment_fetcher_factory is not None:
        attachment_fetcher = attachment_fetcher_factory()

    if should_persist and runtime is None:
        raise RuntimeError("gmail-bootstrap-history requires mailbox memory runtime for bounded backfill.")
    if should_persist and runtime is not None:
        runtime.bootstrap()

    backfill_items: list[dict[str, Any]] = []
    idempotency = {
        "message_existing_count": 0,
        "message_insert_or_update_count": 0,
        "raw_observation_inserted_count": 0,
        "raw_observation_duplicate_count": 0,
        "signal_inserted_count": 0,
        "signal_duplicate_count": 0,
    }
    llm_budget = {
        "enabled": bool(options.selective_llm and not options.no_llm),
        "calls_used": 0,
        "max_calls": int(options.max_llm_calls or 0),
        "per_thread": {},
        "skipped": 0,
        "errors": 0,
    }
    case_linking = {"linked": 0, "weak_link": 0, "competing_links": 0, "no_link": 0, "needs_review": 0}

    candidate_by_message = {item["message_id"]: item for item in candidate_report["candidates"]}
    for item in next_batch:
        if clock.timed_out(options):
            errors.append({"stage": "backfill", "status": "stopped", "reason": "timebox_seconds"})
            break
        message_id = str(item.get("message_id") or "").strip()
        try:
            source_message = dict(item.get("metadata") or {})
            if should_persist:
                if body_fetcher is None:
                    raise RuntimeError("body_fetcher is required when --fetch-body is used.")
                source_message = body_fetcher(
                    settings,
                    message_id=message_id,
                    model=options.model,
                    verbose=options.verbose,
                    gmail_source=options.gmail_source,
                )
            if not options.fetch_attachments_metadata:
                source_message["attachment_parts"] = []
                source_message["attachment_names"] = []
                source_message["attachments"] = []
                source_message["has_attachment"] = False

            provenance = build_bootstrap_provenance(
                run_id=run_id,
                mailbox=mailbox,
                message=source_message,
                observed_at=observed_at,
            )
            source_message = inject_bootstrap_provenance(source_message, provenance)
            snapshot = build_source_snapshot(
                mailbox=mailbox,
                source_message=source_message,
                context_messages=[],
                observed_at=observed_at,
            )
            snapshot["ingest_mode"] = INGEST_MODE
            snapshot["bootstrap_run_id"] = run_id
            snapshot["bootstrap_provenance"] = provenance
            candidate = candidate_by_message.get(message_id, item)
            intake_result = build_bootstrap_intake_seed(snapshot, candidate)
            preclassification = {
                "lane": "review_direct" if candidate.get("candidate") else "reference_only",
                "reasons": list(candidate.get("priority_reasons") or candidate.get("exclusion_reasons") or []),
                "confidence": float(candidate.get("score") or 0.51),
            }
            if _should_selectively_enrich(options, llm_budget, candidate, snapshot):
                enriched = _run_selective_enrichment(
                    llm_enricher=llm_enricher,
                    snapshot=snapshot,
                    candidate=candidate,
                    llm_budget=llm_budget,
                )
                if enriched:
                    intake_result = enriched
                    preclassification["lane"] = "intake_llm"
            context_bundle = {"context_messages": [], "case_link_candidates": list(snapshot.get("case_link_candidates") or [])}
            case_link_result = run_case_linker(snapshot, intake_result, context_bundle)
            needs_review = str(case_link_result.get("decision") or "") in {"weak_link", "competing_links", "no_link"}
            decision = str(case_link_result.get("decision") or "no_link")
            case_linking[decision] = int(case_linking.get(decision, 0)) + 1
            if needs_review:
                case_linking["needs_review"] += 1

            existing_before = False
            if runtime is not None and should_persist:
                existing_before = bool(runtime.store.fetch_case_by_message_id(message_id))
                if existing_before:
                    idempotency["message_existing_count"] += 1
                raw = build_bootstrap_raw_observation(snapshot=snapshot, provenance=provenance)
                raw_result = raw_journal.append(raw) if raw_journal is not None else None
                if raw_result is not None and raw_result.inserted:
                    idempotency["raw_observation_inserted_count"] += 1
                elif raw_result is not None:
                    idempotency["raw_observation_duplicate_count"] += 1
                if candidate.get("candidate") and signal_journal is not None:
                    signals = build_gmail_signals(
                        snapshot=snapshot,
                        intake_result_final=intake_result,
                        preclassification_result=preclassification,
                        lane_stage_plan={
                            "lane": preclassification["lane"],
                            "run_case_linking": True,
                            "run_business_reasoning": False,
                            "run_reply_drafter": False,
                            "run_action_planner": False,
                            "expected_projection_mode": "historical_bootstrap",
                        },
                        context_bundle=context_bundle,
                        raw_observation=raw,
                        triage_result={"historical_bootstrap": True, "candidate": candidate},
                        created_by_runtime=BOOTSTRAP_RUNTIME,
                    )
                    for signal in signals:
                        signal_result = signal_journal.append(signal)
                        if signal_result.inserted:
                            idempotency["signal_inserted_count"] += 1
                        else:
                            idempotency["signal_duplicate_count"] += 1
                ingest = runtime.ingest_message(
                    snapshot=snapshot,
                    intake_result=intake_result,
                    case_link_result=case_link_result,
                    attachment_fetcher=attachment_fetcher,
                    attachment_max_bytes=int(options.max_attachment_bytes or 0),
                    process_attachment_documents=bool(options.fetch_attachments_content and options.max_attachment_bytes > 0),
                    refresh_document_intelligence=False,
                )
                final = runtime.finalize_case(
                    case_id=ingest.case_id,
                    message_id=message_id,
                    thread_id=str((snapshot.get("source_message") or {}).get("thread_id") or ""),
                    business_result={
                        "recommended_next_action": "review_required" if needs_review else "review_history",
                        "recommended_action_reason": "historical_bootstrap",
                    },
                    reply_result={"draft_enabled": False, "drafts": []},
                    action_plan_result={"primary_action": "hold", "why_this_action": "historical_bootstrap_no_outbound_actions"},
                    case_intelligence_result={"review_routing": {"review_mode": "historical_bootstrap_review" if needs_review else "none"}},
                )
                idempotency["message_insert_or_update_count"] += 1
                backfill_items.append(
                    {
                        "message_id": message_id,
                        "thread_id": str((snapshot.get("source_message") or {}).get("thread_id") or ""),
                        "case_id": final.case_id or ingest.case_id,
                        "existing_before": existing_before,
                        "case_link_decision": decision,
                        "needs_review": needs_review,
                        "candidate_tier": str(candidate.get("candidate_tier") or ""),
                        "priority_reasons": list(candidate.get("priority_reasons") or []),
                        "attachment_count": len(ingest.attachments or []),
                        "document_count": len(ingest.documents or []),
                        "raw_observation_inserted": bool(raw_result and raw_result.inserted),
                    }
                )
            else:
                backfill_items.append(
                    {
                        "message_id": message_id,
                        "thread_id": str(source_message.get("thread_id") or ""),
                        "would_persist": bool(options.fetch_body and not options.metadata_only),
                        "case_link_decision": decision,
                        "needs_review": needs_review,
                        "candidate_tier": str(candidate.get("candidate_tier") or ""),
                        "priority_reasons": list(candidate.get("priority_reasons") or []),
                        "dry_run": bool(options.dry_run),
                        "metadata_only": bool(options.metadata_only or not options.fetch_body),
                    }
                )
            consecutive_failures = 0
        except Exception as exc:  # noqa: BLE001
            consecutive_failures += 1
            errors.append(
                {
                    "stage": "backfill",
                    "message_id": message_id,
                    "error_type": type(exc).__name__,
                    "error": sanitize_text(str(exc))[:500],
                }
            )
            if options.max_consecutive_failures and consecutive_failures >= options.max_consecutive_failures:
                errors.append({"stage": "backfill", "status": "stopped", "reason": "max_consecutive_failures"})
                break

    end_profile = _fetch_profile(settings, options=options, profile_fetcher=profile_fetcher)
    safe_cursor = str(end_profile.get("historyId") or end_profile.get("history_id") or "").strip()
    source_cursor_summary = build_source_cursor_summary(
        options=options,
        run_id=run_id,
        mailbox=mailbox,
        safe_cursor=safe_cursor,
        previous_cursor=previous_cursor,
        backfill_success=not errors,
    )
    pre_fix_artifact_summary = audit_prefixed_bootstrap_artifacts(runtime)

    summary = build_run_summary(
        run_id=run_id,
        mailbox=mailbox,
        options=options,
        query=effective_query,
        preflight=preflight,
        scan=scan,
        candidate_report=candidate_report,
        next_batch=next_batch,
        backfill_items=backfill_items,
        idempotency=idempotency,
        llm_budget=llm_budget,
        source_cursor_summary=source_cursor_summary,
        case_linking=case_linking,
        pre_fix_artifact_summary=pre_fix_artifact_summary,
        errors=errors,
        observed_at=observed_at,
    )
    write_bootstrap_proof_pack(summary, options=options)
    return sanitize_for_storage(summary)


def finalize_gmail_bootstrap_source_cursor(
    *,
    settings: Settings,
    runtime: Any | None,
    options: GmailHistoricalBootstrapOptions,
    profile_fetcher: ProfileFetchFn,
) -> dict[str, Any]:
    """Finalize Gmail source cursor as an explicit, separate operator step."""
    run_id = options.normalized_run_id()
    now_iso = datetime.now().astimezone().isoformat()
    errors: list[dict[str, Any]] = []
    if options.dry_run:
        errors.append({"stage": "source_cursor", "status": "blocked", "reason": "dry_run"})
    if runtime is None:
        errors.append({"stage": "source_cursor", "status": "blocked", "reason": "mailbox_memory_runtime_missing"})
    bootstrap_run_id = str(options.bootstrap_run_id or "").strip()
    if not bootstrap_run_id:
        errors.append({"stage": "source_cursor", "status": "blocked", "reason": "bootstrap_run_id_required"})

    profile = _fetch_profile(settings, options=options, profile_fetcher=profile_fetcher) if not errors else {}
    mailbox = infer_mailbox(profile)
    safe_cursor = str(profile.get("historyId") or profile.get("history_id") or "").strip()
    if not safe_cursor and not errors:
        errors.append({"stage": "source_cursor", "status": "blocked", "reason": "gmail_profile_history_id_missing"})

    row: dict[str, Any] | None = None
    if not errors and runtime is not None:
        runtime.bootstrap()
        row = {
            "cursor_key": f"gmail:{options.cursor_scope or 'default'}",
            "source_kind": "gmail",
            "cursor_scope": options.cursor_scope or "default",
            "last_cursor": safe_cursor,
            "last_success_at": now_iso,
            "last_error": "",
            "status": "ok",
            "metadata": {
                "provider": "Gmail",
                "mailbox": mailbox,
                "bootstrap_run_id": bootstrap_run_id,
                "finalize_run_id": run_id,
                "ingest_mode": INGEST_MODE,
                "dry_run": False,
                "runtime_profile": str(getattr(settings, "runtime_profile", "") or ""),
                "operator_command": "gmail-bootstrap-history --finalize-source-cursor",
                "source": "gmail_historical_bootstrap_finalize",
            },
            "updated_at": now_iso,
        }
        runtime.store.upsert_source_cursor(row)

    source_cursor_summary = {
        "provider": "Gmail",
        "mailbox": mailbox,
        "cursor_scope": options.cursor_scope or "default",
        "bootstrap_run_id": bootstrap_run_id,
        "finalize_run_id": run_id,
        "source_cursor_written": bool(row),
        "safe_cursor_history_id": safe_cursor,
        "status": "ok" if row else "blocked",
        "message": "source cursor finalized" if row else SOURCE_CURSOR_NOT_FINALIZED,
        "errors": errors,
    }
    summary = {
        "run_id": run_id,
        "command": "gmail-bootstrap-history",
        "mode": "finalize_source_cursor",
        "status": "ok" if row else "blocked",
        "dry_run": bool(options.dry_run),
        "metadata_only": True,
        "no_daszek_push": True,
        "no_llm": True,
        "confirm_vps_node_b": bool(options.confirm_vps_node_b),
        "execution_environment": build_execution_environment(settings=settings, options=options),
        "source_cursor_summary": source_cursor_summary,
        "errors": errors,
    }
    write_bootstrap_proof_pack(summary, options=options)
    return sanitize_for_storage(summary)


def scan_gmail_metadata(
    *,
    settings: Settings,
    options: GmailHistoricalBootstrapOptions,
    metadata_searcher: MetadataSearchFn,
    query: str,
    mailbox: str,
    clock: BootstrapClock,
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    next_page_token = ""
    total_seen = 0
    page_count = 0
    while len(messages) < options.limit:
        if clock.timed_out(options):
            errors.append({"stage": "metadata_scan", "status": "stopped", "reason": "timebox_seconds"})
            break
        remaining = options.limit - len(messages)
        payload = metadata_searcher(
            settings,
            query=query,
            max_results=min(options.page_size, remaining),
            next_page_token=next_page_token or None,
            model=options.model,
            verbose=options.verbose,
            gmail_source=options.gmail_source,
        )
        page_count += 1
        page_items = [normalize_metadata_message(item, mailbox=mailbox) for item in payload.get("responses") or [] if isinstance(item, dict)]
        total_seen += len(page_items)
        for item in page_items:
            if not _labels_allowed(item, include=options.include_label, exclude=options.exclude_label):
                continue
            messages.append(item)
            if len(messages) >= options.limit:
                break
        next_page_token = str(payload.get("next_page_token") or "").strip()
        if not next_page_token or not page_items:
            break
    return {
        "scan_window": _scan_window(options),
        "query": query,
        "total_seen": total_seen,
        "metadata_record_count": len(messages),
        "page_count": page_count,
        "result_size_estimate": int(payload.get("result_size_estimate") or 0) if "payload" in locals() else 0,
        "next_page_token_present": bool(next_page_token),
        "messages": messages,
    }


def select_bootstrap_candidates(messages: list[dict[str, Any]], *, options: GmailHistoricalBootstrapOptions) -> dict[str, Any]:
    thread_counts: dict[str, int] = {}
    for message in messages:
        thread_id = str(message.get("thread_id") or "").strip()
        if thread_id:
            thread_counts[thread_id] = thread_counts.get(thread_id, 0) + 1

    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    priority_breakdown: dict[str, int] = {}
    exclusion_breakdown: dict[str, int] = {}
    candidate_tier_breakdown: dict[str, int] = {}
    excluded_sender_counter: Counter[str] = Counter()
    excluded_domain_counter: Counter[str] = Counter()
    estimated_attachment_bytes = 0
    for message in messages:
        result = classify_bootstrap_candidate(message, thread_message_count=thread_counts.get(str(message.get("thread_id") or ""), 0))
        record = {**result, "message_id": str(message.get("message_id") or ""), "thread_id": str(message.get("thread_id") or ""), "metadata": message}
        estimated_attachment_bytes += int(record.get("estimated_attachment_bytes_if_fetched") or 0)
        tier = str(record.get("candidate_tier") or ("operational_candidate" if record.get("candidate") else "noise_excluded"))
        candidate_tier_breakdown[tier] = candidate_tier_breakdown.get(tier, 0) + 1
        if record.get("candidate"):
            candidates.append(record)
            for reason in record.get("priority_reasons") or ["candidate"]:
                priority_breakdown[reason] = priority_breakdown.get(reason, 0) + 1
        else:
            excluded.append(record)
            for reason in record.get("exclusion_reasons") or ["low_operational_value"]:
                exclusion_breakdown[reason] = exclusion_breakdown.get(reason, 0) + 1
            sender_key = _redacted_sender_key(str(message.get("sender") or message.get("from") or ""))
            domain_key = _domain_from_sender(str(message.get("sender") or message.get("from") or ""))
            if sender_key:
                excluded_sender_counter[sender_key] += 1
            if domain_key:
                excluded_domain_counter[domain_key] += 1

    candidates.sort(key=lambda item: (int(item.get("priority_rank") or 0), float(item.get("score") or 0.0)), reverse=True)
    return {
        "scan_window": _scan_window(options),
        "total_seen": len(messages),
        "candidate_count": len(candidates),
        "excluded_count": len(excluded),
        "exclusion_breakdown": exclusion_breakdown,
        "priority_breakdown": priority_breakdown,
        "candidate_tier_breakdown": candidate_tier_breakdown,
        "top_excluded_senders": _counter_top_items(excluded_sender_counter),
        "top_excluded_domains": _counter_top_items(excluded_domain_counter),
        "estimated_llm_calls_if_enriched": len(candidates),
        "estimated_attachment_bytes_if_fetched": estimated_attachment_bytes,
        "recommended_next_batch": [
            {
                "message_id": item["message_id"],
                "thread_id": item["thread_id"],
                "priority_reasons": item.get("priority_reasons") or [],
                "candidate_tier": item.get("candidate_tier") or "",
                "score": item.get("score"),
            }
            for item in select_recommended_batch(candidates, options=options)
        ],
        "candidates": candidates,
        "excluded": excluded,
    }


def classify_bootstrap_candidate(message: dict[str, Any], *, thread_message_count: int = 0) -> dict[str, Any]:
    text = _message_text(message)
    sender = str(message.get("sender") or message.get("from") or "").lower()
    labels = {str(item).upper() for item in message.get("labels") or []}
    attachment_parts = [item for item in message.get("attachment_parts") or [] if isinstance(item, dict)]
    attachment_names = [str(item).lower() for item in message.get("attachment_names") or []]
    exclusion_reasons: list[str] = []
    system_sender = _contains_any(sender, SYSTEM_SENDER_TERMS)
    document_value = _contains_any(text, DOCUMENT_REVIEW_TERMS)
    operational_override = _contains_any(text, OPERATIONAL_OVERRIDE_TERMS)

    if labels.intersection({"SPAM", "TRASH"}):
        exclusion_reasons.append("spam_or_trash_label")
    if _contains_any(text, LOGISTICS_NOISE_TERMS) and not operational_override:
        exclusion_reasons.append("logistics_tracking_noise")
    if _contains_any(text, ("google ads", "adwords", "ads.google")) and not document_value:
        exclusion_reasons.append("google_ads_marketing_noise")
    if _contains_any(text, MARKETING_NOISE_TERMS) and not operational_override and not document_value:
        if _contains_any(sender + " " + text, SUPPLIER_HINT_TERMS):
            exclusion_reasons.append("supplier_marketing_newsletter")
        else:
            exclusion_reasons.append("newsletter_or_marketing_noise")
    if system_sender and not document_value:
        exclusion_reasons.append("no_reply_or_system_sender")
    if _contains_any(text, SYSTEM_NOISE_TERMS):
        exclusion_reasons.append("newsletter_or_system_noise")
    if _contains_any(sender, SOCIAL_SENDER_TERMS):
        exclusion_reasons.append("social_notification")
    if exclusion_reasons:
        return {
            "candidate": False,
            "score": 0.0,
            "priority_rank": 0,
            "candidate_tier": "noise_excluded",
            "priority_reasons": [],
            "exclusion_reasons": exclusion_reasons,
            "estimated_llm_calls_if_enriched": 0,
            "estimated_attachment_bytes_if_fetched": _attachment_bytes(attachment_parts),
        }

    priority_reasons: list[str] = []
    score = 0.0
    candidate_tier = "operational_candidate"
    if system_sender and document_value:
        priority_reasons.append("document_review_candidate")
        score += 1.25
        candidate_tier = "document_review_candidate"
    if _contains_any(text, ("lead", "zapytanie", "prosze o oferte", "proszę o ofertę", "wycena", "dobor", "dobór")):
        priority_reasons.append("active_lead_or_offer")
        score += 4.0
    if _contains_any(text, ("oferta", "pompa ciepla", "pompa ciepła", "panasonic", "mitsubishi", "kaisai", "rotenso")):
        priority_reasons.append("offer_or_heat_pump")
        score += 3.0
    if _contains_any(text, ("serwis", "awaria", "usterka", "naprawa", "przeglad", "przegląd")):
        priority_reasons.append("service")
        score += 3.0
    if _contains_any(text, ("reklamacja", "gwarancja", "nie dziala", "nie działa")):
        priority_reasons.append("complaint")
        score += 3.0
    if _contains_any(text, ("dokument", "zalacznik", "załącznik", "projekt", "rzut", "audyt", "faktura", "fv", "ksef")):
        priority_reasons.append("customer_or_finance_document")
        score += 1.25 if candidate_tier == "document_review_candidate" else 2.0
    if attachment_parts or attachment_names:
        priority_reasons.append("has_attachments")
        score += 1.5
    if thread_message_count > 1:
        priority_reasons.append("multi_reply_thread")
        score += 1.0
    if _looks_like_real_customer(sender):
        priority_reasons.append("real_customer_sender")
        score += 1.0
    if _contains_any(sender + " " + text, ("tadmar", "onninen", "hydrosolar", "ims", "beretta", "panasonic")):
        priority_reasons.append("supplier_or_distributor")
        score += 1.0

    if score <= 0.0:
        return {
            "candidate": False,
            "score": 0.0,
            "priority_rank": 0,
            "candidate_tier": "low_value_excluded",
            "priority_reasons": [],
            "exclusion_reasons": ["low_operational_value"],
            "estimated_llm_calls_if_enriched": 0,
            "estimated_attachment_bytes_if_fetched": _attachment_bytes(attachment_parts),
        }
    return {
        "candidate": True,
        "score": round(score, 2),
        "priority_rank": min(10, int(score)),
        "candidate_tier": candidate_tier,
        "priority_reasons": priority_reasons,
        "exclusion_reasons": [],
        "estimated_llm_calls_if_enriched": 1,
        "estimated_attachment_bytes_if_fetched": _attachment_bytes(attachment_parts),
    }


def select_recommended_batch(candidates: list[dict[str, Any]], *, options: GmailHistoricalBootstrapOptions) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    per_thread: dict[str, int] = {}
    thread_seen: set[str] = set()
    max_threads = int(options.max_threads or 0)
    max_per_thread = int(options.max_messages_per_thread or 0)
    for candidate in candidates:
        thread_id = str(candidate.get("thread_id") or "").strip() or str(candidate.get("message_id") or "")
        if max_threads and thread_id not in thread_seen and len(thread_seen) >= max_threads:
            continue
        if max_per_thread and per_thread.get(thread_id, 0) >= max_per_thread:
            continue
        selected.append(candidate)
        thread_seen.add(thread_id)
        per_thread[thread_id] = per_thread.get(thread_id, 0) + 1
        if len(selected) >= int(options.limit or 0):
            break
    return selected


def normalize_metadata_message(message: dict[str, Any], *, mailbox: str) -> dict[str, Any]:
    normalized = dict(message)
    normalized["message_id"] = str(normalized.get("message_id") or normalized.get("id") or "").strip()
    normalized["thread_id"] = str(normalized.get("thread_id") or normalized.get("threadId") or "").strip()
    normalized["history_id"] = str(normalized.get("history_id") or normalized.get("historyId") or "").strip()
    normalized["labels"] = [str(item).strip() for item in normalized.get("labels") or [] if str(item).strip()]
    normalized["attachment_parts"] = _normalize_attachment_parts(normalized)
    normalized["attachment_names"] = [
        str(item).strip()
        for item in normalized.get("attachment_names") or []
        if str(item).strip()
    ]
    normalized["attachment_count"] = len(normalized["attachment_parts"] or normalized["attachment_names"])
    normalized["direction"] = infer_direction(normalized, mailbox=mailbox)
    normalized["body"] = ""
    return normalized


def infer_direction(message: dict[str, Any], *, mailbox: str) -> str:
    mailbox_email = _first_email(mailbox)
    sender = _first_email(str(message.get("sender") or message.get("from") or ""))
    recipients = {_first_email(str(item)) for item in [*list(message.get("to") or []), *list(message.get("cc") or []), *list(message.get("bcc") or [])]}
    recipients.discard("")
    if mailbox_email and sender == mailbox_email:
        return "outbound"
    if mailbox_email and mailbox_email in recipients:
        return "inbound"
    if sender and recipients and sender in recipients:
        return "internal"
    return "unknown"


def build_bootstrap_query(options: GmailHistoricalBootstrapOptions) -> str:
    parts = [str(options.query or DEFAULT_BOOTSTRAP_QUERY).strip()]
    if options.days_back and not options.after:
        parts.append(f"newer_than:{int(options.days_back)}d")
    if options.after:
        parts.append(f"after:{_gmail_query_date(options.after)}")
    if options.before:
        parts.append(f"before:{_gmail_query_date(options.before)}")
    return " ".join(part for part in parts if part).strip()


def build_bootstrap_provenance(*, run_id: str, mailbox: str, message: dict[str, Any], observed_at: str) -> dict[str, Any]:
    message_id = str(message.get("message_id") or message.get("id") or "").strip()
    thread_id = str(message.get("thread_id") or message.get("threadId") or "").strip()
    history_id = str(message.get("history_id") or message.get("historyId") or "").strip()
    source_timestamp = str(message.get("date") or message.get("email_ts") or message.get("internal_date") or "")
    return {
        "source": SOURCE_KIND,
        "source_type": SOURCE_KIND,
        "ingest_mode": INGEST_MODE,
        "bootstrap_run_id": run_id,
        "mailbox": mailbox,
        "gmail_message_id": message_id,
        "gmail_thread_id": thread_id,
        "gmail_history_id": history_id,
        "source_timestamp": source_timestamp,
        "observed_at": observed_at,
        "idempotency_key": build_idempotency_key(mailbox=mailbox, thread_id=thread_id, message_id=message_id),
        "parser_status": "metadata_only" if not str(message.get("body") or "") else "body_fetched",
    }


def inject_bootstrap_provenance(message: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    payload = dict(message)
    payload["bootstrap_provenance"] = provenance
    payload["ingest_mode"] = INGEST_MODE
    payload["bootstrap_run_id"] = provenance["bootstrap_run_id"]
    payload["history_id"] = str(payload.get("history_id") or payload.get("historyId") or provenance.get("gmail_history_id") or "")
    raw = dict(payload.get("raw") or {})
    raw["bootstrap_provenance"] = provenance
    payload["raw"] = raw
    return payload


def build_bootstrap_raw_observation(*, snapshot: dict[str, Any], provenance: dict[str, Any]):
    source_message = snapshot.get("source_message") or {}
    observed_at = str(snapshot.get("observed_at") or provenance.get("observed_at") or "")
    return build_raw_observation(
        observation_kind="gmail_historical_bootstrap_message",
        source_kind=SOURCE_KIND,
        source_ref={
            "mailbox": provenance.get("mailbox") or snapshot.get("mailbox") or "",
            "message_id": provenance.get("gmail_message_id") or source_message.get("message_id") or "",
            "thread_id": provenance.get("gmail_thread_id") or source_message.get("thread_id") or "",
            "history_id": provenance.get("gmail_history_id") or source_message.get("history_id") or "",
            "ingest_mode": INGEST_MODE,
        },
        occurred_at=str(provenance.get("source_timestamp") or source_message.get("date") or "") or None,
        observed_at=observed_at,
        payload={"snapshot": snapshot, "bootstrap_provenance": provenance},
        source_marker=str(provenance.get("idempotency_key") or ""),
        created_by_runtime=BOOTSTRAP_RUNTIME,
    )


def build_bootstrap_intake_seed(snapshot: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    message = snapshot.get("source_message") or {}
    family = _case_family_from_candidate(candidate)
    review_required = bool(candidate.get("candidate"))
    return {
        "schema_version": "1.0",
        "source": {
            "channel": "gmail",
            "mailbox": str(snapshot.get("mailbox") or ""),
            "observed_at": str(snapshot.get("observed_at") or ""),
        },
        "message": {
            "message_id": str(message.get("message_id") or ""),
            "date": str(message.get("date") or ""),
            "sender": str(message.get("sender") or ""),
            "to": list(message.get("to") or []),
            "cc": list(message.get("cc") or []),
            "subject": str(message.get("subject") or ""),
            "snippet": str(message.get("snippet") or ""),
            "has_attachments": bool(message.get("has_attachments")),
            "labels": list(message.get("labels") or []),
        },
        "thread": {
            "thread_id": str(message.get("thread_id") or ""),
            "thread_position": str(message.get("thread_position_hint") or "unknown"),
            "is_reply_or_forward": bool(message.get("is_reply_or_forward_hint")),
            "thread_summary": "historical_bootstrap deterministic seed",
            "linked_case_candidates": list(snapshot.get("case_link_candidates") or []),
        },
        "business_area": family,
        "primary_signal": {
            "code": "historical_bootstrap_candidate" if candidate.get("candidate") else "historical_bootstrap_reference",
            "name": "Historical bootstrap candidate",
            "description": "Deterministic historical bootstrap triage.",
            "business_significance": "Mailbox memory backfill candidate selected without outbound action.",
        },
        "secondary_signals": [],
        "case_assessment": {
            "case_family": family,
            "is_new_case": False,
            "state_detected": "historical",
            "state_change": {"detected": False},
        },
        "decision": {
            "action": "review" if review_required else "mark_reference",
            "action_rationale": "historical_bootstrap deterministic triage",
        },
        "priority": "medium" if review_required else "low",
        "confidence": {
            "signal_confidence": min(0.9, max(0.5, float(candidate.get("score") or 0.5) / 10.0)),
            "case_link_confidence": 0.0,
            "decision_confidence": 0.65,
            "extraction_confidence": 0.55,
        },
        "review": {
            "required": review_required,
            "flags": ["historical_bootstrap", *list(candidate.get("priority_reasons") or [])[:5]],
        },
        "reason": "historical_bootstrap deterministic triage",
        "extracted_data": {
            "entities": {"people": [], "organizations": [], "locations": [], "products": []},
            "dates": [],
            "amounts": [],
            "references": {"invoice_numbers": [], "shipment_numbers": [], "order_numbers": [], "transaction_numbers": [], "case_ids": []},
            "deadlines": [],
        },
    }


def build_preflight_summary(*, settings: Settings, runtime: Any | None, options: GmailHistoricalBootstrapOptions, profile: dict[str, Any]) -> dict[str, Any]:
    expected_profile = str(options.runtime_profile or "").strip().lower()
    actual_profile = str(getattr(settings, "runtime_profile", "") or "").strip().lower()
    runtime_profile_ok = not expected_profile or expected_profile == actual_profile
    return {
        "gmail_api_readiness": "configured" if profile else "not_checked",
        "gmail_mailbox": infer_mailbox(profile),
        "gmail_history_id_present": bool(profile.get("historyId") or profile.get("history_id")),
        "mailbox_memory_runtime_present": runtime is not None,
        "mailbox_memory_stage_mode": str(getattr(settings, "mailbox_memory_stage_mode", "") or ""),
        "pgvector_enabled": bool(getattr(settings, "mailbox_memory_vector_enabled", False)),
        "llm_provider_configured": bool(str(getattr(settings, "groq_api_key", "") or getattr(settings, "openai_compat_api_key", "") or "").strip()),
        "llm_required": bool(options.selective_llm and not options.no_llm),
        "daszek_push_requested": False,
        "daszek_push_allowed": False,
        "runtime_profile_expected": expected_profile,
        "runtime_profile_actual": actual_profile,
        "runtime_profile_ok": runtime_profile_ok,
        "dry_run": bool(options.dry_run),
    }


def build_source_cursor_summary(
    *,
    options: GmailHistoricalBootstrapOptions,
    run_id: str,
    mailbox: str,
    safe_cursor: str,
    previous_cursor: dict[str, Any] | None,
    backfill_success: bool,
) -> dict[str, Any]:
    can_write = bool(options.write_source_cursor and not options.dry_run and backfill_success and safe_cursor)
    return {
        "provider": "Gmail",
        "mailbox": mailbox,
        "cursor_scope": options.cursor_scope or "default",
        "bootstrap_run_id": run_id,
        "previous_cursor": sanitize_for_storage(previous_cursor or {}),
        "safe_cursor_history_id": safe_cursor,
        "source_cursor_written": False,
        "write_requested": bool(options.write_source_cursor),
        "write_allowed": can_write,
        "status": "ready_for_finalize" if safe_cursor and backfill_success else "blocked",
        "message": "ready for explicit finalize step" if safe_cursor and backfill_success else SOURCE_CURSOR_NOT_FINALIZED,
        "dry_run": bool(options.dry_run),
    }


def build_run_summary(
    *,
    run_id: str,
    mailbox: str,
    options: GmailHistoricalBootstrapOptions,
    query: str,
    preflight: dict[str, Any],
    scan: dict[str, Any],
    candidate_report: dict[str, Any],
    next_batch: list[dict[str, Any]],
    backfill_items: list[dict[str, Any]],
    idempotency: dict[str, Any],
    llm_budget: dict[str, Any],
    source_cursor_summary: dict[str, Any],
    case_linking: dict[str, Any],
    pre_fix_artifact_summary: dict[str, Any] | None,
    errors: list[dict[str, Any]],
    observed_at: str,
) -> dict[str, Any]:
    coverage_summary = build_coverage_summary(
        options=options,
        scan=scan,
        candidate_report=candidate_report,
        backfill_items=backfill_items,
        idempotency=idempotency,
        llm_budget=llm_budget,
        source_cursor_summary=source_cursor_summary,
    )
    operator_review_cases = build_operator_review_cases(backfill_items, limit=10)
    return {
        "run_id": run_id,
        "command": "gmail-bootstrap-history",
        "status": "completed_with_errors" if errors else "completed",
        "started_at": observed_at,
        "mailbox": mailbox,
        "dry_run": bool(options.dry_run),
        "metadata_only": bool(options.metadata_only or not options.fetch_body),
        "fetch_body": bool(options.fetch_body),
        "fetch_attachments_metadata": bool(options.fetch_attachments_metadata),
        "fetch_attachments_content": bool(options.fetch_attachments_content),
        "no_llm": bool(options.no_llm),
        "selective_llm": bool(options.selective_llm),
        "no_daszek_push": True,
        "confirm_vps_node_b": bool(options.confirm_vps_node_b),
        "execution_environment": build_execution_environment(settings=None, options=options),
        "query": query,
        "preflight": preflight,
        "metadata_scan_summary": {
            key: value for key, value in scan.items() if key != "messages"
        },
        "candidate_selection_summary": {
            key: value for key, value in candidate_report.items() if key not in {"candidates", "excluded"}
        },
        "coverage_summary": coverage_summary,
        "candidate_tier_summary": build_candidate_tier_summary(candidate_report),
        "top_exclusions": build_top_exclusions(candidate_report),
        "backfill_summary": {
            "requested_count": len(next_batch),
            "processed_count": len(backfill_items),
            "persisted_count": sum(1 for item in backfill_items if "case_id" in item),
            "dry_run": bool(options.dry_run),
            "items": backfill_items,
        },
        "excluded_summary": {
            "excluded_count": len(candidate_report.get("excluded") or []),
            "exclusion_breakdown": candidate_report.get("exclusion_breakdown") or {},
            "items": build_redacted_excluded_records(candidate_report.get("excluded") or [], limit=50),
        },
        "idempotency_check": idempotency,
        "llm_summary": llm_budget,
        "source_cursor_summary": source_cursor_summary,
        "case_linking_summary": case_linking,
        "pre_fix_artifact_summary": pre_fix_artifact_summary or {"status": "skipped", "reason": "runtime_unavailable"},
        "operator_review_cases": operator_review_cases,
        "errors": errors,
        "sample_records": build_redacted_samples(scan.get("messages") or [], limit=10),
    }


def build_coverage_summary(
    *,
    options: GmailHistoricalBootstrapOptions,
    scan: dict[str, Any],
    candidate_report: dict[str, Any],
    backfill_items: list[dict[str, Any]],
    idempotency: dict[str, Any],
    llm_budget: dict[str, Any],
    source_cursor_summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "scan_window": scan.get("scan_window", ""),
        "query": scan.get("query", ""),
        "metadata_record_count": scan.get("metadata_record_count", 0),
        "total_seen": scan.get("total_seen", 0),
        "candidate_count": candidate_report.get("candidate_count", 0),
        "excluded_count": candidate_report.get("excluded_count", 0),
        "candidate_tier_breakdown": candidate_report.get("candidate_tier_breakdown") or {},
        "requested_batch_count": len(select_recommended_batch(candidate_report.get("candidates") or [], options=options)),
        "processed_count": len(backfill_items),
        "persisted_count": sum(1 for item in backfill_items if "case_id" in item),
        "raw_observation_inserted_count": idempotency.get("raw_observation_inserted_count", 0),
        "signal_inserted_count": idempotency.get("signal_inserted_count", 0),
        "signal_duplicate_count": idempotency.get("signal_duplicate_count", 0),
        "llm_calls_used": llm_budget.get("calls_used", 0),
        "daszek_push_enabled": False,
        "source_cursor_written": bool(source_cursor_summary.get("source_cursor_written", False)),
        "future_historical_batches_should_write_source_cursor": False,
    }


def build_candidate_tier_summary(candidate_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_tier_breakdown": candidate_report.get("candidate_tier_breakdown") or {},
        "priority_breakdown": candidate_report.get("priority_breakdown") or {},
        "exclusion_breakdown": candidate_report.get("exclusion_breakdown") or {},
        "document_review_candidate_policy": "system/no-reply finance documents are review candidates, not high-priority leads",
    }


def build_top_exclusions(candidate_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "top_excluded_senders": candidate_report.get("top_excluded_senders") or [],
        "top_excluded_domains": candidate_report.get("top_excluded_domains") or [],
        "exclusion_breakdown": candidate_report.get("exclusion_breakdown") or {},
    }


def build_operator_review_cases(backfill_items: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_cases: set[str] = set()
    sorted_items = sorted(
        backfill_items,
        key=lambda item: (
            not bool(item.get("needs_review")),
            str(item.get("candidate_tier") or ""),
            str(item.get("case_id") or ""),
        ),
    )
    for item in sorted_items:
        case_id = str(item.get("case_id") or "").strip()
        if not case_id or case_id in seen_cases:
            continue
        seen_cases.add(case_id)
        selected.append(
            {
                "case_id": case_id,
                "message_id": str(item.get("message_id") or ""),
                "thread_id": str(item.get("thread_id") or ""),
                "candidate_tier": str(item.get("candidate_tier") or ""),
                "priority_reasons": list(item.get("priority_reasons") or [])[:5],
                "case_link_decision": str(item.get("case_link_decision") or ""),
                "needs_review": bool(item.get("needs_review")),
                "recommended_check": 'case-context --query-text "status sprawy i najwazniejsze fakty"',
            }
        )
        if len(selected) >= limit:
            break
    return selected


def build_redacted_excluded_records(records: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    redacted: list[dict[str, Any]] = []
    for record in records[:limit]:
        metadata = record.get("metadata") or {}
        redacted.append(
            {
                "message_id": str(record.get("message_id") or ""),
                "thread_id": str(record.get("thread_id") or ""),
                "candidate_tier": str(record.get("candidate_tier") or ""),
                "exclusion_reasons": list(record.get("exclusion_reasons") or []),
                "sender": _redact_private_text(str(metadata.get("sender") or metadata.get("from") or "")),
                "sender_domain": _domain_from_sender(str(metadata.get("sender") or metadata.get("from") or "")),
                "subject": _redact_private_text(str(metadata.get("subject") or ""))[:160],
                "snippet": _redact_private_text(str(metadata.get("snippet") or ""))[:160],
                "labels": list(metadata.get("labels") or []),
                "attachment_count": int(metadata.get("attachment_count") or 0),
            }
        )
    return redacted


def audit_prefixed_bootstrap_artifacts(runtime: Any | None) -> dict[str, Any]:
    """Read-only audit for logical signal duplicates created before stable bootstrap keys."""
    if runtime is None:
        return {"status": "skipped", "reason": "runtime_unavailable"}
    try:
        rows = runtime.store.fetch_signals_for_source(SOURCE_KIND, limit=BOOTSTRAP_ARTIFACT_AUDIT_SIGNAL_LIMIT)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "error": sanitize_text(str(exc))[:300],
            "mutated": False,
        }

    bootstrap_rows = [row for row in rows if _is_bootstrap_signal_row(row)]
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in bootstrap_rows:
        groups.setdefault(_logical_bootstrap_signal_key(row), []).append(row)
    duplicate_groups = {key: value for key, value in groups.items() if len(value) > 1}
    samples = []
    for key, items in list(duplicate_groups.items())[:20]:
        samples.append(
            {
                "logical_key": key,
                "row_count": len(items),
                "signal_ids": [str(item.get("signal_id") or "") for item in items[:5]],
                "idempotency_keys": [str(item.get("idempotency_key") or "") for item in items[:5]],
                "created_at": [str(item.get("created_at") or "") for item in items[:5]],
            }
        )
    return {
        "status": "ok",
        "mutated": False,
        "note": "Read-only report. Do not delete automatically; treat duplicate groups as pre-fix artifacts until operator review.",
        "signal_rows_scanned": len(rows),
        "bootstrap_signal_rows": len(bootstrap_rows),
        "logical_group_count": len(groups),
        "duplicate_logical_group_count": len(duplicate_groups),
        "duplicate_signal_row_count": sum(len(items) for items in duplicate_groups.values()),
        "sample_duplicate_groups": samples,
    }


def write_bootstrap_proof_pack(summary: dict[str, Any], *, options: GmailHistoricalBootstrapOptions) -> None:
    proof_dir = options.proof_dir
    if proof_dir is None:
        return
    proof_dir = Path(proof_dir).expanduser().resolve()
    proof_dir.mkdir(parents=True, exist_ok=True)
    safe_summary = sanitize_for_storage(summary)
    write_text(proof_dir / "README.md", render_proof_readme(safe_summary))
    write_text(proof_dir / "commands.txt", render_command_summary(options, safe_summary))
    write_json(proof_dir / "environment-summary.json", build_environment_summary(safe_summary))
    write_json(proof_dir / "bootstrap-plan.json", build_bootstrap_plan(options, safe_summary))
    write_json(proof_dir / "metadata-scan-summary.json", safe_summary.get("metadata_scan_summary") or {})
    write_json(proof_dir / "candidate-selection-summary.json", safe_summary.get("candidate_selection_summary") or {})
    write_json(proof_dir / "coverage-summary.json", safe_summary.get("coverage_summary") or {})
    write_json(proof_dir / "candidate-tier-summary.json", safe_summary.get("candidate_tier_summary") or {})
    write_json(proof_dir / "top-exclusions.json", safe_summary.get("top_exclusions") or {})
    write_json(proof_dir / "backfill-summary.json", safe_summary.get("backfill_summary") or {})
    write_json(proof_dir / "excluded-summary.json", safe_summary.get("excluded_summary") or {})
    write_json(proof_dir / "idempotency-check.json", safe_summary.get("idempotency_check") or {})
    write_json(proof_dir / "source-cursor-summary.json", safe_summary.get("source_cursor_summary") or {})
    write_json(proof_dir / "case-linking-summary.json", safe_summary.get("case_linking_summary") or {})
    write_json(proof_dir / "pre-fix-artifact-summary.json", safe_summary.get("pre_fix_artifact_summary") or {})
    write_jsonl(proof_dir / "errors.jsonl", list(safe_summary.get("errors") or []))
    write_jsonl(proof_dir / "sample-records.redacted.jsonl", list(safe_summary.get("sample_records") or []))
    write_jsonl(proof_dir / "operator-review-cases.redacted.jsonl", list(safe_summary.get("operator_review_cases") or []))


def render_proof_readme(summary: dict[str, Any]) -> str:
    scan = summary.get("metadata_scan_summary") or {}
    candidates = summary.get("candidate_selection_summary") or {}
    backfill = summary.get("backfill_summary") or {}
    cursor = summary.get("source_cursor_summary") or {}
    tiers = summary.get("candidate_tier_summary") or {}
    pre_fix = summary.get("pre_fix_artifact_summary") or {}
    lines = [
        "# Gmail Historical Bootstrap Proof Pack",
        "",
        f"- run_id: {summary.get('run_id', '')}",
        f"- status: {summary.get('status', '')}",
        f"- mailbox: {summary.get('mailbox', '')}",
        f"- dry_run: {summary.get('dry_run', False)}",
        f"- metadata_only: {summary.get('metadata_only', False)}",
        f"- scan_window: {scan.get('scan_window', '')}",
        f"- total_seen: {scan.get('total_seen', 0)}",
        f"- candidate_count: {candidates.get('candidate_count', 0)}",
        f"- excluded_count: {candidates.get('excluded_count', 0)}",
        f"- candidate_tiers: {json.dumps(tiers.get('candidate_tier_breakdown') or {}, ensure_ascii=False, sort_keys=True)}",
        f"- persisted_count: {backfill.get('persisted_count', 0)}",
        f"- llm_calls_used: {(summary.get('llm_summary') or {}).get('calls_used', 0)}",
        f"- attachment_content_fetched: {summary.get('fetch_attachments_content', False)}",
        f"- source_cursor_written: {cursor.get('source_cursor_written', False)}",
        f"- daszek_push: disabled",
        f"- pre_fix_artifact_duplicate_groups: {pre_fix.get('duplicate_logical_group_count', 0)}",
        "",
        "This proof pack does not prove VPS/operator Gate B readiness unless it was produced on Node B with the approved production-shaped runtime and recorded separately.",
        "It intentionally excludes secrets, full email bodies, and raw attachment content.",
        "",
    ]
    if summary.get("errors"):
        lines.append("## Errors")
        for error in summary.get("errors") or []:
            lines.append(f"- {error.get('stage', '')}: {error.get('reason') or error.get('error') or ''}")
    return "\n".join(lines) + "\n"


def render_command_summary(options: GmailHistoricalBootstrapOptions, summary: dict[str, Any]) -> str:
    parts = [
        "python tools/gmail_audit/gmail_intake.py gmail-bootstrap-history",
        f"--query {json.dumps(options.query)}",
        f"--limit {options.limit}",
    ]
    if options.days_back:
        parts.append(f"--days-back {options.days_back}")
    if options.after:
        parts.append(f"--after {options.after}")
    if options.before:
        parts.append(f"--before {options.before}")
    for flag, enabled in (
        ("--metadata-only", options.metadata_only),
        ("--fetch-body", options.fetch_body),
        ("--fetch-attachments-metadata", options.fetch_attachments_metadata),
        ("--fetch-attachments-content", options.fetch_attachments_content),
        ("--dry-run", options.dry_run),
        ("--no-llm", options.no_llm),
        ("--selective-llm", options.selective_llm),
        ("--no-daszek-push", True),
        ("--confirm-vps-node-b", options.confirm_vps_node_b),
    ):
        if enabled:
            parts.append(flag)
    if options.proof_dir:
        parts.append(f"--proof-dir {options.proof_dir}")
    return " ".join(parts) + "\n\n# run_id\n" + str(summary.get("run_id") or "") + "\n"


def build_environment_summary(summary: dict[str, Any]) -> dict[str, Any]:
    preflight = summary.get("preflight") or {}
    env = dict(summary.get("execution_environment") or {})
    return {
        "execution_mode": env.get("execution_mode", "unknown"),
        "hostname": env.get("hostname", ""),
        "cwd": env.get("cwd", ""),
        "runtime_profile": env.get("runtime_profile", preflight.get("runtime_profile_actual", "")),
        "dry_run": bool(summary.get("dry_run", False)),
        "metadata_only": bool(summary.get("metadata_only", False)),
        "confirm_vps_node_b": bool(summary.get("confirm_vps_node_b", False)),
        "llm_enabled": not bool(summary.get("no_llm", True)),
        "daszek_push_enabled": False,
        "runtime_profile_actual": preflight.get("runtime_profile_actual", ""),
        "runtime_profile_expected": preflight.get("runtime_profile_expected", ""),
        "runtime_profile_ok": preflight.get("runtime_profile_ok", False),
        "mailbox_memory_runtime_present": preflight.get("mailbox_memory_runtime_present", False),
        "mailbox_memory_stage_mode": preflight.get("mailbox_memory_stage_mode", ""),
        "pgvector_enabled": preflight.get("pgvector_enabled", False),
        "gmail_api_readiness": preflight.get("gmail_api_readiness", ""),
        "llm_required": preflight.get("llm_required", False),
        "daszek_push_requested": False,
        "daszek_push_allowed": False,
    }


def build_bootstrap_plan(options: GmailHistoricalBootstrapOptions, summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": summary.get("run_id", ""),
        "query": summary.get("query", ""),
        "limits": {
            "limit": options.limit,
            "max_threads": options.max_threads,
            "max_messages_per_thread": options.max_messages_per_thread,
            "timebox_seconds": options.timebox_seconds,
            "max_consecutive_failures": options.max_consecutive_failures,
        },
        "fetch_policy": {
            "metadata_only": bool(options.metadata_only or not options.fetch_body),
            "fetch_body": bool(options.fetch_body),
            "fetch_attachments_metadata": bool(options.fetch_attachments_metadata),
            "fetch_attachments_content": bool(options.fetch_attachments_content),
            "max_attachment_bytes": int(options.max_attachment_bytes or 0),
        },
        "safety": {
            "dry_run": bool(options.dry_run),
            "no_llm": bool(options.no_llm),
            "selective_llm": bool(options.selective_llm),
            "no_daszek_push": True,
            "write_source_cursor": bool(options.write_source_cursor),
            "confirm_vps_node_b": bool(options.confirm_vps_node_b),
        },
    }


def build_redacted_samples(messages: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for message in messages[:limit]:
        sender = str(message.get("sender") or message.get("from") or "")
        samples.append(
            {
                "message_id": str(message.get("message_id") or ""),
                "thread_id": str(message.get("thread_id") or ""),
                "date": str(message.get("date") or ""),
                "sender": _redact_private_text(sender),
                "subject": _redact_private_text(str(message.get("subject") or "")),
                "snippet": _redact_private_text(str(message.get("snippet") or ""))[:240],
                "labels": list(message.get("labels") or []),
                "attachment_count": int(message.get("attachment_count") or 0),
                "direction": str(message.get("direction") or ""),
            }
        )
    return samples


def infer_mailbox(profile: dict[str, Any]) -> str:
    return str(profile.get("email") or profile.get("emailAddress") or profile.get("mailbox") or "unknown").strip() or "unknown"


def build_idempotency_key(*, mailbox: str, thread_id: str, message_id: str) -> str:
    return f"gmail:{INGEST_MODE}:{mailbox or 'unknown'}:{thread_id or 'no-thread'}:{message_id or 'no-message'}"


def _validate_options(options: GmailHistoricalBootstrapOptions) -> None:
    if options.gmail_source != "google_api":
        raise ValueError("gmail-bootstrap-history requires --gmail-source google_api for metadata-only safety.")
    if options.metadata_only and options.fetch_body:
        raise ValueError("--metadata-only conflicts with --fetch-body.")
    if options.metadata_only and options.fetch_attachments_content:
        raise ValueError("--metadata-only conflicts with --fetch-attachments-content.")
    if options.fetch_attachments_content and not options.fetch_attachments_metadata:
        raise ValueError("--fetch-attachments-content requires --fetch-attachments-metadata.")
    if options.fetch_attachments_content and int(options.max_attachment_bytes or 0) <= 0:
        raise ValueError("--fetch-attachments-content requires --max-attachment-bytes > 0.")
    if options.selective_llm and options.no_llm:
        raise ValueError("--selective-llm conflicts with --no-llm.")
    if options.write_source_cursor and options.dry_run:
        raise ValueError("--write-source-cursor=true is forbidden with --dry-run.")
    if _requires_vps_node_b_confirmation(options) and not options.confirm_vps_node_b:
        raise ValueError(LIVE_BOOTSTRAP_CONFIRMATION_ERROR)
    if options.limit <= 0:
        raise ValueError("--limit must be positive.")


def _requires_vps_node_b_confirmation(options: GmailHistoricalBootstrapOptions) -> bool:
    if options.finalize_source_cursor:
        return True
    return not bool(options.dry_run)


def build_execution_environment(*, settings: Settings | None, options: GmailHistoricalBootstrapOptions) -> dict[str, Any]:
    runtime_profile = str(getattr(settings, "runtime_profile", "") or options.runtime_profile or "").strip()
    metadata_only = bool(options.metadata_only or not options.fetch_body)
    if options.finalize_source_cursor:
        execution_mode = "source_cursor_finalization"
    elif options.dry_run:
        execution_mode = "dry_run"
    elif metadata_only:
        execution_mode = "metadata_scan"
    else:
        execution_mode = "bounded_backfill"
    return {
        "execution_mode": execution_mode,
        "hostname": socket.gethostname(),
        "cwd": str(Path.cwd()),
        "runtime_profile": runtime_profile,
        "dry_run": bool(options.dry_run),
        "metadata_only": metadata_only,
        "confirm_vps_node_b": bool(options.confirm_vps_node_b),
        "llm_enabled": bool(options.selective_llm and not options.no_llm),
        "daszek_push_enabled": False,
        "pid": os.getpid(),
    }


def _fetch_profile(settings: Settings, *, options: GmailHistoricalBootstrapOptions, profile_fetcher: ProfileFetchFn) -> dict[str, Any]:
    return profile_fetcher(settings, model=options.model, verbose=options.verbose, gmail_source=options.gmail_source)


def _safe_fetch_source_cursor(runtime: Any | None, cursor_scope: str) -> dict[str, Any] | None:
    if runtime is None:
        return None
    try:
        return runtime.store.fetch_source_cursor(SOURCE_KIND, cursor_scope or "default")
    except Exception:  # noqa: BLE001
        return None


def _labels_allowed(message: dict[str, Any], *, include: tuple[str, ...], exclude: tuple[str, ...]) -> bool:
    labels = {str(item).strip().lower() for item in message.get("labels") or []}
    include_set = {item.strip().lower() for item in include if item.strip()}
    exclude_set = {item.strip().lower() for item in exclude if item.strip()}
    if include_set and not labels.intersection(include_set):
        return False
    if exclude_set and labels.intersection(exclude_set):
        return False
    return True


def _normalize_attachment_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw = message.get("attachment_parts") or message.get("attachments") or []
    parts: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("filename") or item.get("file_name") or "").strip()
                if not name:
                    continue
                parts.append(
                    {
                        "name": name,
                        "filename": name,
                        "file_name": name,
                        "mime_type": str(item.get("mime_type") or item.get("mimeType") or "").strip(),
                        "attachment_id": str(item.get("attachment_id") or item.get("attachmentId") or item.get("storage_ref") or "").strip(),
                        "size_bytes": int(item.get("size_bytes") or item.get("size") or 0),
                    }
                )
            elif str(item or "").strip():
                name = str(item).strip()
                parts.append({"name": name, "filename": name, "file_name": name, "mime_type": "", "attachment_id": "", "size_bytes": 0})
    return parts


def _attachment_bytes(parts: list[dict[str, Any]]) -> int:
    return sum(int(item.get("size_bytes") or item.get("size") or 0) for item in parts if isinstance(item, dict))


def _message_text(message: dict[str, Any]) -> str:
    return " ".join(
        str(part or "").strip().lower()
        for part in (
            message.get("subject"),
            message.get("snippet"),
            message.get("sender"),
            message.get("from"),
            " ".join(str(item) for item in message.get("attachment_names") or []),
        )
        if str(part or "").strip()
    )


def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
    return any(needle in value for needle in needles)


def _looks_like_real_customer(sender: str) -> bool:
    sender = sender.strip().lower()
    if not sender or "@" not in sender:
        return False
    return not any(token in sender for token in (*SYSTEM_SENDER_TERMS, "newsletter", "notification", "system"))


def _counter_top_items(counter: Counter[str], *, limit: int = 20) -> list[dict[str, Any]]:
    return [{"value": key, "count": count} for key, count in counter.most_common(limit)]


def _redacted_sender_key(value: str) -> str:
    email = _first_email(value)
    if "@" not in email:
        return _redact_private_text(value)[:120]
    local, domain = email.split("@", 1)
    if not domain:
        return "<email>"
    return f"{local[:2]}***@{domain}"


def _domain_from_sender(value: str) -> str:
    email = _first_email(value)
    if "@" not in email:
        return ""
    return email.split("@", 1)[1].lower()


def _is_bootstrap_signal_row(row: dict[str, Any]) -> bool:
    if str(row.get("created_by_runtime") or "") == BOOTSTRAP_RUNTIME:
        return True
    source_ref = row.get("source_ref_json") or {}
    if isinstance(source_ref, dict) and str(source_ref.get("ingest_mode") or "") == INGEST_MODE:
        return True
    payload = row.get("payload_json") or {}
    if isinstance(payload, dict):
        snapshot = payload.get("snapshot") or {}
        if not isinstance(snapshot, dict):
            return False
        source_message = snapshot.get("source_message") or {}
        if not isinstance(source_message, dict):
            source_message = {}
        return str(snapshot.get("ingest_mode") or source_message.get("ingest_mode") or "") == INGEST_MODE
    return False


def _logical_bootstrap_signal_key(row: dict[str, Any]) -> str:
    source_ref = row.get("source_ref_json") or {}
    if not isinstance(source_ref, dict):
        source_ref = {}
    payload = row.get("payload_json") or {}
    if not isinstance(payload, dict):
        payload = {}
    snapshot = payload.get("snapshot") or {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    source_message = snapshot.get("source_message") or {}
    if not isinstance(source_message, dict):
        source_message = {}
    attachment = payload.get("attachment") or {}
    if not isinstance(attachment, dict):
        attachment = {}
    message_id = str(source_ref.get("message_id") or source_message.get("message_id") or payload.get("message_id") or "").strip()
    thread_id = str(source_ref.get("thread_id") or source_message.get("thread_id") or payload.get("thread_id") or "").strip()
    signal_kind = str(row.get("signal_kind") or "").strip()
    if signal_kind == "gmail_attachment_observed":
        attachment_index = str(source_ref.get("attachment_index") or "").strip()
        filename = str(source_ref.get("filename") or attachment.get("name") or attachment.get("filename") or attachment.get("file_name") or "").strip().lower()
        mime_type = str(source_ref.get("mime_type") or attachment.get("mime_type") or attachment.get("mimeType") or "").strip().lower()
        size_bytes = str(source_ref.get("size_bytes") or attachment.get("size_bytes") or attachment.get("size") or "").strip()
        return "|".join((signal_kind, thread_id, message_id, attachment_index, filename, mime_type, size_bytes))
    return "|".join((signal_kind, thread_id, message_id))


def _first_email(value: str) -> str:
    match = re.search(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", value or "", flags=re.IGNORECASE)
    return match.group(0).lower() if match else str(value or "").strip().lower()


def _gmail_query_date(value: str) -> str:
    raw = value.strip()
    if not raw:
        return raw
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y/%m/%d")
        except ValueError:
            continue
    return raw.replace("-", "/")


def _scan_window(options: GmailHistoricalBootstrapOptions) -> str:
    if options.after or options.before:
        return f"{options.after or '*'}..{options.before or '*'}"
    if options.days_back:
        start = datetime.now(timezone.utc) - timedelta(days=int(options.days_back))
        return f"{start.date().isoformat()}..now"
    return "query-bounded"


def _case_family_from_candidate(candidate: dict[str, Any]) -> str:
    reasons = set(candidate.get("priority_reasons") or [])
    if reasons.intersection({"active_lead_or_offer", "offer_or_heat_pump"}):
        return "heat_pump_offer"
    if "service" in reasons:
        return "service"
    if "complaint" in reasons:
        return "complaint"
    if "customer_or_finance_document" in reasons:
        return "documents_finance"
    return "general_admin"


def _should_selectively_enrich(
    options: GmailHistoricalBootstrapOptions,
    llm_budget: dict[str, Any],
    candidate: dict[str, Any],
    snapshot: dict[str, Any],
) -> bool:
    if not options.selective_llm or options.no_llm or not candidate.get("candidate"):
        return False
    max_calls = int(options.max_llm_calls or 0)
    if max_calls <= 0 or int(llm_budget.get("calls_used") or 0) >= max_calls:
        llm_budget["skipped"] = int(llm_budget.get("skipped") or 0) + 1
        return False
    thread_id = str((snapshot.get("source_message") or {}).get("thread_id") or "")
    per_thread = dict(llm_budget.get("per_thread") or {})
    if int(per_thread.get(thread_id, 0)) >= int(options.max_llm_calls_per_thread or 1):
        llm_budget["skipped"] = int(llm_budget.get("skipped") or 0) + 1
        return False
    reasons = set(candidate.get("priority_reasons") or [])
    return bool(reasons.intersection({"active_lead_or_offer", "service", "complaint", "has_attachments", "multi_reply_thread"}))


def _run_selective_enrichment(
    *,
    llm_enricher: LlmEnricherFn | None,
    snapshot: dict[str, Any],
    candidate: dict[str, Any],
    llm_budget: dict[str, Any],
) -> dict[str, Any] | None:
    if llm_enricher is None:
        llm_budget["skipped"] = int(llm_budget.get("skipped") or 0) + 1
        return None
    thread_id = str((snapshot.get("source_message") or {}).get("thread_id") or "")
    try:
        enriched = llm_enricher(snapshot, candidate)
    except Exception as exc:  # noqa: BLE001
        llm_budget["errors"] = int(llm_budget.get("errors") or 0) + 1
        llm_budget.setdefault("last_error", sanitize_text(str(exc))[:500])
        return None
    per_thread = dict(llm_budget.get("per_thread") or {})
    per_thread[thread_id] = int(per_thread.get(thread_id, 0)) + 1
    llm_budget["per_thread"] = per_thread
    llm_budget["calls_used"] = int(llm_budget.get("calls_used") or 0) + 1
    return enriched if isinstance(enriched, dict) else None


def _redact_private_text(value: str) -> str:
    text = sanitize_text(value)
    text = re.sub(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", "<email>", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:\+?\d[\s\-]?){7,}\b", "<phone>", text)
    return text


def stable_digest(payload: Any) -> str:
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


__all__ = [
    "GmailHistoricalBootstrapOptions",
    "LIVE_BOOTSTRAP_CONFIRMATION_ERROR",
    "SOURCE_CURSOR_NOT_FINALIZED",
    "build_execution_environment",
    "build_bootstrap_query",
    "build_idempotency_key",
    "classify_bootstrap_candidate",
    "finalize_gmail_bootstrap_source_cursor",
    "run_gmail_historical_bootstrap",
    "select_bootstrap_candidates",
]
