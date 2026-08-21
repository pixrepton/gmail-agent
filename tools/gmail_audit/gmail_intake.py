"""Local Gmail Intake Intelligence runner in safe shadow mode."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import json
import os
import sys

from log_config import get_logger

logger = get_logger("gmail_intake")
import time
from datetime import datetime
from pathlib import Path
from requests import RequestException
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

from artifact_contracts import (
    RUN_ARTIFACT_FILENAMES,
    build_run_artifact_paths,
    build_run_checkpoint as build_checkpoint_record,
    build_run_manifest as build_manifest_record,
    build_run_summary_record as build_summary_record,
    build_validation_result,
    empty_doctor_summary,
    empty_preflight_summary,
    empty_run_summary,
)
from artifact_io import append_jsonl, read_json as load_json, read_jsonl as load_jsonl, write_json
from action_planner import plan_actions as build_action_plan_result
from attachment_content_extraction import inspect_docling_runtime, inspect_ocr_runtime
from document_parse_adapters import inspect_unstructured_runtime
from business_context import build_business_context_bundle
from business_reasoner import (
    build_skipped_business_reasoning,
    run_business_reasoning as run_shadow_business_reasoning,
)
from attachment_intelligence import build_attachment_intelligence, refresh_attachment_intelligence_with_intake_context
from automation_gates import build_automation_policy
from case_guidance_reasoner import (
    build_case_guidance_prompt_input,
    build_skipped_case_guidance,
    fallback_case_guidance,
    fetch_remote_state_for_guidance,
    run_case_guidance_reasoning,
)
from case_intelligence import (
    apply_hot_state_to_case_intelligence,
    build_case_intelligence as build_case_intelligence_result,
    merge_case_guidance_into_intelligence,
    validate_case_intelligence_result,
)
from case_linker import build_no_link_case_result, link_case as run_case_linker
from case_family_boundary import filter_operational_feed_case_rows
from case_context_contract import build_case_context_pack_vnext, format_vnext_human_summary
from confidence_calibration import calibration_meta, merge_threshold_overrides
from confidence_review import apply_confidence_to_intelligence, build_confidence_domains, route_review
from cohort_proof import DEFAULT_GMAIL_COHORT_QUERY, build_cohort_run_record, write_cohort_run_record
from desk_maintenance import apply_maintenance_actions, collect_maintenance_preview, persist_maintenance_artifacts
from drive_client import build_google_drive_check
from drive_ingest_runtime import build_drive_ingest_runtime
from event_memory import EventLog, emit_case_intelligence, emit_signal_received
from thread_memory import build_thread_memory
from config import (
    ConfigError,
    Settings,
    document_intelligence_promote_facts_enabled,
    existing_env_candidates,
    load_settings,
)
from dash_preview import build_dash_preview
from dash_projection_v2 import build_v2_shadow_projection, validate_v2_shadow_projection
from daszek_client import DaszekClient, DaszekClientError
from daszek_push_policy import evaluate_live_push_policy
from exceptions import IntakeError
from policy_action_proposal import attach_policy_and_proposals, attach_policy_evaluation_to_results
from decision_pipeline import run_decision_pipeline
from understanding_output import (
    build_case_understanding_provenance,
    build_understanding_output,
    validate_understanding_invariants,
)
from mailbox_memory_health import (
    build_vector_retrieval_readiness_check,
    check_mailbox_memory_database,
    check_pgvector_extension,
)
from neo4j_pilot import build_case_context_neo4j_pilot_block, build_neo4j_pilot_connectivity_check
from observability_runtime import ObservabilityRuntime, build_otel_check


@contextmanager
def proof_telemetry_span(
    settings: Settings,
    *,
    command_name: str,
    case_id: str,
    proof_telemetry_dir: Path | None,
):
    """Optional local JSONL mirror for bounded mailbox/Drive CLI commands (shared directory across a proof chain)."""
    if proof_telemetry_dir is None:
        yield None
        return
    run_dir = Path(proof_telemetry_dir).expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    obs = ObservabilityRuntime(
        run_id=str(run_dir.name),
        run_dir=run_dir,
        command_name=command_name,
        enabled=bool(getattr(settings, "gmail_agent_otel_enabled", False)),
        local_mirror_enabled=bool(getattr(settings, "gmail_agent_otel_local_mirror_enabled", True)),
        service_name=str(getattr(settings, "otel_service_name", "") or "gmail-agent"),
        otlp_endpoint=str(getattr(settings, "otel_exporter_otlp_endpoint", "") or ""),
        otlp_headers=str(getattr(settings, "otel_exporter_otlp_headers", "") or ""),
    )
    with obs.span(f"{command_name}_bounded", case_id=str(case_id or ""), stage_name=command_name):
        yield obs
from exceptions import ExternalServiceError, IntakeError
from eval_shadow import (
    evaluate_annotations,
    summarize_validation_results,
    write_eval_details,
    write_eval_markdown_report,
    write_eval_summary,
    write_shadow_review_template,
)
from real_mail_intelligence_discovery import (
    RealMailDiscoveryOptions,
    run_real_mail_intelligence_discovery,
    write_real_mail_discovery_proof,
)
from central_llm_stage import (
    resolve_case_id,
    resolve_engagement_id,
    run_central_structured_stage,
)
from groq_client import (
    reset_structured_alternation_stage_slots_for_message,
    GroqClientError,
    is_auth_error_message,
    is_payload_too_large_error_message,
    is_rate_limit_error_message,
    request_structured_output,
)
from signal_extractor import build_signal_extraction_query, run_signal_extraction, signal_extraction_failed
from llm_contracts.intake_reasoning import IntakeReasoningResult
from inference_enrichment import enrich_snapshot_for_inference, envelopes_for_telemetry
from intake_payload import (
    build_inference_payload_variants,
    build_related_context_query,
    build_intake_reasoning_payload,
    build_source_snapshot,
    coerce_source_snapshot,
    render_system_prompt,
    render_task_prompt,
)
from intake_second_pass import (
    merge_intake_second_pass_supplement,
    run_intake_second_pass_supplement,
    should_run_intake_second_pass,
)
from intake_policy import (
    CHECK_STATUS_FAILED,
    CHECK_STATUS_OK,
    CHECK_STATUS_SKIPPED,
    DOCTOR_STATUS_FAILED,
    DOCTOR_STATUS_FAILED_AUTH,
    DOCTOR_STATUS_FAILED_CONFIG,
    DOCTOR_STATUS_OK,
    PREFLIGHT_STATUS_OK,
    OUTPUT_ORIGIN_GUARDRAILED_REVIEW,
    OUTPUT_ORIGIN_INVALID,
    OUTPUT_ORIGIN_NORMALIZED_VALID,
    OUTPUT_ORIGIN_RAW_VALID,
    OUTPUT_ORIGIN_REPAIRED_VALID,
    PREFLIGHT_STATUS_FAILED,
    PREFLIGHT_STATUS_WARNING,
    RUN_STATUS_ABORTED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_COMPLETED_WITH_ERRORS,
    RUN_STATUS_FAILED,
    RUN_STATUS_FAILED_AUTH,
    RUN_STATUS_FAILED_PREFLIGHT,
)
from intake_schema import (
    apply_contextual_guards,
    load_intake_schema,
    validate_case_link_result,
    validate_intake_result,
    validate_output_with_repair,
)
from mailbox_memory_runtime import build_mailbox_memory_runtime
from preclassifier import preclassify_snapshot as run_preclassifier
from reply_drafter import (
    annotate_reply_causal_observability,
    build_skipped_reply_draft,
    fallback_reply_drafter,
    run_reply_drafter as run_shadow_reply_drafter,
)
from redaction import sanitize_for_storage, sanitize_text
from gmail_intake_parser import build_parser, positive_int, non_negative_int, non_negative_float
from runtime_imports import (
    DEFAULT_GMAIL_SOURCE,
    build_google_auth_check,
    build_period_query,
    get_profile,
    search_email_metadata,
    get_thread_messages,
    normalize_gmail_source,
    read_email,
    run_google_direct_auth_check,
    search_emails,
)
from v2_runtime import (
    build_v2_ingest_payload,
    extract_v2_projection_from_stage_record,
    push_v2_projection_to_daszek,
)

logger = get_logger("gmail_intake")


def daszek_legacy_v2_push_allowed(settings: Settings, run_state: dict[str, Any]) -> bool:
    """PR-E: skip legacy v2 projection push when Daszek feed reads EngagementSnapshot.v2."""
    from daszek_engagement_feed import engagement_feed_source_enabled

    if engagement_feed_source_enabled(settings):
        return False
    manifest = run_state.get("manifest") or {}
    return bool(manifest.get("daszek_v2_push_enabled")) or bool(
        (run_state.get("runtime_controls") or {}).get("projection_proof")
    )


TOOL_DIR = Path(__file__).resolve().parent
RUNS_DIR = TOOL_DIR / "runs"
DEFAULT_SOURCE_MESSAGES_FILE = RUN_ARTIFACT_FILENAMES["source_messages"]


def _parse_args() -> argparse.Namespace:
    """Configure stdio, build and parse CLI arguments, print help if no command."""
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        raise SystemExit(2)
    return args


def _run_doctor_mode(args: argparse.Namespace) -> int:
    """Run the doctor diagnostic command."""
    return run_doctor_command(args)


def _run_backfill_mode(args: argparse.Namespace) -> int:
    """Run the mailbox memory backfill command."""
    return run_mailbox_memory_backfill_command(args)


def _run_proof_mode(args: argparse.Namespace) -> int:
    """Run the cohort proof command."""
    return run_cohort_proof_command(args)


def _run_pipeline(args: argparse.Namespace) -> int:
    """Run the default live intake pipeline."""
    return run_live_command(args)


def main() -> int:
    try:
        args = _parse_args()
    except SystemExit as e:
        return e.code

    try:
        if args.command == "doctor":
            return _run_doctor_mode(args)
        if args.command == "eval":
            return run_eval_command(args)
        if args.command == "real-mail-discovery":
            return run_real_mail_discovery_command(args)
        if args.command == "maintain-desk":
            return run_maintenance_command(args)
        if args.command == "replay-v2":
            return run_replay_v2_command(args)
        if args.command == "push-memory-v2":
            return run_push_memory_v2_command(args)
        if args.command == "memory-backfill":
            return _run_backfill_mode(args)
        if args.command == "gmail-bootstrap-history":
            return run_gmail_bootstrap_history_command(args)
        if args.command == "case-context":
            return run_case_context_command(args)
        if args.command == "cohort-proof":
            return _run_proof_mode(args)
        if args.command == "action-proposal-list":
            return run_action_proposal_list_command(args)
        if args.command == "action-proposal-approve":
            return run_action_proposal_approve_command(args)
        if args.command == "action-proposal-reject":
            return run_action_proposal_reject_command(args)
        if args.command == "action-proposal-execute":
            return run_action_proposal_execute_command(args)
        if args.command == "calendar-ingest":
            return run_calendar_ingest_command(args)
        if args.command == "calendar-context":
            return run_calendar_context_command(args)
        if args.command == "document-intelligence":
            return run_document_intelligence_command(args)
        if args.command == "eval-summary":
            return run_eval_summary_command(args)
        if args.command == "drive-ingest":
            return run_drive_ingest_command(args)
        if args.command == "drive-case-context":
            return run_drive_case_context_command(args)
        if args.command == "drive-graph-rebuild":
            return run_drive_graph_rebuild_command(args)
        if args.command == "signal-run":
            return run_signal_run_command(args)
        if args.command == "signal-worker":
            return run_signal_worker_command(args)
        if args.command == "event-spine-processor":
            return run_event_spine_processor_command(args)
        if args.command == "signal-replay":
            return run_signal_replay_command(args)
        if args.command == "signal-rebuild-case":
            return run_signal_rebuild_case_command(args)
        if args.command == "agent-mcp-serve":
            return run_agent_mcp_serve_command(args)
        if args.command == "operator-feedback":
            return run_operator_feedback_command(args)
        if args.command == "daszek-bridge-drain":
            return run_daszek_bridge_drain_command(args)
        if args.command == "gmail-detect-changes":
            return run_gmail_detect_changes_command(args)
        if args.command == "drive-detect-changes":
            return run_drive_detect_changes_command(args)
        if args.command == "bizdict-extract":
            from business_dictionary.cli import run_extract_cli
            return run_extract_cli(args)
        if args.command == "bizdict-search":
            from business_dictionary.cli import run_search_cli
            return run_search_cli(args)
        if args.command == "bizdict-sync":
            from business_dictionary.cli import run_sync_cli
            return run_sync_cli(args)
        if args.command == "bizdict-outbox-process":
            from business_dictionary.cli import run_outbox_process_cli
            return run_outbox_process_cli(args)
        if args.command == "sla-watcher":
            from sla_watcher import sla_watcher_oneshot
            from config import load_settings
            settings = load_settings(require_groq=False, require_google=False)
            import time
            if args.loop:
                while True:
                    result = sla_watcher_oneshot(settings)
                    print(f"[SLA Watcher] {result}", file=sys.stderr)
                    time.sleep(900)
            else:
                result = sla_watcher_oneshot(settings)
                print(result)
                critical_count = result.get("violations", {}).get("critical", [])
                high_count = result.get("violations", {}).get("high", [])
                print(f"[SLA Watcher] Critical: {len(critical_count)}, High: {len(high_count)}", file=sys.stderr)
            return 0
        if args.command == "follow-up-guardian":
            from follow_up_guardian import follow_up_guardian_oneshot
            from config import load_settings
            settings = load_settings(require_groq=False, require_google=False)
            result = follow_up_guardian_oneshot(settings, limit=int(getattr(args, "limit", 200) or 200))
            print(result)
            print(
                f"[Follow-up Guardian] ok={result.get('ok')} checked={result.get('checked')} "
                f"proposed={result.get('proposed_count')}",
                file=sys.stderr,
            )
            return 0 if bool(result.get("ok")) else 1
        if args.command == "os-events-cleanup":
            from event_spine.query import cleanup_old_events
            from config import load_settings
            settings = load_settings(require_groq=False, require_google=False)
            db_url = str(getattr(settings, "mailbox_memory_database_url", "") or "")
            if not db_url:
                print("Database not configured.", file=sys.stderr)
                return 1
            result = cleanup_old_events(db_url, ttl_days=int(args.days or 30), dry_run=bool(args.dry_run))
            print(result)
            if args.dry_run:
                print(f"[DRY RUN] Would delete {result.get('ready_to_delete', 0)} events older than {args.days or 30} days.")
            else:
                print(f"[OK] Deleted {result.get('deleted', 0)} events. {result.get('remaining', 0)} remaining.")
            return 0
        if args.command == "rerun":
            return run_rerun_command(args)
        return _run_pipeline(args)
    except ConfigError as exc:
        logger.error("Config error in intake", extra={"x": {"step": "init", "error": str(exc)[:200]}})
        print(f"Config error: {sanitize_text(str(exc))}", file=sys.stderr)
        return 1
    except GroqClientError as exc:
        logger.error("LLM provider error in intake", extra={"x": {"step": "llm_reasoning", "error": str(exc)[:200]}})
        print(f"Groq error: {sanitize_text(str(exc))}", file=sys.stderr)
        return 1
    except DaszekClientError as exc:
        logger.error("Daszek API error in intake", extra={"x": {"step": "daszek", "error": str(exc)[:200]}})
        print(f"Daszek error: {sanitize_text(str(exc))}", file=sys.stderr)
        return 1
    except OSError as exc:
        logger.error("File/OS error in intake", extra={"x": {"step": "io", "error": str(exc)[:200]}})
        print(f"File error: {sanitize_text(str(exc))}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        # Avoid importing google_gmail_api at module load (import isolation tests block google.*).
        if exc.__class__.__name__ == "GoogleGmailApiError" and getattr(exc.__class__, "__module__", "") == "google_gmail_api":
            logger.error("Gmail API error in intake", extra={"x": {"step": "gmail_api", "error": str(exc)[:200]}})
            print(f"Gmail API error: {sanitize_text(str(exc))}", file=sys.stderr)
            return 1
        raise


# build_parser() imported from gmail_intake_parser
def attach_daszek_v2_manifest_from_settings(run_state: dict[str, Any], settings: Settings) -> None:
    """Copy Daszek v2 ingest, feed V3 auto-push, and operator-desk policy flags into the run manifest."""
    from agent_runtime.manifest import attach_agent_runtime_manifest

    attach_agent_runtime_manifest(run_state, settings)
    run_state["manifest"]["daszek_v2_push_enabled"] = bool(settings.daszek_v2_push_enabled)
    run_state["manifest"]["daszek_operational_feed_auto_push_enabled"] = bool(
        getattr(settings, "daszek_operational_feed_auto_push_enabled", False)
    )
    run_state["manifest"]["daszek_operational_feed_push_min_interval_sec"] = int(
        getattr(settings, "daszek_operational_feed_push_min_interval_sec", 60) or 60
    )
    run_state["manifest"]["daszek_v2_readback_enabled"] = bool(settings.daszek_v2_readback_enabled)
    run_state["manifest"]["daszek_v2_desk_relax_rejected"] = bool(
        getattr(settings, "daszek_v2_desk_relax_rejected", False)
    )
    run_state["manifest"]["daszek_v2_desk_include_ignore"] = bool(
        getattr(settings, "daszek_v2_desk_include_ignore", False)
    )


def run_live_command(args: argparse.Namespace) -> int:
    batch_items: list[Any] | None = None
    if args.command == "batch":
        batch_items = load_batch_items(args.batch_file)
    elif args.command == "shadow-run" and args.batch_file:
        batch_items = load_batch_items(args.batch_file)

    requires_google = _requires_google_token(args.command, batch_items=batch_items)
    settings = load_settings(
        require_groq=True,
        require_google=requires_google,
    )
    from gmail_ingress_guard import enforce_legacy_cli_ingress_allowed

    enforce_legacy_cli_ingress_allowed(settings, command=str(args.command or ""))
    schema = load_intake_schema(args.schema_path)
    instructions = render_system_prompt()

    selector = build_selector(args, batch_items=batch_items)
    run_id = make_run_id(args.command)
    run_dir = RUNS_DIR / run_id
    mailbox = infer_mailbox_from_snapshots(batch_items, fallback="unknown") if batch_items is not None and not requires_google else "unknown"

    run_state = init_run_state(
        run_id=run_id,
        run_dir=run_dir,
        command=args.command,
        selector=selector,
        mailbox=mailbox,
        model=args.model or settings.groq_model,
        schema_path=args.schema_path,
        source_run=None,
        push_daszek=args.push_daszek,
        runtime_controls={
            "keep_going": bool(args.keep_going),
            "timebox_seconds": int(args.timebox_seconds),
            "max_failures": int(args.max_failures),
            "max_consecutive_failures": int(args.max_consecutive_failures),
            "attachments_metadata_only": bool(getattr(args, "attachments_metadata_only", False)),
            "llm_inter_item_delay_seconds": float(getattr(args, "llm_inter_item_delay_seconds", 0.0) or 0.0),
            "projection_proof": bool(getattr(args, "projection_proof", False)),
        },
    )
    attach_observability_runtime(run_state, settings, command_name=args.command)
    run_state["manifest"]["gmail_source"] = normalize_gmail_source(args.gmail_source)
    attach_daszek_v2_manifest_from_settings(run_state, settings)
    run_state["manifest"]["signal_runtime_mode"] = str(getattr(settings, "signal_runtime_mode", "active") or "active")
    write_json(run_state["manifest_path"], run_state["manifest"])
    annotate_env_metadata(run_state, settings)
    with observed_run_span(
        run_state,
        "preflight",
        stage_name="preflight",
        extra={"gmail_source": normalize_gmail_source(args.gmail_source)},
    ):
        preflight_ok = perform_run_preflight(
            run_state=run_state,
            settings=settings,
            require_google=requires_google,
            check_daszek=bool(
                args.push_daszek
                or settings.daszek_v2_push_enabled
                or getattr(settings, "daszek_operational_feed_auto_push_enabled", False)
            ),
            model=args.model,
            verbose=args.verbose,
            gmail_source=args.gmail_source,
        )
    if not preflight_ok:
        return finalize_run(run_state)

    mailbox_memory_runtime = build_mailbox_memory_runtime(settings)
    if bool(getattr(settings, "signal_runtime_enabled", False)) and mailbox_memory_runtime is None:
        raise ConfigError(
            "Signal runtime requires durable mailbox/shared memory storage. "
            "Configure MAILBOX_MEMORY_DATABASE_URL and MAILBOX_MEMORY_STAGE_MODE before using SIGNAL_RUNTIME_MODE=shadow|active. "
            "Next check: python tools/gmail_audit/gmail_intake.py doctor --skip-gmail --verbose"
        )
    if mailbox_memory_runtime is not None:
        mailbox_memory_runtime.bootstrap()
        run_state["mailbox_memory_runtime"] = mailbox_memory_runtime
        run_state["manifest"]["mailbox_memory"] = {
            "enabled": True,
            "stage_mode": settings.mailbox_memory_stage_mode,
            "blob_root": str(settings.mailbox_memory_blob_root),
            "database_url_configured": bool(settings.mailbox_memory_database_url),
            "allowlist_size": len(settings.mailbox_memory_stage_allowlist),
        }
    else:
        run_state["manifest"]["mailbox_memory"] = {
            "enabled": False,
            "stage_mode": settings.mailbox_memory_stage_mode,
            "blob_root": str(settings.mailbox_memory_blob_root),
            "database_url_configured": bool(settings.mailbox_memory_database_url),
            "allowlist_size": len(settings.mailbox_memory_stage_allowlist),
        }
    write_json(run_state["manifest_path"], run_state["manifest"])

    if batch_items is not None:
        write_json(run_state["artifacts"]["selection"], {"selector": selector, "items": sanitize_for_storage(batch_items)})
        with observed_run_span(run_state, "batch_processing", stage_name="selection"):
            process_batch_items(
                settings=settings,
                schema=schema,
                instructions=instructions,
                run_state=run_state,
                batch_items=batch_items,
                model=args.model,
                verbose=args.verbose,
                context_limit=args.context_limit,
                keep_going=args.keep_going,
                gmail_source=args.gmail_source,
            )
        return finalize_run(run_state)

    if args.command == "message":
        selected_items = [{"message_id": args.message_id}]
        write_json(run_state["artifacts"]["selection"], {"selector": selector, "selected_messages": selected_items})
        with observed_run_span(run_state, "message_processing", stage_name="selection", message_id=str(args.message_id or "")):
            process_live_selection(
                settings=settings,
                schema=schema,
                instructions=instructions,
                run_state=run_state,
                selected_items=selected_items,
                model=args.model,
                verbose=args.verbose,
                context_limit=args.context_limit,
                keep_going=args.keep_going,
                gmail_source=args.gmail_source,
            )
        return finalize_run(run_state)

    query = build_period_query(args.query, days=args.days)
    selector["query"] = query
    run_state["manifest"]["selector"] = sanitize_for_storage(selector)
    write_json(run_state["manifest_path"], run_state["manifest"])

    with observed_run_span(run_state, "period_selection", stage_name="selection"):
        selected_items = select_live_period_items(
            settings=settings,
            run_state=run_state,
            selector=selector,
            query=query,
            limit=args.limit,
            page_size=args.page_size,
            model=args.model,
            verbose=args.verbose,
            gmail_source=args.gmail_source,
        )
    if not selected_items:
        mark_stop_reason(
            run_state,
            reason="no_items_selected",
            details={"stage": "selection", "query": query},
        )
        return finalize_run(run_state)

    with observed_run_span(run_state, "period_processing", stage_name="selection"):
        process_live_selection(
            settings=settings,
            schema=schema,
            instructions=instructions,
            run_state=run_state,
            selected_items=selected_items,
            model=args.model,
            verbose=args.verbose,
            context_limit=args.context_limit,
            keep_going=args.keep_going,
            gmail_source=args.gmail_source,
        )
    return finalize_run(run_state)


def run_rerun_command(args: argparse.Namespace) -> int:
    source_run_dir = resolve_run_dir(run_id=args.run_id, run_dir=args.run_dir)
    source_messages_path = source_run_dir / args.source_file
    frozen_snapshots = load_jsonl(source_messages_path)
    if not frozen_snapshots:
        raise OSError(f"No frozen source snapshots found in {source_messages_path}.")

    settings = load_settings(require_groq=True, require_google=False)
    schema = load_intake_schema(args.schema_path)
    instructions = render_system_prompt()

    run_id = make_run_id("rerun")
    run_dir = RUNS_DIR / run_id
    selector = {
        "type": "rerun",
        "source_run": source_run_dir.name,
        "source_file": args.source_file,
        "items": len(frozen_snapshots),
        "gmail_source": normalize_gmail_source(args.gmail_source),
    }
    mailbox = infer_mailbox_from_snapshots(frozen_snapshots, fallback="unknown")

    run_state = init_run_state(
        run_id=run_id,
        run_dir=run_dir,
        command="rerun",
        selector=selector,
        mailbox=mailbox,
        model=args.model or settings.groq_model,
        schema_path=args.schema_path,
        source_run=str(source_run_dir),
        push_daszek=args.push_daszek,
        runtime_controls={
            "keep_going": bool(args.keep_going),
            "timebox_seconds": int(args.timebox_seconds),
            "max_failures": int(args.max_failures),
            "max_consecutive_failures": int(args.max_consecutive_failures),
            "attachments_metadata_only": bool(getattr(args, "attachments_metadata_only", False)),
            "llm_inter_item_delay_seconds": float(getattr(args, "llm_inter_item_delay_seconds", 0.0) or 0.0),
            "projection_proof": bool(getattr(args, "projection_proof", False)),
        },
    )
    attach_observability_runtime(run_state, settings, command_name="rerun")
    run_state["manifest"]["gmail_source"] = normalize_gmail_source(args.gmail_source)
    attach_daszek_v2_manifest_from_settings(run_state, settings)
    write_json(run_state["manifest_path"], run_state["manifest"])
    annotate_env_metadata(run_state, settings)
    with observed_run_span(run_state, "preflight", stage_name="preflight"):
        preflight_ok = perform_run_preflight(
            run_state=run_state,
            settings=settings,
            require_google=False,
            check_daszek=bool(
                args.push_daszek
                or settings.daszek_v2_push_enabled
                or getattr(settings, "daszek_operational_feed_auto_push_enabled", False)
            ),
            model=args.model,
            verbose=args.verbose,
            gmail_source=args.gmail_source,
        )
    if not preflight_ok:
        return finalize_run(run_state)
    run_state["summary"]["items_selected"] = len(frozen_snapshots)
    write_json(
        run_state["artifacts"]["selection"],
        {"selector": selector, "source_run_dir": str(source_run_dir), "frozen_snapshots": len(frozen_snapshots)},
    )
    update_checkpoint(run_state)

    with observed_run_span(run_state, "rerun_processing", stage_name="selection"):
        process_frozen_snapshots(
            settings=settings,
            schema=schema,
            instructions=instructions,
            run_state=run_state,
            snapshots=frozen_snapshots,
            model=args.model,
            verbose=args.verbose,
            keep_going=args.keep_going,
        )
    return finalize_run(run_state)


def run_eval_command(args: argparse.Namespace) -> int:
    run_dir = resolve_run_dir(run_id=args.run_id, run_dir=args.run_dir)
    outputs = load_jsonl(run_dir / RUN_ARTIFACT_FILENAMES["intake_outputs"])
    stage_records = load_jsonl(run_dir / RUN_ARTIFACT_FILENAMES["stage_records"], allow_missing=True)
    validation_rows = load_jsonl(run_dir / RUN_ARTIFACT_FILENAMES["validation_results"], allow_missing=True)
    annotations_path = Path(args.annotations) if args.annotations else run_dir / RUN_ARTIFACT_FILENAMES["human_annotations"]
    if not annotations_path.is_file():
        raise OSError(f"Missing annotations file: {annotations_path}")

    summary, details = evaluate_annotations(outputs, annotations_path, stage_records=stage_records)
    if validation_rows:
        summary["validation_summary"] = summarize_validation_results(validation_rows)
    write_eval_summary(run_dir, summary)
    write_eval_details(run_dir, details)
    write_eval_markdown_report(run_dir, summary)

    manifest_path = run_dir / RUN_ARTIFACT_FILENAMES["manifest"]
    if manifest_path.is_file():
        manifest = load_json(manifest_path)
        manifest["last_eval_at"] = datetime.now().astimezone().isoformat()
        manifest["annotations_path"] = str(annotations_path)
        write_json(manifest_path, manifest)

    _emit_json(summary)
    print(f"[info] Evaluation details: {run_dir / RUN_ARTIFACT_FILENAMES['eval_details']}", file=sys.stderr)
    return 0


def run_real_mail_discovery_command(args: argparse.Namespace) -> int:
    """Run file-only real-mail intelligence discovery without side effects."""

    input_path = Path(getattr(args, "input")).expanduser().resolve()
    base_output_dir = Path(getattr(args, "output_dir")).expanduser().resolve()
    options = RealMailDiscoveryOptions(
        input_path=input_path,
        output_dir=base_output_dir,
        run_id=str(getattr(args, "run_id", "") or ""),
        min_cases=int(getattr(args, "min_cases", 10) or 10),
        max_cases=int(getattr(args, "max_cases", 15) or 15),
        allow_small_sample=bool(getattr(args, "allow_small_sample", False)),
    )
    summary = run_real_mail_intelligence_discovery(options)
    run_output_dir = base_output_dir / str(summary.get("run_id") or "real-mail-discovery")
    artifact_paths = write_real_mail_discovery_proof(summary, output_dir=run_output_dir)
    ok = summary.get("status") in {"completed", "completed_small_sample"}
    _emit_json({"ok": ok, "artifact_paths": artifact_paths, "summary": summary})
    return 0 if ok else 1


def run_maintenance_command(args: argparse.Namespace) -> int:
    settings = load_settings(require_groq=False, require_google=False)
    started_at = datetime.now().astimezone()
    run_id = make_maintenance_run_id()
    run_dir = RUNS_DIR / run_id
    observability = ObservabilityRuntime(
        run_id=run_id,
        run_dir=run_dir,
        command_name="maintain-desk",
        enabled=bool(getattr(settings, "gmail_agent_otel_enabled", False)),
        local_mirror_enabled=bool(getattr(settings, "gmail_agent_otel_local_mirror_enabled", True)),
        service_name=str(getattr(settings, "otel_service_name", "") or "gmail-agent"),
        otlp_endpoint=str(getattr(settings, "otel_exporter_otlp_endpoint", "") or ""),
        otlp_headers=str(getattr(settings, "otel_exporter_otlp_headers", "") or ""),
    )
    client = DaszekClient(settings, observability_runtime=observability)
    client.login()
    manifest = {
        "run_id": run_id,
        "command": "maintain-desk",
        "operation": "apply" if args.apply else "preview",
        "mode": "run_invoked",
        "status": "running",
        "mailbox": "daszek_v2",
        "selector": sanitize_for_storage(
            {
                "case_id": str(args.case_id or "").strip(),
                "note_id": str(args.note_id or "").strip(),
                "limit": int(args.limit) if args.limit else None,
            }
        ),
        "started_at": started_at.isoformat(),
        "completed_at": "",
        "env_source": str(settings.env_path.resolve()) if settings.env_path else "environment_only",
        "telemetry": {},
    }

    with observability.span("maintenance_preview", stage_name="maintenance_preview", case_id=str(args.case_id or "").strip()):
        preview = collect_maintenance_preview(
            client,
            case_id=str(args.case_id or "").strip(),
            note_id=str(args.note_id or "").strip(),
            limit=int(args.limit) if args.limit else None,
            now=started_at,
        )
    apply_result = None
    exit_code = 0
    if args.apply:
        with observability.span("maintenance_apply", stage_name="maintenance_apply", case_id=str(args.case_id or "").strip()):
            apply_result = apply_maintenance_actions(client, run_id=run_id, preview=preview, now=started_at)
        failed = int((apply_result.get("summary") or {}).get("apply_failed_count") or 0)
        manifest["status"] = "completed_with_errors" if failed else "completed"
        exit_code = 1 if failed else 0
    else:
        manifest["status"] = "completed"

    telemetry_summary = observability.summary()
    manifest["telemetry"] = sanitize_for_storage(telemetry_summary)
    manifest["completed_at"] = datetime.now().astimezone().isoformat()
    preview["summary"].update(telemetry_summary)
    if apply_result:
        apply_result["summary"].update(telemetry_summary)
    persist_maintenance_artifacts(run_dir, preview=preview, apply_result=apply_result, manifest=manifest)

    summary = dict((apply_result or preview).get("summary") or {})
    summary["run_id"] = run_id
    summary["run_dir"] = str(run_dir)
    if args.verbose:
        summary["manifest"] = manifest
    _emit_json(summary)
    print(f"[info] Maintenance run directory: {run_dir}", file=sys.stderr)
    return exit_code


def run_replay_v2_command(args: argparse.Namespace) -> int:
    source_run_dir = resolve_run_dir(run_id=args.run_id, run_dir=args.run_dir)
    source_file = source_run_dir / args.source_file
    stage_records = load_jsonl(source_file)
    if not stage_records:
        raise OSError(f"No stage records found in {source_file}.")

    settings = load_settings(require_groq=False, require_google=False)
    replay_run_id = make_run_id("replay-v2")
    replay_run_dir = RUNS_DIR / replay_run_id
    observability = ObservabilityRuntime(
        run_id=replay_run_id,
        run_dir=replay_run_dir,
        command_name="replay-v2",
        enabled=bool(getattr(settings, "gmail_agent_otel_enabled", False)),
        local_mirror_enabled=bool(getattr(settings, "gmail_agent_otel_local_mirror_enabled", True)),
        service_name=str(getattr(settings, "otel_service_name", "") or "gmail-agent"),
        otlp_endpoint=str(getattr(settings, "otel_exporter_otlp_endpoint", "") or ""),
        otlp_headers=str(getattr(settings, "otel_exporter_otlp_headers", "") or ""),
    )
    client = DaszekClient(settings, observability_runtime=observability)
    client.login()

    replayed = 0
    failed = 0
    skipped = 0
    attempted = 0
    for stage_record in stage_records:
        if not isinstance(stage_record, dict):
            continue
        message_id = str(stage_record.get("message_id") or "").strip()
        if args.message_id and message_id != args.message_id:
            continue
        projection = extract_v2_projection_from_stage_record(stage_record)
        if projection is None:
            skipped += 1
            continue

        payload = build_v2_ingest_payload(
            run_id=source_run_dir.name,
            message_key=message_id,
            v2_projection=projection,
        )
        attempted += 1
        try:
            with observability.span(
                "replay_item",
                stage_name="replay_item",
                message_id=message_id,
                signal_id=str((projection.get("signal_projection") or {}).get("signal_id") or ""),
                trace_id=str((projection.get("decision_trace") or {}).get("trace_id") or ""),
            ):
                result = client.push_v2_projection(payload)
        except DaszekClientError as exc:
            failed += 1
            if args.verbose:
                print(
                    json.dumps(
                        {
                            "message_id": message_id,
                            "status": "failed",
                            "error": sanitize_text(str(exc)),
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
        else:
            replayed += 1
            if args.verbose:
                print(
                    json.dumps(
                        {
                            "message_id": message_id,
                            "status": result.status,
                            "signal_id": result.signal_id,
                            "trace_id": result.trace_id,
                        },
                        ensure_ascii=False,
                    )
                )

        if args.limit and attempted >= args.limit:
            break

    summary = {
        "run_id": replay_run_id,
        "run_dir": str(replay_run_dir),
        "source_run_dir": str(source_run_dir),
        "source_file": str(source_file),
        "replayed": replayed,
        "failed": failed,
        "skipped": skipped,
        "attempted": attempted,
        "message_filter": args.message_id or "",
    }
    summary.update(observability.summary())
    _emit_json(summary)
    return 0 if failed == 0 else 1


def run_push_memory_v2_command(args: argparse.Namespace) -> int:
    settings = load_settings(require_groq=False, require_google=False)
    runtime = _require_mailbox_memory_runtime(settings)
    runtime.bootstrap()

    selected = runtime.store.fetch_any_message(order=str(getattr(args, "order", "") or "oldest"))
    if not isinstance(selected, dict) or not str(selected.get("message_id") or "").strip():
        raise OSError("No mailbox-memory message found to replay (mailbox_memory_messages is empty).")

    message_id = str(selected.get("message_id") or "").strip()
    run_id = make_run_id("push-memory-v2")
    intake_output = _minimal_intake_output_from_mailbox_memory_message(selected)
    stage_outputs: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "case_link_result": {"decision": "no_link"},
    }
    projection = build_v2_shadow_projection(intake_output, stage_outputs=stage_outputs, run_id=run_id)
    validate_v2_shadow_projection(projection)

    client = DaszekClient(settings)
    client.login()
    payload = build_v2_ingest_payload(run_id=run_id, message_key=message_id, v2_projection=projection)
    result = client.push_v2_projection(payload)

    _emit_json(
        {
            "ok": True,
            "command": "push-memory-v2",
            "run_id": run_id,
            "selected_message_id": message_id,
            "order": str(getattr(args, "order", "") or "oldest"),
            "daszek_status": result.status,
            "signal_id": result.signal_id,
            "trace_id": result.trace_id,
        }
    )
    return 0


def _minimal_intake_output_from_mailbox_memory_message(message_row: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic intake_output skeleton sufficient for v2 projection."""
    raw = message_row.get("raw_snapshot") if isinstance(message_row.get("raw_snapshot"), dict) else {}
    source_message = raw.get("source_message") if isinstance(raw.get("source_message"), dict) else {}

    message_id = str(message_row.get("message_id") or source_message.get("message_id") or "").strip()
    thread_id = str(message_row.get("thread_id") or source_message.get("thread_id") or "").strip()
    mailbox = str(message_row.get("mailbox") or raw.get("mailbox") or "").strip()
    subject = str(message_row.get("subject") or source_message.get("subject") or "").strip()
    snippet = str(message_row.get("snippet") or source_message.get("snippet") or "").strip()
    body = str(message_row.get("body_text") or source_message.get("body") or "").strip()
    received_at = str(message_row.get("received_at") or source_message.get("date") or raw.get("observed_at") or "").strip()

    # This is intentionally a "review" action to ensure the desk note is created (not suppressed).
    return {
        "schema_version": "1.0",
        "source": {
            "channel": "gmail",
            "mailbox": mailbox,
            "observed_at": raw.get("observed_at") or received_at,
        },
        "message": {
            "message_id": message_id,
            "thread_id": thread_id,
            "date": received_at,
            "subject": subject,
            "snippet": snippet,
            "body": body,
        },
        "thread": {"thread_id": thread_id},
        "decision": {"action": "review", "reason": "Replayed from mailbox-memory archive for operator visibility."},
        "primary_signal": {
            "code": "archival_replay",
            "name": "Archiwalny mail (replay)",
            "description": "Archiwalny mail odtworzony z mailbox-memory, aby pojawil sie na Daszku.",
            "business_significance": "Operator verification of projection + storage pipeline.",
        },
        "business_area": "operations",
        "case_assessment": {"case_family": "mail_case", "state_detected": "none", "state_change": {"detected": False}},
        "review": {"required": True, "flags": ["archival_replay"]},
        "confidence": {
            "signal_confidence": 0.3,
            "case_link_confidence": 0.0,
            "decision_confidence": 0.3,
            "extraction_confidence": 0.1,
        },
        "priority": "medium",
        "reason": "Minimal deterministic projection skeleton (no LLM) for replay.",
    }


def _load_message_ids_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    ids: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        ids.append(stripped)
    return ids


def run_mailbox_memory_backfill_command(args: argparse.Namespace) -> int:
    needs_google = not bool(args.run_id or args.run_dir)
    settings = load_settings(require_groq=False, require_google=needs_google)
    proof_dir = getattr(args, "proof_telemetry_dir", None)
    with proof_telemetry_span(
        settings,
        command_name="memory-backfill",
        case_id="",
        proof_telemetry_dir=proof_dir,
    ):
        return _run_mailbox_memory_backfill_command_impl(args, settings)


def _run_mailbox_memory_backfill_command_impl(args: argparse.Namespace, settings: Settings) -> int:
    runtime = _require_mailbox_memory_runtime(settings)
    runtime.bootstrap()

    snapshots: list[dict[str, Any]]
    source_label = "live_gmail"
    if args.run_id or args.run_dir:
        source_run_dir = resolve_run_dir(run_id=args.run_id, run_dir=args.run_dir)
        snapshots = [coerce_source_snapshot(item, mailbox_fallback="unknown") for item in load_jsonl(source_run_dir / RUN_ARTIFACT_FILENAMES["source_messages"])]
        source_label = str(source_run_dir)
    elif args.message_id:
        snapshots = _build_live_snapshots_for_memory_backfill(
            settings,
            message_ids=[args.message_id],
            model=args.model,
            verbose=args.verbose,
            context_limit=args.context_limit,
            gmail_source=args.gmail_source,
        )
    elif getattr(args, "message_ids_file", None):
        cohort_path = Path(args.message_ids_file).expanduser().resolve()
        if not cohort_path.is_file():
            raise OSError(f"Message ids file not found: {cohort_path}")
        cohort_ids = _load_message_ids_file(cohort_path)
        if not cohort_ids:
            raise OSError(f"No message ids in file: {cohort_path}")
        snapshots = _build_live_snapshots_for_memory_backfill(
            settings,
            message_ids=cohort_ids,
            model=args.model,
            verbose=args.verbose,
            context_limit=args.context_limit,
            gmail_source=args.gmail_source,
        )
        source_label = str(cohort_path)
    else:
        query = build_period_query(args.query, days=args.days)
        payload = search_emails(
            settings,
            query=query,
            max_results=args.limit,
            model=args.model,
            verbose=args.verbose,
            gmail_source=args.gmail_source,
        )
        message_ids = [extract_message_id(item) for item in payload.get("responses") or [] if extract_message_id(item)]
        snapshots = _build_live_snapshots_for_memory_backfill(
            settings,
            message_ids=message_ids[: args.limit],
            model=args.model,
            verbose=args.verbose,
            context_limit=args.context_limit,
            gmail_source=args.gmail_source,
        )

    attachment_fetcher = _default_attachment_fetcher(settings)
    results: list[dict[str, Any]] = []
    for snapshot in snapshots:
        seed = _build_memory_backfill_intake_seed(snapshot)
        context_bundle = build_context_bundle(snapshot)
        case_link_result = run_case_linker(snapshot, seed, context_bundle)
        ingest_result = runtime.ingest_message(
            snapshot=snapshot,
            intake_result=seed,
            case_link_result=case_link_result,
            attachment_fetcher=attachment_fetcher,
            attachment_max_bytes=int(getattr(settings, "attachment_extraction_max_bytes", 8_000_000) or 8_000_000),
            refresh_document_intelligence=bool(getattr(args, "refresh_document_intelligence", False)),
        )
        final_result = runtime.finalize_case(
            case_id=ingest_result.case_id,
            message_id=str((snapshot.get("source_message") or {}).get("message_id") or ""),
            thread_id=str((snapshot.get("source_message") or {}).get("thread_id") or ""),
            business_result={"recommended_next_action": "review_required", "recommended_action_reason": "memory_backfill"},
            reply_result={"draft_enabled": False, "drafts": []},
            action_plan_result={"primary_action": "hold", "why_this_action": "memory_backfill"},
            case_intelligence_result={},
        )
        results.append(
            {
                "message_id": str((snapshot.get("source_message") or {}).get("message_id") or ""),
                "case_id": final_result.case_id or ingest_result.case_id,
                "snapshot": final_result.snapshot or ingest_result.snapshot,
                "context_pack": (final_result.context_pack or ingest_result.context_pack).to_dict() if (final_result.context_pack or ingest_result.context_pack) else {},
                "warnings": list(ingest_result.warnings or []) + list(final_result.warnings or []),
            }
        )

    summary = {
        "source": source_label,
        "ingested_count": len(results),
        "refresh_document_intelligence": bool(getattr(args, "refresh_document_intelligence", False)),
        "items": results,
    }
    _emit_json(summary)
    return 0


def run_gmail_bootstrap_history_command(args: argparse.Namespace) -> int:
    from gmail_historical_bootstrap import (
        GmailHistoricalBootstrapOptions,
        LIVE_BOOTSTRAP_CONFIRMATION_ERROR,
        run_gmail_historical_bootstrap,
    )

    selective_llm = bool(getattr(args, "selective_llm", False))
    explicit_no_llm = getattr(args, "no_llm", None)
    no_llm = bool(explicit_no_llm) if explicit_no_llm is not None else not selective_llm
    mutating_requested = bool(
        getattr(args, "finalize_source_cursor", False)
        or not getattr(args, "dry_run", False)
    )
    if mutating_requested and not bool(getattr(args, "confirm_vps_node_b", False)):
        print(LIVE_BOOTSTRAP_CONFIRMATION_ERROR, file=sys.stderr)
        return 2
    settings = load_settings(require_groq=selective_llm and not no_llm, require_google=True)
    needs_runtime = bool(getattr(args, "finalize_source_cursor", False) or (getattr(args, "fetch_body", False) and not getattr(args, "dry_run", False)))
    runtime = _require_mailbox_memory_runtime(settings) if needs_runtime else build_mailbox_memory_runtime(settings)
    options = GmailHistoricalBootstrapOptions(
        run_id=str(getattr(args, "run_id", "") or ""),
        query=str(getattr(args, "query", "") or "to:me -in:spam -in:trash"),
        after=str(getattr(args, "after", "") or ""),
        before=str(getattr(args, "before", "") or ""),
        days_back=int(getattr(args, "days_back", 0) or 0),
        limit=int(getattr(args, "limit", 100) or 100),
        page_size=int(getattr(args, "page_size", 100) or 100),
        max_threads=int(getattr(args, "max_threads", 0) or 0),
        max_messages_per_thread=int(getattr(args, "max_messages_per_thread", 0) or 0),
        include_label=tuple(str(item).strip() for item in getattr(args, "include_label", []) or [] if str(item).strip()),
        exclude_label=tuple(str(item).strip() for item in getattr(args, "exclude_label", []) or [] if str(item).strip()),
        metadata_only=bool(getattr(args, "metadata_only", False)),
        fetch_body=bool(getattr(args, "fetch_body", False)),
        fetch_attachments_metadata=bool(getattr(args, "fetch_attachments_metadata", False)),
        fetch_attachments_content=bool(getattr(args, "fetch_attachments_content", False)),
        max_attachment_bytes=int(getattr(args, "max_attachment_bytes", 0) or 0),
        dry_run=bool(getattr(args, "dry_run", False)),
        no_llm=no_llm,
        selective_llm=selective_llm,
        max_llm_calls=int(getattr(args, "max_llm_calls", 0) or 0),
        max_llm_calls_per_thread=int(getattr(args, "max_llm_calls_per_thread", 1) or 1),
        max_consecutive_failures=int(getattr(args, "max_consecutive_failures", 0) or 0),
        timebox_seconds=int(getattr(args, "timebox_seconds", 0) or 0),
        no_daszek_push=True,
        proof_dir=getattr(args, "proof_dir", None),
        write_source_cursor=str(getattr(args, "write_source_cursor", "false") or "false").lower() == "true",
        finalize_source_cursor=bool(getattr(args, "finalize_source_cursor", False)),
        confirm_vps_node_b=bool(getattr(args, "confirm_vps_node_b", False)),
        bootstrap_run_id=str(getattr(args, "bootstrap_run_id", "") or ""),
        runtime_profile=str(getattr(args, "runtime_profile", "") or ""),
        cursor_scope=str(getattr(args, "cursor_scope", "default") or "default"),
        gmail_source=str(getattr(args, "gmail_source", DEFAULT_GMAIL_SOURCE) or DEFAULT_GMAIL_SOURCE),
        model=getattr(args, "model", None),
        verbose=bool(getattr(args, "verbose", False)),
    )

    llm_enricher = None
    if selective_llm and not no_llm:
        llm_enricher = _build_gmail_bootstrap_llm_enricher(settings, args)

    try:
        summary = run_gmail_historical_bootstrap(
            settings=settings,
            runtime=runtime,
            options=options,
            profile_fetcher=get_profile,
            metadata_searcher=search_email_metadata,
            body_fetcher=read_email,
            attachment_fetcher_factory=lambda: _default_attachment_fetcher(settings),
            llm_enricher=llm_enricher,
        )
    except ValueError as exc:
        print(sanitize_text(str(exc)), file=sys.stderr)
        return 2
    _emit_json(summary)
    return 0 if str(summary.get("status") or "") != "blocked" else 1


def _build_gmail_bootstrap_llm_enricher(settings: Settings, args: argparse.Namespace) -> Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]:
    schema = load_intake_schema(getattr(args, "schema_path", None))
    instructions = render_system_prompt()

    def _enrich(snapshot: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        preclassification = {
            "lane": "intake_llm",
            "reasons": ["historical_bootstrap_selective_llm", *list(candidate.get("priority_reasons") or [])[:5]],
            "confidence": 0.75,
        }
        context_bundle = build_context_bundle(snapshot)
        stage_config = {
            "settings": settings,
            "schema": schema,
            "instructions": instructions,
            "model": getattr(args, "model", None) or settings.groq_model,
            "verbose": bool(getattr(args, "verbose", False)),
            "snapshot": snapshot,
            "preclassification_result": preclassification,
            "lane_stage_plan": _build_lane_stage_plan(preclassification),
        }
        raw = run_intake_reasoning(snapshot, context_bundle, stage_config)
        validation_result = validate_intake_output(raw, stage_config)
        if not validation_result.get("is_valid") or not validation_result.get("intake_result_final"):
            validation = validation_result.get("validation")
            errors = getattr(validation, "errors", []) if validation is not None else []
            raise GroqClientError("Selective bootstrap LLM output failed validation: " + "; ".join(str(item) for item in errors[:3]))
        return sanitize_for_storage(validation_result["intake_result_final"])

    return _enrich


def _vnext_kwargs_from_ns(ns: argparse.Namespace) -> dict[str, Any]:
    kw: dict[str, Any] = {}
    for name in ("evidence_limit", "chunk_limit", "conflict_limit", "gap_limit"):
        v = int(getattr(ns, name, 0) or 0)
        if v > 0:
            kw[name] = v
    return kw


def run_case_context_command(args: argparse.Namespace) -> int:
    settings = load_settings(require_groq=False, require_google=False)
    proof_dir = getattr(args, "proof_telemetry_dir", None)
    case_id = str(args.case_id or "")
    with proof_telemetry_span(
        settings,
        command_name="case-context",
        case_id=case_id,
        proof_telemetry_dir=proof_dir,
    ):
        runtime = _require_mailbox_memory_runtime(settings)
        runtime.bootstrap()
        pack = runtime.get_context_pack(
            case_id=case_id,
            message_id=str(args.message_id or ""),
            query_text=str(args.query_text or ""),
        )
        if not pack.case_id:
            raise OSError("No mailbox-memory case found for the requested case_id/message_id.")
        if bool(getattr(args, "neo4j_project", False) or getattr(args, "neo4j_graph_aware", False)):
            pack.neo4j_pilot = build_case_context_neo4j_pilot_block(
                settings=settings,
                store=runtime.store,
                case_id=pack.case_id,
                context_pack=pack.to_dict(),
                project=bool(getattr(args, "neo4j_project", False)),
                graph_aware=bool(getattr(args, "neo4j_graph_aware", False)),
                max_hops=int(getattr(args, "neo4j_max_hops", 2) or 2),
                limit=int(getattr(args, "neo4j_limit", 10) or 10),
                anchor_mode=str(getattr(args, "neo4j_anchor_mode", "auto") or "auto"),
            )
        if bool(getattr(args, "human_summary", False)) and not bool(getattr(args, "vnext", False)):
            raise SystemExit("case-context: --human-summary requires --vnext.")
        use_vnext = bool(getattr(args, "vnext", False))
        if use_vnext:
            payload = build_case_context_pack_vnext(pack, **_vnext_kwargs_from_ns(args))
        else:
            payload = pack.to_dict()
        if use_vnext and bool(getattr(args, "human_summary", False)):
            print(format_vnext_human_summary(payload), end="")
        else:
            _emit_json(payload)
    return 0


def run_cohort_proof_command(args: argparse.Namespace) -> int:
    live_gmail = bool(getattr(args, "live_gmail_cohort", False))
    force_memory = bool(getattr(args, "existing_memory_only", False))
    needs_google = live_gmail and not force_memory
    if bool(getattr(args, "ingest_selected", False)) and not needs_google:
        raise SystemExit(
            "cohort-proof: --ingest-selected only applies with --live-gmail-cohort "
            "(memory-only default has no Gmail selection to ingest)."
        )
    settings = load_settings(require_groq=False, require_google=needs_google)
    runtime = _require_mailbox_memory_runtime(settings)
    runtime.bootstrap()
    run_id = str(getattr(args, "run_id", "") or "").strip() or make_run_id("cohort-proof")
    store = runtime.store

    gmail_items: list[dict[str, Any]] = []
    context_packs: list[Any] = []
    item_statuses: dict[str, dict[str, Any]] = {}

    if needs_google:
        payload = search_emails(
            settings,
            query=str(getattr(args, "query", DEFAULT_GMAIL_COHORT_QUERY) or DEFAULT_GMAIL_COHORT_QUERY),
            max_results=int(getattr(args, "gmail_limit", 100) or 100),
            model=getattr(args, "model", None),
            verbose=bool(getattr(args, "verbose", False)),
            gmail_source=str(getattr(args, "gmail_source", DEFAULT_GMAIL_SOURCE) or DEFAULT_GMAIL_SOURCE),
        )
        gmail_items = [item for item in payload.get("responses") or [] if isinstance(item, dict)]
        message_ids = [extract_message_id(item) for item in gmail_items if extract_message_id(item)]

        if bool(getattr(args, "ingest_selected", False)) and message_ids:
            snapshots = _build_live_snapshots_for_memory_backfill(
                settings,
                message_ids=message_ids[: int(getattr(args, "gmail_limit", 100) or 100)],
                model=getattr(args, "model", None),
                verbose=bool(getattr(args, "verbose", False)),
                context_limit=int(getattr(args, "context_limit", 3) or 3),
                gmail_source=str(getattr(args, "gmail_source", DEFAULT_GMAIL_SOURCE) or DEFAULT_GMAIL_SOURCE),
            )
            attachment_fetcher = _default_attachment_fetcher(settings)
            for snapshot in snapshots:
                message_id = str((snapshot.get("source_message") or {}).get("message_id") or "")
                seed = _build_memory_backfill_intake_seed(snapshot)
                context_bundle = build_context_bundle(snapshot)
                case_link_result = run_case_linker(snapshot, seed, context_bundle)
                ingest_result = runtime.ingest_message(
                    snapshot=snapshot,
                    intake_result=seed,
                    case_link_result=case_link_result,
                    attachment_fetcher=attachment_fetcher,
                    attachment_max_bytes=int(getattr(settings, "attachment_extraction_max_bytes", 8_000_000) or 8_000_000),
                    refresh_document_intelligence=False,
                )
                final_result = runtime.finalize_case(
                    case_id=ingest_result.case_id,
                    message_id=message_id,
                    thread_id=str((snapshot.get("source_message") or {}).get("thread_id") or ""),
                    business_result={"recommended_next_action": "review_required", "recommended_action_reason": "cohort_proof"},
                    reply_result={"draft_enabled": False, "drafts": []},
                    action_plan_result={"primary_action": "hold", "why_this_action": "cohort_proof"},
                    case_intelligence_result={},
                )
                cid = final_result.case_id or ingest_result.case_id
                item_statuses[cid] = {"status": "ingested_for_cohort"}

        for message_id in message_ids:
            pack = runtime.get_context_pack(message_id=message_id, query_text=str(getattr(args, "query", "") or ""))
            if pack.case_id:
                context_packs.append(pack)
                item_statuses.setdefault(pack.case_id, {"status": "selected_from_gmail"})
    else:
        fetch_cases = getattr(store, "fetch_cases", None)
        rows = (
            filter_operational_feed_case_rows(list(fetch_cases(limit=int(getattr(args, "gmail_limit", 100) or 100)) or []))
            if callable(fetch_cases)
            else []
        )
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            case_id = str(row.get("case_id") or "").strip()
            if not case_id:
                continue
            pack = runtime.get_context_pack(case_id=case_id, query_text="")
            if pack.case_id:
                context_packs.append(pack)
                item_statuses.setdefault(pack.case_id, {"status": "existing_memory"})

    unique_packs: list[Any] = []
    seen_case_ids: set[str] = set()
    for pack in context_packs:
        cid = str(getattr(pack, "case_id", "") or (pack.get("case_id") if isinstance(pack, dict) else "") or "")
        if not cid or cid in seen_case_ids:
            continue
        seen_case_ids.add(cid)
        unique_packs.append(pack)

    fetch_drive_documents = getattr(store, "fetch_drive_documents", None)
    drive_items = fetch_drive_documents(limit=int(getattr(args, "drive_limit", 500) or 500)) if callable(fetch_drive_documents) else []
    record = build_cohort_run_record(
        run_id=run_id,
        gmail_items=gmail_items,
        drive_items=[item for item in drive_items if isinstance(item, dict)],
        context_packs=unique_packs,
        item_statuses=item_statuses,
    )
    record_path = write_cohort_run_record(record, root=Path(getattr(args, "output_root", RUNS_DIR / "cohort-proof")).expanduser().resolve())
    _emit_json({"ok": True, "record_path": str(record_path), "cohort_run": record})
    return 0


def run_action_proposal_list_command(args: argparse.Namespace) -> int:
    settings = load_settings(require_groq=False, require_google=False)
    runtime = _require_mailbox_memory_runtime(settings)
    runtime.bootstrap()
    rows = runtime.store.fetch_action_proposals(
        case_id=str(args.case_id or ""),
        status=str(args.status or ""),
        limit=int(args.limit or 50),
    )
    _emit_json({"ok": True, "items": rows})
    return 0


def run_action_proposal_approve_command(args: argparse.Namespace) -> int:
    from execution_runtime import approve_action_proposal

    settings = load_settings(require_groq=False, require_google=False)
    runtime = _require_mailbox_memory_runtime(settings)
    runtime.bootstrap()
    proposal = approve_action_proposal(
        runtime.store,
        str(args.proposal_id or ""),
        approved_by=str(args.approved_by or ""),
        reason=str(args.reason or ""),
    )
    _emit_json({"ok": True, "proposal": proposal.to_dict()})
    return 0


def run_action_proposal_reject_command(args: argparse.Namespace) -> int:
    from execution_runtime import reject_action_proposal

    settings = load_settings(require_groq=False, require_google=False)
    runtime = _require_mailbox_memory_runtime(settings)
    runtime.bootstrap()
    proposal = reject_action_proposal(
        runtime.store,
        str(args.proposal_id or ""),
        rejected_by=str(args.rejected_by or ""),
        reason=str(args.reason or ""),
    )
    _emit_json({"ok": True, "proposal": proposal.to_dict()})
    return 0


def run_action_proposal_execute_command(args: argparse.Namespace) -> int:
    from calendar_client import GoogleCalendarClient
    from execution_runtime import execute_action_proposal

    settings = load_settings(require_groq=False, require_google=False)
    runtime = _require_mailbox_memory_runtime(settings)
    runtime.bootstrap()
    calendar_client = GoogleCalendarClient(settings) if bool(getattr(settings, "google_calendar_enabled", False)) else None
    result = execute_action_proposal(
        runtime.store,
        str(args.proposal_id or ""),
        executed_by=str(args.executed_by or ""),
        dry_run=bool(args.dry_run),
        calendar_client=calendar_client,
    )
    _emit_json({"ok": result.execution_status in {"executed", "skipped"}, "execution_result": result.to_dict()})
    return 0 if result.execution_status in {"executed", "skipped"} else 1


def run_calendar_ingest_command(args: argparse.Namespace) -> int:
    from calendar_runtime import CalendarRuntime

    settings = load_settings(require_groq=False, require_google=False)
    if not bool(getattr(settings, "google_calendar_enabled", False)):
        _emit_json({"ok": False, "status": "disabled", "detail": "GOOGLE_CALENDAR_ENABLED is false"})
        return 1
    if bool(args.dry_run):
        from mailbox_memory_store import InMemoryMailboxMemoryStore

        store = InMemoryMailboxMemoryStore()
    else:
        try:
            runtime = _require_mailbox_memory_runtime(settings)
            runtime.bootstrap()
            store = runtime.store
        except (IntakeError, ExternalServiceError, ConnectionError, TimeoutError, OSError) as exc:
            _emit_json(
                {
                    "ok": False,
                    "status": "fail_env",
                    "error": sanitize_text(str(exc)),
                    "detail": "mailbox_memory bootstrap failed; use --dry-run for Calendar read-only proof without persistence.",
                }
            )
            return 1
    try:
        out = CalendarRuntime(settings=settings, store=store).ingest_events(
            time_min=str(args.time_min or ""),
            time_max=str(args.time_max or ""),
            limit=int(args.limit or 50),
            dry_run=bool(args.dry_run),
        )
    except (IntakeError, ExternalServiceError, ConnectionError, TimeoutError, OSError) as exc:
        _emit_json({"ok": False, "status": "fail_env", "error": sanitize_text(str(exc))})
        return 1
    if bool(args.dry_run):
        out["persistence"] = "skipped_dry_run"
        out["case_linking"] = "skipped_no_mailbox_memory_dry_run"
    _emit_json(out)
    return 0


def run_calendar_context_command(args: argparse.Namespace) -> int:
    from calendar_runtime import CalendarRuntime

    settings = load_settings(require_groq=False, require_google=False)
    runtime = _require_mailbox_memory_runtime(settings)
    runtime.bootstrap()
    if bool(getattr(settings, "google_calendar_enabled", False)):
        out = CalendarRuntime(settings=settings, store=runtime.store).context_for_case(str(args.case_id or ""))
    else:
        events = runtime.store.fetch_calendar_events_for_case(str(args.case_id or ""), limit=10)
        out = {
            "case_id": str(args.case_id or ""),
            "events": events,
            "next_event": events[0] if events else {},
            "has_calendar_event": bool(events),
            "calendar_risk": "calendar_event_exists" if events else "calendar_event_missing",
            "calendar_runtime": "disabled",
        }
    _emit_json(out)
    return 0


def run_document_intelligence_command(args: argparse.Namespace) -> int:
    from document_intelligence_runtime import build_document_intelligence_result, document_fields_to_fact_rows

    text = ""
    if getattr(args, "text_file", None):
        text = Path(args.text_file).read_text(encoding="utf-8", errors="replace")
    result = build_document_intelligence_result(
        source_type=str(args.source_type or "gmail_attachment"),
        source_id=str(args.source_id or ""),
        case_id=str(args.case_id or ""),
        filename=str(args.filename or ""),
        mime_type=str(args.mime_type or ""),
        text=text,
        parser="text_fixture" if text else "fallback",
        parser_confidence=0.7 if text else 0.2,
    )
    if bool(args.persist):
        settings = load_settings(require_groq=False, require_google=False)
        runtime = _require_mailbox_memory_runtime(settings)
        runtime.bootstrap()
        result_row = result.to_dict()
        runtime.store.upsert_document_intelligence_result(result_row)
        fact_rows = document_fields_to_fact_rows(result_row) if document_intelligence_promote_facts_enabled() else []
        if fact_rows and hasattr(runtime.store, "append_fact_rows"):
            runtime.store.append_fact_rows(fact_rows)
    _emit_json({"ok": True, "document_intelligence": result.to_dict(), "persisted": bool(args.persist)})
    return 0


def run_eval_summary_command(args: argparse.Namespace) -> int:
    from ai_quality_runtime import build_ai_quality_summary

    settings = load_settings(require_groq=False, require_google=False)
    runtime = _require_mailbox_memory_runtime(settings)
    runtime.bootstrap()
    _emit_json(build_ai_quality_summary(runtime.store, window=str(args.window or "all_time")))
    return 0


def run_drive_ingest_command(args: argparse.Namespace) -> int:
    settings = load_settings(require_groq=False, require_google=False)
    proof_dir = getattr(args, "proof_telemetry_dir", None)
    with proof_telemetry_span(
        settings,
        command_name="drive-ingest",
        case_id="",
        proof_telemetry_dir=proof_dir,
    ):
        runtime = _require_drive_runtime(settings)
        runtime.bootstrap()
        result = runtime.ingest_batch(
            limit=int(args.limit),
            root_folder_id=str(args.root_folder_id or ""),
            page_token=str(args.page_token or ""),
            run_id=str(args.run_id or ""),
            refresh_document_intelligence=bool(getattr(args, "refresh_document_intelligence", False)),
        )
        payload = result.to_dict()
        payload["refresh_document_intelligence"] = bool(getattr(args, "refresh_document_intelligence", False))
        _emit_json(payload)
    return 0


def run_drive_case_context_command(args: argparse.Namespace) -> int:
    settings = load_settings(require_groq=False, require_google=False)
    proof_dir = getattr(args, "proof_telemetry_dir", None)
    case_id = str(args.case_id or "")
    with proof_telemetry_span(
        settings,
        command_name="drive-case-context",
        case_id=case_id,
        proof_telemetry_dir=proof_dir,
    ):
        drive_runtime = _require_drive_runtime(settings)
        drive_runtime.bootstrap()
        if bool(args.refresh_projection):
            drive_runtime.refresh_case_projection(case_id)
        runtime = _require_mailbox_memory_runtime(settings)
        runtime.bootstrap()
        pack = runtime.get_context_pack(
            case_id=case_id,
            query_text=str(args.query_text or ""),
        )
        if not pack.case_id:
            raise OSError("No mailbox-memory case found for the requested case_id.")
        if bool(getattr(args, "human_summary", False)) and not bool(getattr(args, "vnext", False)):
            raise SystemExit("drive-case-context: --human-summary requires --vnext.")
        use_vnext = bool(getattr(args, "vnext", False))
        if use_vnext:
            payload = build_case_context_pack_vnext(pack, **_vnext_kwargs_from_ns(args))
        else:
            payload = pack.to_dict()
        if use_vnext and bool(getattr(args, "human_summary", False)):
            print(format_vnext_human_summary(payload), end="")
        else:
            _emit_json(payload)
    return 0


def run_drive_graph_rebuild_command(args: argparse.Namespace) -> int:
    settings = load_settings(require_groq=False, require_google=False)
    runtime = _require_drive_runtime(settings)
    runtime.bootstrap()
    result = runtime.rebuild_graph(
        limit=int(args.limit),
        case_id=str(args.case_id or ""),
    )
    _emit_json(result)
    return 0


def run_signal_run_command(args: argparse.Namespace) -> int:
    settings = load_settings(require_groq=True, require_google=False)
    from signal_worker import run_signal_loop

    pinned_message_id = str(getattr(args, "message_id", "") or "").strip()
    result = run_signal_loop(
        settings,
        loop_mode="oneshot",
        dry_run=bool(args.dry_run),
        max_iterations=1,
        verbose=bool(args.verbose),
        push_daszek=bool(args.push_daszek),
        max_messages=1 if pinned_message_id else int(args.max_messages or 0),
        timebox_seconds=int(args.timebox_seconds or 0),
        pinned_message_id=pinned_message_id,
        projection_proof=bool(getattr(args, "projection_proof", False)),
        keep_going=bool(getattr(args, "keep_going", False)),
    )
    if result.run_state is None:
        raise RuntimeError("Signal run did not produce a run_state.")
    result.run_state["manifest"]["signal_worker_result"] = sanitize_for_storage(result.to_dict())
    write_json(result.run_state["manifest_path"], result.run_state["manifest"])
    return finalize_run(result.run_state)


def run_event_spine_processor_command(args: argparse.Namespace) -> int:
    settings = load_settings(require_groq=False, require_google=False)
    if not bool(settings.event_spine_processor_enabled):
        raise ConfigError(
            "Event spine processor requires EVENT_SPINE_PROCESSOR_ENABLED=1. "
            "Use EVENT_SPINE_PROCESSOR_MODE=shadow for dry-run consumption."
        )
    if not str(settings.mailbox_memory_database_url or "").strip():
        raise ConfigError(
            "Event spine processor requires MAILBOX_MEMORY_DATABASE_URL."
        )
    from event_spine.processor import build_event_processor

    processor = build_event_processor(settings)
    poll_interval = int(settings.event_spine_processor_poll_interval_sec)

    if not bool(args.loop):
        aggregate = processor.process_once()
    else:
        max_iterations = int(args.max_iterations or 0)
        aggregate = processor.run_loop(
            max_iterations=max_iterations,
            poll_interval_sec=poll_interval,
        )
    if bool(args.verbose):
        print(
            {
                "claimed": aggregate.claimed,
                "processed": aggregate.processed,
                "failed": aggregate.failed,
                "skipped": aggregate.skipped,
                "errors": aggregate.errors[:5],
            },
            file=sys.stderr,
        )
    return 1 if aggregate.failed and not aggregate.processed else 0


def run_signal_worker_command(args: argparse.Namespace) -> int:
    settings = load_settings(require_groq=True, require_google=False)
    from signal_worker import run_signal_loop

    result = run_signal_loop(
        settings,
        loop_mode="continuous_poll",
        dry_run=bool(args.dry_run),
        max_iterations=int(args.max_iterations or 0),
        verbose=bool(args.verbose),
        push_daszek=bool(args.push_daszek),
    )
    if result.run_state is None:
        raise RuntimeError("Signal worker did not produce a run_state.")
    result.run_state["manifest"]["signal_worker_result"] = sanitize_for_storage(result.to_dict())
    write_json(result.run_state["manifest_path"], result.run_state["manifest"])
    return finalize_run(result.run_state)


def run_signal_replay_command(args: argparse.Namespace) -> int:
    settings = load_settings(require_groq=False, require_google=False)
    from signal_worker import replay_signal_from_journal

    _emit_json(replay_signal_from_journal(settings, signal_id=str(args.signal_id or "")))
    return 0


def run_signal_rebuild_case_command(args: argparse.Namespace) -> int:
    settings = load_settings(require_groq=False, require_google=False)
    from signal_worker import rebuild_case_from_signal_journal

    _emit_json(
        rebuild_case_from_signal_journal(
            settings,
            case_id=str(args.case_id or ""),
            case_key_hint=str(args.case_key_hint or ""),
        )
    )
    return 0


def run_daszek_bridge_drain_command(args: argparse.Namespace) -> int:
    """Process pending Daszek bridge_queue.jsonl rows (host-side runner)."""
    from daszek_bridge_queue_drain import run_daszek_bridge_drain

    return run_daszek_bridge_drain(args)


def run_agent_mcp_serve_command(_args: argparse.Namespace) -> int:
    """stdio MCP server for EngagementSnapshot.v2 operator tools (PR-G)."""
    import asyncio

    from agent_runtime.mcp_server import run_stdio_server

    asyncio.run(run_stdio_server())
    return 0


def run_operator_feedback_command(args: argparse.Namespace) -> int:
    """Daszek→Python bridge: persist calibration; persist adjudication + bounded reconcile."""
    settings = load_settings(require_groq=False, require_google=False)
    runtime = _require_mailbox_memory_runtime(settings)
    runtime.bootstrap()
    from adjudication_executioner import bridge_operator_feedback
    from signal_journal import SignalJournal
    from signal_reconciler import SignalRuntimeContext

    if args.json_file:
        raw_text = Path(str(args.json_file)).read_text(encoding="utf-8")
    else:
        raw_text = sys.stdin.read()
    payload = json.loads(raw_text)
    if not isinstance(payload, dict):
        raise ConfigError("operator-feedback payload must be a JSON object")

    journal = SignalJournal(
        runtime.store,
        jsonl_mirror_enabled=bool(getattr(settings, "signal_journal_jsonl_mirror_enabled", False)),
    )
    ctx = SignalRuntimeContext(
        settings=settings,
        journal=journal,
        mailbox_memory_runtime=runtime,
        graph_store=getattr(runtime, "graph_store", None),
        run_state={"run_id": str(args.run_id or "operator-feedback-cli")},
        model=getattr(settings, "groq_model", None),
        verbose=False,
        mode=str(getattr(settings, "signal_runtime_mode", "active") or "active"),
        persist_entity_links=True,
    )
    out = bridge_operator_feedback(
        store=runtime.store,
        journal=journal,
        runtime_context=ctx,
        raw_operator_payload=payload,
    )
    _emit_json(sanitize_for_storage(out))
    return 0


def run_gmail_detect_changes_command(args: argparse.Namespace) -> int:
    settings = load_settings(require_groq=False, require_google=True)
    runtime = _require_mailbox_memory_runtime(settings)
    runtime.bootstrap()
    from gmail_change_detector import poll_gmail_changes

    _emit_json(
        poll_gmail_changes(
            settings,
            store=runtime.store,
            cursor_scope=str(args.cursor_scope or "default"),
            max_results=int(args.max_results),
            verbose=bool(args.verbose),
            bootstrap_if_missing=not bool(args.no_bootstrap),
        )
    )
    return 0


def run_drive_detect_changes_command(args: argparse.Namespace) -> int:
    settings = load_settings(require_groq=False, require_google=False)
    runtime = _require_drive_runtime(settings)
    runtime.bootstrap()
    from drive_change_detector import poll_drive_changes

    _emit_json(
        poll_drive_changes(
            settings,
            store=runtime.store,
            client=runtime.client,
            cursor_scope=str(args.cursor_scope or "default"),
            max_results=int(args.max_results),
            bootstrap_if_missing=not bool(args.no_bootstrap),
        )
    )
    return 0


def _require_mailbox_memory_runtime(settings: Settings):
    runtime = build_mailbox_memory_runtime(settings)
    if runtime is None:
        raise ConfigError(
            "Mailbox memory is disabled or missing MAILBOX_MEMORY_DATABASE_URL. "
            "Set MAILBOX_MEMORY_STAGE_MODE=shadow|live and configure Postgres first. "
            "Next check: python tools/gmail_audit/gmail_intake.py doctor --skip-gmail --verbose"
        )
    return runtime


def _require_drive_runtime(settings: Settings):
    runtime = build_drive_ingest_runtime(settings)
    if runtime is None:
        raise ConfigError(
            "Drive ingest is disabled or missing shared-memory storage. "
            "Set GOOGLE_DRIVE_ENABLED=1, GOOGLE_DRIVE_INGEST_ENABLED=1, and configure MAILBOX_MEMORY_DATABASE_URL. "
            "Next check: python tools/gmail_audit/gmail_intake.py doctor --gmail-source google_api --check-drive --verbose"
        )
    return runtime


def _build_live_snapshots_for_memory_backfill(
    settings: Settings,
    *,
    message_ids: list[str],
    model: str | None,
    verbose: bool,
    context_limit: int,
    gmail_source: str,
) -> list[dict[str, Any]]:
    profile = get_profile(
        settings,
        model=model,
        verbose=verbose,
        gmail_source=gmail_source,
    )
    mailbox = infer_mailbox(profile)
    snapshots: list[dict[str, Any]] = []
    for message_id in message_ids:
        source_message = read_email(
            settings,
            message_id=message_id,
            model=model,
            verbose=verbose,
            gmail_source=gmail_source,
        )
        context_messages = fetch_context_messages(
            settings,
            source_message=source_message,
            model=model,
            verbose=verbose,
            context_limit=context_limit,
            gmail_source=gmail_source,
        )
        snapshots.append(
            build_source_snapshot(
                mailbox=mailbox,
                source_message=source_message,
                context_messages=context_messages,
            )
        )
    return snapshots


def _build_memory_backfill_intake_seed(snapshot: dict[str, Any]) -> dict[str, Any]:
    return _build_preclassified_intake_candidate(
        snapshot,
        {
            "lane": "review_direct",
            "confidence": 0.51,
        },
    )


def _config_sources_subset(settings: Settings, *keys: str) -> dict[str, str]:
    include = {"_loaded_env_file", *keys}
    return {
        key: value
        for key, value in settings.config_sources.items()
        if key in include
    }


def build_doctor_config_check(settings: Settings, *, model_override: str | None = None) -> dict[str, Any]:
    check = {
        "status": CHECK_STATUS_OK,
        "llm_backend": getattr(settings, "llm_backend", "groq"),
        "llm_primary_provider": str(getattr(settings, "llm_primary_provider", "") or ""),
        "llm_fallback_providers": list(getattr(settings, "llm_fallback_providers", ()) or ()),
        "llm_structured_provider_alternation": bool(
            getattr(settings, "llm_structured_provider_alternation", False)
        ),
        "signal_extraction_mode": str(getattr(settings, "signal_extraction_mode", "llm") or "llm"),
        "groq_model": model_override or settings.groq_model,
        "openai_compat_base_url": getattr(settings, "openai_compat_base_url", "") or None,
        "http_timeout": settings.http_timeout,
        "http_max_retries": settings.http_max_retries,
        "http_retry_base_delay": settings.http_retry_base_delay,
        "daszek_v2_push_enabled": bool(settings.daszek_v2_push_enabled),
        "daszek_operational_feed_auto_push_enabled": bool(
            getattr(settings, "daszek_operational_feed_auto_push_enabled", False)
        ),
        "daszek_operational_feed_push_min_interval_sec": int(
            getattr(settings, "daszek_operational_feed_push_min_interval_sec", 60) or 60
        ),
        "daszek_v2_readback_enabled": bool(getattr(settings, "daszek_v2_readback_enabled", False)),
        "daszek_v2_desk_relax_rejected": bool(getattr(settings, "daszek_v2_desk_relax_rejected", False)),
        "daszek_v2_desk_include_ignore": bool(getattr(settings, "daszek_v2_desk_include_ignore", False)),
        "case_guidance_enabled": bool(settings.case_guidance_enabled),
        "case_guidance_model": settings.case_guidance_model or settings.groq_model,
        "case_guidance_remote_state_enabled": bool(settings.case_guidance_remote_state_enabled),
        "attachment_extraction_enabled": bool(settings.attachment_extraction_enabled),
        "attachment_extraction_max_bytes": int(settings.attachment_extraction_max_bytes),
        "mailbox_memory_stage_mode": settings.mailbox_memory_stage_mode,
        "mailbox_memory_database_configured": bool(str(settings.mailbox_memory_database_url or "").strip()),
        "google_drive_enabled": bool(settings.google_drive_enabled),
        "google_drive_ingest_enabled": bool(settings.google_drive_ingest_enabled),
        "google_drive_graph_enabled": bool(settings.google_drive_graph_enabled),
        "google_calendar_enabled": bool(getattr(settings, "google_calendar_enabled", False)),
        "google_calendar_id": str(getattr(settings, "google_calendar_id", "") or ""),
        "neo4j_pilot_enabled": bool(getattr(settings, "neo4j_pilot_enabled", False)),
        "neo4j_uri": str(getattr(settings, "neo4j_uri", "") or "") or None,
        "neo4j_database": str(getattr(settings, "neo4j_database", "") or "neo4j"),
        "otel_enabled": bool(settings.gmail_agent_otel_enabled),
        "otel_local_mirror_enabled": bool(settings.gmail_agent_otel_local_mirror_enabled),
        "otel_service_name": settings.otel_service_name,
        "mailbox_memory_vector_enabled": bool(settings.mailbox_memory_vector_enabled),
        "embedding_openai_compat_base_url": str(getattr(settings, "openai_compat_embedding_base_url", "") or "").strip()
        or None,
        "embedding_model": settings.openai_compat_embedding_model or "",
        "embedding_dimensions": int(settings.openai_compat_embedding_dimensions or 0),
        "docling_enabled": bool(settings.docling_enabled),
        "docling_max_pages": int(settings.docling_max_pages),
        "docling_timeout_sec": int(settings.docling_timeout_sec),
        "runtime_profile": str(getattr(settings, "runtime_profile", "") or "") or "default",
        "config_sources": _config_sources_subset(
            settings,
            "LLM_BACKEND",
            "LLM_PRIMARY_PROVIDER",
            "LLM_FALLBACK_PROVIDERS",
            "LLM_STRUCTURED_PROVIDER_ALTERNATION",
            "OPENAI_COMPAT_BASE_URL",
            "OPENAI_COMPAT_MODEL",
            "OPENAI_COMPAT_API_KEY",
            "GROQ_MODEL",
            "GROQ_BASE_URL",
            "HTTP_TIMEOUT",
            "HTTP_MAX_RETRIES",
            "HTTP_RETRY_BASE_DELAY",
            "DASZEK_BASE_URL",
            "DASZEK_LOGIN",
            "DASZEK_PASSWORD",
            "DASZEK_V2_PUSH",
            "DASZEK_OPERATIONAL_FEED_AUTO_PUSH",
            "DASZEK_OPERATIONAL_FEED_PUSH_MIN_INTERVAL_SEC",
            "DASZEK_V2_READBACK_ENABLED",
            "DASZEK_V2_DESK_RELAX_REJECTED",
            "DASZEK_V2_DESK_INCLUDE_IGNORE",
            "CASE_GUIDANCE_ENABLED",
            "CASE_GUIDANCE_MODEL",
            "CASE_GUIDANCE_REMOTE_STATE",
            "ATTACHMENT_EXTRACTION_ENABLED",
            "ATTACHMENT_EXTRACTION_MAX_BYTES",
            "MAILBOX_MEMORY_DATABASE_URL",
            "DATABASE_URL",
            "MAILBOX_MEMORY_STAGE_MODE",
            "MAILBOX_MEMORY_STAGE_ALLOWLIST",
            "MAILBOX_MEMORY_BLOB_ROOT",
            "GOOGLE_DRIVE_ENABLED",
            "GOOGLE_DRIVE_CREDENTIALS_PATH",
            "GOOGLE_DRIVE_SHARED_DRIVE_ID",
            "GOOGLE_DRIVE_ROOT_FOLDER_ID",
            "GOOGLE_DRIVE_BATCH_PAGE_SIZE",
            "GOOGLE_DRIVE_MAX_DOWNLOAD_BYTES",
            "GOOGLE_DRIVE_INGEST_ENABLED",
            "GOOGLE_DRIVE_GRAPH_ENABLED",
            "GOOGLE_CALENDAR_ENABLED",
            "GOOGLE_CALENDAR_ID",
            "NEO4J_PILOT_ENABLED",
            "NEO4J_URI",
            "NEO4J_USERNAME",
            "NEO4J_PASSWORD",
            "NEO4J_DATABASE",
            "GMAIL_AGENT_OTEL_ENABLED",
            "GMAIL_AGENT_OTEL_LOCAL_MIRROR_ENABLED",
            "OTEL_SERVICE_NAME",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_HEADERS",
            "MAILBOX_MEMORY_VECTOR_ENABLED",
            "OPENAI_COMPAT_EMBEDDING_BASE_URL",
            "OPENAI_COMPAT_EMBEDDING_API_KEY",
            "OPENAI_COMPAT_EMBEDDING_MODEL",
            "OPENAI_COMPAT_EMBEDDING_DIMENSIONS",
            "DOCLING_ENABLED",
            "DOCLING_MAX_PAGES",
            "DOCLING_TIMEOUT_SEC",
            "GMAIL_AGENT_RUNTIME_PROFILE",
            "SIGNAL_RUNTIME_MODE",
            "SIGNAL_WORKER_ENABLED",
            "GMAIL_INGRESS_OWNER",
            "GMAIL_CHANGE_DETECTION_ENABLED",
            "EVENT_SPINE_PROCESSOR_ENABLED",
            "SIGNAL_RUNTIME_COMPAT",
            "INTAKE_LLM_BEFORE_SIGNAL",
        ),
        "signal_runtime_mode": str(getattr(settings, "signal_runtime_mode", "") or ""),
        "intake_llm_before_signal": bool(getattr(settings, "intake_llm_before_signal", False)),
        "signal_worker_enabled": bool(getattr(settings, "signal_worker_enabled", False)),
        "gmail_ingress_owner": str(getattr(settings, "gmail_ingress_owner", "") or ""),
        "gmail_change_detection_enabled": bool(getattr(settings, "gmail_change_detection_enabled", False)),
        "event_spine_processor_enabled": bool(getattr(settings, "event_spine_processor_enabled", False)),
        "signal_runtime_compat": bool(getattr(settings, "signal_runtime_compat", False)),
    }
    if settings.signal_runtime_mode != "active":
        check["status"] = "error"
        check.setdefault("errors", []).append("SIGNAL_RUNTIME_MODE must be active (signal-active only).")
    if not settings.signal_worker_enabled:
        check.setdefault("warnings", []).append(
            "SIGNAL_WORKER_ENABLED=0 — signal-run / signal-worker will raise until set to 1."
        )
    if settings.event_spine_processor_enabled and settings.gmail_ingress_owner == "signal_worker":
        check.setdefault("warnings", []).append(
            "EVENT_SPINE_PROCESSOR_ENABLED=1 with GMAIL_INGRESS_OWNER=signal_worker may duplicate Gmail work."
        )
    if settings.config_warnings:
        check["warnings"] = list(dict.fromkeys([*(check.get("warnings") or []), *settings.config_warnings]))
    return check


def build_ocr_check(settings: Settings) -> dict[str, Any]:
    check = {
        "enabled": bool(settings.attachment_extraction_enabled),
        "max_bytes": int(settings.attachment_extraction_max_bytes),
        "config_sources": _config_sources_subset(
            settings,
            "ATTACHMENT_EXTRACTION_ENABLED",
            "ATTACHMENT_EXTRACTION_MAX_BYTES",
        ),
    }
    if not settings.attachment_extraction_enabled:
        check["status"] = "disabled"
        check["reason"] = "Attachment extraction is disabled by ATTACHMENT_EXTRACTION_ENABLED=0."
        return check
    runtime = inspect_ocr_runtime()
    status = str(runtime.get("status") or "")
    if status == "ok":
        check["status"] = CHECK_STATUS_OK
    elif status == "deps_missing":
        check["status"] = "deps_missing"
    elif status == "binary_missing":
        check["status"] = "binary_missing"
    else:
        check["status"] = CHECK_STATUS_FAILED
    check.update(runtime)
    return check


def build_pgvector_check(settings: Settings) -> dict[str, Any]:
    return check_pgvector_extension(
        str(getattr(settings, "mailbox_memory_database_url", "") or ""),
        vector_enabled=bool(getattr(settings, "mailbox_memory_vector_enabled", False)),
    )


def build_unstructured_check(settings: Settings) -> dict[str, Any]:
    check = {
        "enabled": bool(getattr(settings, "unstructured_enabled", False)),
        "parser_chain": list(getattr(settings, "attachment_parser_chain", ()) or ()),
        "config_sources": _config_sources_subset(
            settings,
            "UNSTRUCTURED_ENABLED",
            "ATTACHMENT_PARSER_CHAIN",
            "DOCUMENT_STRUCTURED_FACTS",
        ),
    }
    if not check["enabled"]:
        check["status"] = CHECK_STATUS_SKIPPED
        check["reason"] = "Unstructured is disabled by UNSTRUCTURED_ENABLED=0."
        return check
    runtime = inspect_unstructured_runtime()
    status = str(runtime.get("status") or "")
    check["status"] = CHECK_STATUS_OK if status == "ok" else CHECK_STATUS_FAILED
    check.update(runtime)
    return check


def build_docling_check(settings: Settings) -> dict[str, Any]:
    check = {
        "enabled": bool(settings.docling_enabled),
        "max_pages": int(settings.docling_max_pages),
        "timeout_sec": int(settings.docling_timeout_sec),
        "config_sources": _config_sources_subset(
            settings,
            "DOCLING_ENABLED",
            "DOCLING_MAX_PAGES",
            "DOCLING_TIMEOUT_SEC",
        ),
    }
    if not settings.docling_enabled:
        check["status"] = CHECK_STATUS_SKIPPED
        check["reason"] = "Docling is disabled by DOCLING_ENABLED=0."
        return check
    runtime = inspect_docling_runtime()
    status = str(runtime.get("status") or "")
    check["status"] = CHECK_STATUS_OK if status == "ok" else CHECK_STATUS_FAILED
    check.update(runtime)
    return check


def process_batch_items(
    *,
    settings: Settings,
    schema: dict[str, Any],
    instructions: str,
    run_state: dict[str, Any],
    batch_items: list[Any],
    model: str | None,
    verbose: bool,
    context_limit: int,
    keep_going: bool,
    gmail_source: str,
) -> None:
    run_state["summary"]["items_selected"] = len(batch_items)
    update_checkpoint(run_state)

    live_items = [item for item in batch_items if not is_frozen_snapshot_item(item)]
    frozen_items = [item for item in batch_items if is_frozen_snapshot_item(item)]

    if live_items:
        process_live_selection(
            settings=settings,
            schema=schema,
            instructions=instructions,
            run_state=run_state,
            selected_items=live_items,
            model=model,
            verbose=verbose,
            context_limit=context_limit,
            keep_going=keep_going,
            gmail_source=gmail_source,
        )
    if frozen_items and not run_state["summary"]["aborted"]:
        process_frozen_snapshots(
            settings=settings,
            schema=schema,
            instructions=instructions,
            run_state=run_state,
            snapshots=frozen_items,
            model=model,
            verbose=verbose,
            keep_going=keep_going,
        )


def process_live_selection(
    *,
    settings: Settings,
    schema: dict[str, Any],
    instructions: str,
    run_state: dict[str, Any],
    selected_items: list[Any],
    model: str | None,
    verbose: bool,
    context_limit: int,
    keep_going: bool,
    gmail_source: str,
) -> None:
    delay_s = float((run_state.get("runtime_controls") or {}).get("llm_inter_item_delay_seconds") or 0.0)
    for idx, selected in enumerate(selected_items):
        if idx > 0 and delay_s > 0:
            time.sleep(delay_s)
        if check_runtime_stop_conditions(run_state, stage="selection"):
            return
        run_state["summary"]["items_seen"] += 1
        message_id = extract_message_id(selected)
        if not message_id:
            run_state["summary"]["items_failed"] += 1
            record_error(run_state, stage="selection", message_id="", error="Missing message id.", details={"item": selected})
            update_checkpoint(run_state)
            if not keep_going:
                mark_stop_reason(
                    run_state,
                    reason="stopped_after_failure",
                    details=latest_failure_details(run_state, fallback_stage="selection"),
                )
                return
            if check_runtime_stop_conditions(run_state, stage="selection"):
                return
            continue

        try:
            source_message = read_email(
                settings,
                message_id=message_id,
                model=model,
                verbose=verbose,
                gmail_source=gmail_source,
            )
        except GroqClientError as exc:
            run_state["summary"]["items_failed"] += 1
            record_error(run_state, stage="fetch", message_id=message_id, error=str(exc))
            update_checkpoint(run_state, last_message_id=message_id)
            if not keep_going:
                mark_stop_reason(
                    run_state,
                    reason="stopped_after_failure",
                    details=latest_failure_details(run_state, fallback_stage="fetch", fallback_message_id=message_id),
                )
                return
            if check_runtime_stop_conditions(run_state, stage="fetch", message_id=message_id):
                return
            continue

        context_messages = fetch_context_messages(
            settings,
            source_message=source_message,
            model=model,
            verbose=verbose,
            context_limit=context_limit,
            gmail_source=gmail_source,
        )
        snapshot = build_source_snapshot(
            mailbox=run_state["manifest"]["mailbox"],
            source_message=source_message,
            context_messages=context_messages,
        )
        run_state["summary"]["items_fetched"] += 1

        should_continue = process_snapshot(
            settings=settings,
            schema=schema,
            instructions=instructions,
            run_state=run_state,
            snapshot=snapshot,
            model=model,
            verbose=verbose,
            keep_going=keep_going,
        )
        update_checkpoint(run_state, last_message_id=message_id)
        if not should_continue:
            if not run_state["summary"]["stop_reason"]:
                mark_stop_reason(
                    run_state,
                    reason="stopped_after_failure",
                    details=latest_failure_details(run_state, fallback_stage="model", fallback_message_id=message_id),
                )
            return


def process_frozen_snapshots(
    *,
    settings: Settings,
    schema: dict[str, Any],
    instructions: str,
    run_state: dict[str, Any],
    snapshots: list[Any],
    model: str | None,
    verbose: bool,
    keep_going: bool,
) -> None:
    delay_s = float((run_state.get("runtime_controls") or {}).get("llm_inter_item_delay_seconds") or 0.0)
    for idx, item in enumerate(snapshots):
        if idx > 0 and delay_s > 0:
            time.sleep(delay_s)
        if check_runtime_stop_conditions(run_state, stage="snapshot"):
            return
        run_state["summary"]["items_seen"] += 1
        try:
            snapshot = coerce_source_snapshot(item, mailbox_fallback=run_state["manifest"]["mailbox"])
        except (IntakeError, ExternalServiceError, ValueError, TypeError, KeyError) as exc:
            run_state["summary"]["items_failed"] += 1
            record_error(run_state, stage="snapshot", message_id=extract_message_id(item), error=str(exc))
            update_checkpoint(run_state, last_message_id=extract_message_id(item))
            logger.warning("Snapshot resilient error", extra={"x": {
                "error": str(exc)[:200],
                "message_id": extract_message_id(item),
            }})
            if not keep_going:
                mark_stop_reason(
                    run_state,
                    reason="stopped_after_failure",
                    details=latest_failure_details(
                        run_state,
                        fallback_stage="snapshot",
                        fallback_message_id=extract_message_id(item),
                    ),
                )
                return
            if check_runtime_stop_conditions(run_state, stage="snapshot", message_id=extract_message_id(item)):
                return
            continue

        run_state["summary"]["items_fetched"] += 1
        message_id = str(snapshot.get("source_message", {}).get("message_id") or "")
        should_continue = process_snapshot(
            settings=settings,
            schema=schema,
            instructions=instructions,
            run_state=run_state,
            snapshot=snapshot,
            model=model,
            verbose=verbose,
            keep_going=keep_going,
        )
        update_checkpoint(run_state, last_message_id=message_id)
        if not should_continue:
            if not run_state["summary"]["stop_reason"]:
                mark_stop_reason(
                    run_state,
                    reason="stopped_after_failure",
                    details=latest_failure_details(run_state, fallback_stage="snapshot", fallback_message_id=message_id),
                )
            return


def build_context_bundle(snapshot: dict[str, Any], *, case_context_pack: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a compact context bundle reused across v2 stages."""
    context_messages = snapshot.get("context_messages") or []
    bundle = {
        "thread_context": sanitize_for_storage(snapshot.get("thread_context") or {}),
        "routing_hints": sanitize_for_storage(snapshot.get("routing_hints") or {}),
        "case_link_candidates": sanitize_for_storage(snapshot.get("case_link_candidates") or []),
        "context_messages": [
            {
                "message_id": str(item.get("message_id") or ""),
                "sender": str(item.get("sender") or ""),
                "subject": str(item.get("subject") or ""),
                "thread_id": str(item.get("thread_id") or ""),
            }
            for item in context_messages[:5]
            if isinstance(item, dict)
        ],
    }
    if isinstance(case_context_pack, dict) and case_context_pack:
        bundle["case_context_pack"] = sanitize_for_storage(case_context_pack)
    return bundle


def _resolve_effective_mailbox_context_pack(config: dict[str, Any]) -> dict[str, Any] | None:
    preflight = config.get("mailbox_memory_context_pack_preflight")
    if isinstance(preflight, dict) and preflight:
        return preflight
    current = config.get("mailbox_memory_context_pack")
    if isinstance(current, dict) and current:
        return current
    return None


def _resolve_effective_context_bundle(
    snapshot: dict[str, Any],
    context_bundle: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    effective_pack = _resolve_effective_mailbox_context_pack(config)
    if isinstance(effective_pack, dict) and effective_pack:
        return build_context_bundle(snapshot, case_context_pack=effective_pack)
    return context_bundle if isinstance(context_bundle, dict) else build_context_bundle(snapshot)


def _resolve_preflight_hot_state(config: dict[str, Any]) -> dict[str, Any] | None:
    hot = config.get("case_snapshot_hot_state_preflight")
    if isinstance(hot, dict) and hot:
        return hot
    return None


def _build_lane_stage_plan(preclassification_result: dict[str, Any]) -> dict[str, Any]:
    lane = str(preclassification_result.get("lane") or "intake_llm")
    plans = {
        "skip": {
            "lane": "skip",
            "intake_reasoning_mode": "deterministic_preclassification",
            "run_case_linking": False,
            "run_business_reasoning": False,
            "run_reply_drafter": False,
            "run_action_planner": True,
            "expected_projection_mode": "ignore",
            "notes": ["obvious noise stays on the deterministic control-plane path"],
        },
        "reference_only": {
            "lane": "reference_only",
            "intake_reasoning_mode": "deterministic_preclassification",
            "run_case_linking": True,
            "run_business_reasoning": False,
            "run_reply_drafter": False,
            "run_action_planner": True,
            "expected_projection_mode": "reference",
            "notes": ["reference-only lane skips deep business reasoning and reply drafting"],
        },
        "review_direct": {
            "lane": "review_direct",
            "intake_reasoning_mode": "deterministic_preclassification",
            "run_case_linking": True,
            "run_business_reasoning": False,
            "run_reply_drafter": False,
            "run_action_planner": True,
            "expected_projection_mode": "review",
            "notes": ["review-direct lane preserves explicit review semantics without extra LLM stages"],
        },
        "intake_llm": {
            "lane": "intake_llm",
            "intake_reasoning_mode": "llm",
            "run_case_linking": True,
            "run_business_reasoning": True,
            "run_reply_drafter": True,
            "run_action_planner": True,
            "expected_projection_mode": "",
            "notes": ["full v2 shadow path enabled"],
        },
    }
    return sanitize_for_storage(plans.get(lane, plans["intake_llm"]))


def preclassify_snapshot(snapshot: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Run the deterministic preclassifier stage."""
    _ = config
    return sanitize_for_storage(run_preclassifier(snapshot))


def _run_llm_with_timeout(
    config: dict[str, Any],
    stage_payload: dict[str, Any],
    request_variants: list[dict[str, Any]],
    intake_variants: list[dict[str, Any]],
    snapshot: dict[str, Any] | None = None,
    case_id: str | None = None,
    engagement_id: str | None = None,
) -> dict[str, Any] | None:
    """Run the intake-reasoning stage under the stage's own bounded deadline.

    This used to wrap the call in ``ThreadPoolExecutor(...).result(timeout=60)``. That outer
    envelope was numerically identical to a single inner HTTP attempt (``http_timeout=60``)
    and its clock started first, so the router's 4-attempt retry and its ``groq`` fallback
    could never run -- Fresh38 measured 6/38 first attempts dying that way. ``future.cancel()``
    also could not stop the in-flight request, leaving an orphan thread whose outcome nobody
    observed. The bound now lives in one place, ``run_central_structured_stage``, which shares
    it with retry and fallback instead of racing them (see ``llm_deadline``).

    Returns ``None`` only when the stage budget is genuinely exhausted -- the same terminal
    contract the old timeout path had, so downstream parity handling is unchanged. Every other
    provider error still propagates.
    """
    _snapshot = snapshot or config.get("snapshot", {})

    def _call():
        return run_central_structured_stage(
            config["settings"],
            stage_name="intake_reasoning",
            task_instructions=config["instructions"],
            prompt_input=stage_payload,
            query_text=build_signal_extraction_query(_snapshot),
            json_schema=config["schema"],
            schema_name="intake_output_v1",
            case_id=case_id or None,
            engagement_id=engagement_id or None,
            model=config["model"],
            verbose=config["verbose"],
            input_variants=request_variants,
            output_model=IntakeReasoningResult,
            context_bundle=config.get("context_bundle", {}),
            correlation_id=str((_snapshot.get("source_message") or {}).get("message_id") or "").strip() or None,
        )

    try:
        return _call()
    except GroqClientError as exc:
        details = dict(getattr(exc, "details", {}) or {})
        if str(details.get("terminal_failure_reason") or "") != "stage_deadline_exhausted":
            raise
        from log_config import get_logger

        stage_telemetry = details.get("llm_stage_deadline") or {}
        get_logger("gmail_intake").error("INTAKE_LLM_TIMEOUT", extra={"x": {
            "terminal_failure_reason": "stage_deadline_exhausted",
            "configured_stage_budget_ms": stage_telemetry.get("configured_stage_budget_ms"),
            "elapsed_ms": stage_telemetry.get("elapsed_ms"),
            "remaining_budget_ms": stage_telemetry.get("remaining_budget_ms"),
            "provider_attempts": details.get("llm_provider_attempts") or [],
            "fallback_used": bool(details.get("llm_fallback_used")),
            "fallback_reason": details.get("llm_fallback_reason"),
        }})
        return None


def run_intake_reasoning(
    snapshot: dict[str, Any],
    context_bundle: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Run intake reasoning or a deterministic shortcut, depending on the preclassification lane."""
    preclassification = config["preclassification_result"]
    stage_payload = build_intake_reasoning_payload(snapshot, context_bundle)
    lane = str(preclassification.get("lane") or "intake_llm")

    if lane != "intake_llm":
        candidate = _build_preclassified_intake_candidate(snapshot, preclassification)
        return {
            "raw_output_text": json.dumps(candidate, ensure_ascii=False),
            "response_json": sanitize_for_storage(candidate),
            "second_pass_applied": False,
            "request_meta": {
                "final_inference_mode": "deterministic_preclassification",
                "attempts_made": 1,
            },
            "input_variants": [
                {
                    "mode": "deterministic_preclassification",
                    "payload": sanitize_for_storage(stage_payload),
                    "metrics": {"lane": lane},
                    "task_prompt": "",
                }
            ],
            "execution_metadata": {
                "stage_name": "intake_reasoning",
                "model_name": "deterministic_preclassification",
                "attempt_count": 1,
                "latency_ms": 0,
                "fallback_used": False,
                "response_text": json.dumps(candidate, ensure_ascii=False),
                "parse_status": "deterministic_shortcut",
                "request_meta": {"final_inference_mode": "deterministic_preclassification"},
                "prompt_input": sanitize_for_storage(stage_payload),
            },
        }

    inference_packages = build_inference_payload_variants(snapshot)
    request_variants: list[dict[str, Any]] = []
    intake_variants: list[dict[str, Any]] = []
    for package in inference_packages:
        prompt = render_task_prompt(snapshot, inference_payload=package)
        request_variants.append(
            {
                "mode": package["mode"],
                "input": prompt,
                "metadata": package.get("metrics") or {},
            }
        )
        intake_variants.append(
            {
                "mode": package["mode"],
                "payload": package["payload"],
                "metrics": package.get("metrics") or {},
                "task_prompt": prompt,
            }
        )

    settings = config["settings"]
    hvac_signals: dict[str, Any] = {}
    if str(getattr(settings, "signal_extraction_mode", "llm") or "llm").strip().lower() == "llm":
        hvac_signals = run_signal_extraction(
            settings=settings,
            snapshot=snapshot,
            context_bundle=context_bundle,
        )
        # SLICE-1: run_signal_extraction returns a TRUTHY marker dict on failure
        # ({"parse_status": "extraction_failed"|"empty_result", "error_reason": ...}), so a bare
        # `if hvac_signals` previously injected an internal error string into the Intake prompt as
        # if it were extracted evidence — absence and failure were indistinguishable to the model.
        # A failed extraction is a blocking gap, never evidence (operator decision 2026-06-09:
        # provider-chain exhaustion must surface as an error, never be rescued by a heuristic).
        # The reason is preserved for telemetry; it just never enters the prompt.
        if signal_extraction_failed(hvac_signals):
            stage_payload["hvac_signals_error"] = {
                "parse_status": str(hvac_signals.get("parse_status") or ""),
                "error_reason": str(hvac_signals.get("error_reason") or "")[:300],
            }
        elif hvac_signals:
            stage_payload["hvac_signals"] = hvac_signals

    case_link_result = config.get("case_link_result") if isinstance(config.get("case_link_result"), dict) else {}
    case_id = resolve_case_id(
        context_bundle=context_bundle,
        case_link_result=case_link_result,
    )
    engagement_id = resolve_engagement_id(
        context_bundle=context_bundle,
        case_link_result=case_link_result,
    )
    stage_call = _run_llm_with_timeout(
        config, stage_payload, request_variants, intake_variants,
        snapshot=snapshot, case_id=case_id, engagement_id=engagement_id,
    )
    if stage_call is None:
        return {
            "raw_output_text": "",
            "response_json": {},
            "request_meta": {"central_llm_failed": True},
            "input_variants": intake_variants,
            "execution_metadata": {"parse_status": "central_llm_failed"},
            "second_pass_applied": False,
        }
    if str(stage_call.get("parse_status") or "") == "pydantic_failed":
        errors = (stage_call.get("request_meta") or {}).get("pydantic_errors")
        logger.warning("[intake_reasoning] Pydantic ValidationError: %s", errors)
    second_pass_applied = False

    def _parse_json_dict(raw: str) -> dict[str, Any]:
        try:
            obj = json.loads(raw or "")
        except json.JSONDecodeError:
            return {}
        return obj if isinstance(obj, dict) else {}

    first_json = _parse_json_dict(str(stage_call.get("response_text") or ""))
    if first_json.get("schema_version") == "1.0":
        stage_call["response_json"] = first_json
    if (
        should_run_intake_second_pass(
            preclassification=config.get("preclassification_result") or {},
            first_response_json=first_json,
            cached_attachment_intelligence=config.get("cached_attachment_intelligence"),
        )
        and str(first_json.get("schema_version") or "") == "1.0"
    ):
        try:
            sp_call = run_intake_second_pass_supplement(
                settings=config["settings"],
                model=config["model"],
                verbose=config["verbose"],
                first_response_json=first_json,
                cached_attachment_intelligence=config.get("cached_attachment_intelligence"),
                context_bundle=context_bundle,
                case_link_result=case_link_result,
            )
            sup = _parse_json_dict(str(sp_call.get("response_text") or ""))
            if sup.get("schema_version") == "1.0" and first_json:
                merged = merge_intake_second_pass_supplement(first_json, sup)
                stage_call["response_json"] = merged
                stage_call["response_text"] = json.dumps(merged, ensure_ascii=False)
                second_pass_applied = True
                stage_call["second_pass_execution"] = {
                    "applied": True,
                    "latency_ms": sp_call.get("latency_ms"),
                    "attempt_count": sp_call.get("attempt_count"),
                }
            else:
                stage_call["second_pass_execution"] = {"applied": False, "reason": "empty_or_invalid_supplement"}
        except GroqClientError as exc:
            stage_call["second_pass_execution"] = {"applied": False, "error": sanitize_text(str(exc))}
    request_meta = stage_call.get("request_meta") or {}
    request_meta["second_pass_applied"] = second_pass_applied
    result = {
        "raw_output_text": stage_call["response_text"],
        "response_json": stage_call.get("response_json") or {},
        "request_meta": request_meta,
        "input_variants": intake_variants,
        "execution_metadata": stage_call,
        "second_pass_applied": second_pass_applied,
    }
    if hvac_signals:
        result["hvac_signals"] = hvac_signals
    return result


def validate_intake_output(raw_output: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Validate intake output, apply normalization/repair, and enforce contextual guards."""
    raw_text = str(raw_output.get("raw_output_text") or "")
    validation_trace = validate_output_with_repair(raw_text, schema=config["schema"], snapshot=config["snapshot"])
    validation = validation_trace.result
    raw_valid = bool(validation_trace.raw_result.is_valid)
    normalized_valid = bool(validation_trace.normalized_result and validation_trace.normalized_result.is_valid)
    repaired_valid = bool(validation_trace.repaired_result and validation_trace.repaired_result.is_valid)

    if not validation.is_valid or validation.data is None:
        return {
            "is_valid": False,
            "validation_trace": validation_trace,
            "validation": validation,
            "raw_valid": raw_valid,
            "normalized_valid": normalized_valid,
            "repaired_valid": repaired_valid,
            "intake_output": None,
            "intake_result_final": None,
            "guardrail_flags": [],
            "final_output_origin": validation_trace.final_output_origin,
            "original_action": "",
        }

    intake_output = validation.data
    original_action = intake_output["decision"]["action"]
    guardrail_flags: list[str] = []
    final_output_origin = validation_trace.final_output_origin
    intake_result_final = validate_intake_result(
        intake_output,
        final_output_origin=validation_trace.final_output_origin,
        normalization_notes=validation_trace.normalization_notes,
        repair_notes=validation_trace.repair_notes,
    )

    try:
        guarded_output, guardrail_flags = apply_contextual_guards(
            intake_output,
            snapshot=config["snapshot"],
            schema=config["schema"],
        )
    except GroqClientError as exc:
        return {
            "is_valid": False,
            "validation_trace": validation_trace,
            "validation": validation,
            "raw_valid": raw_valid,
            "normalized_valid": normalized_valid,
            "repaired_valid": repaired_valid,
            "intake_output": None,
            "intake_result_final": None,
            "guardrail_flags": [],
            "final_output_origin": OUTPUT_ORIGIN_INVALID,
            "original_action": original_action,
            "guardrail_error": sanitize_text(str(exc)),
        }

    if guardrail_flags:
        final_output_origin = OUTPUT_ORIGIN_GUARDRAILED_REVIEW
    intake_result_final = validate_intake_result(
        guarded_output,
        final_output_origin=final_output_origin,
        normalization_notes=validation_trace.normalization_notes,
        repair_notes=validation_trace.repair_notes,
        guardrail_flags=guardrail_flags,
    )
    return {
        "is_valid": True,
        "validation_trace": validation_trace,
        "validation": validation,
        "raw_valid": raw_valid,
        "normalized_valid": normalized_valid,
        "repaired_valid": repaired_valid,
        "intake_output": guarded_output,
        "intake_result_final": intake_result_final,
        "guardrail_flags": guardrail_flags,
        "final_output_origin": final_output_origin,
        "original_action": original_action,
    }


def link_case_context(
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    context_bundle: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Run deterministic case linking after intake validation."""
    lane_plan = config.get("lane_stage_plan") or {}
    lane = str((config.get("preclassification_result") or {}).get("lane") or "intake_llm")
    entity_link_result = config.get("entity_link_result") or {}
    if (
        isinstance(entity_link_result, dict)
        and str(entity_link_result.get("link_status") or "") == "VERIFIED"
        and str(entity_link_result.get("case_key") or "").strip()
    ):
        selected_key = str(entity_link_result.get("case_key") or "").strip()
        cands = entity_link_result.get("candidates") or []
        result = validate_case_link_result(
            {
                "selected_case_key": selected_key,
                "decision": "linked",
                "confidence": float(entity_link_result.get("confidence") or 1.0),
                "source": "entity_match",
                "reasons": [
                    "entity_linker_identity_first",
                    *[str(x) for x in (entity_link_result.get("reasons") or ())],
                ],
                "candidates": cands if isinstance(cands, list) else [],
            }
        )
        result["execution_metadata"] = {
            "stage_name": "case_linking",
            "deterministic": True,
            "lane": lane,
            "parse_status": "entity_link_override",
        }
        return result
    if not bool(lane_plan.get("run_case_linking", True)):
        result = build_no_link_case_result(reason="case_linking_skipped_for_lane")
    else:
        result = sanitize_for_storage(run_case_linker(snapshot, intake_result, context_bundle))
    result["execution_metadata"] = {
        "stage_name": "case_linking",
        "deterministic": True,
        "lane": lane,
        "parse_status": "skipped_for_lane" if not bool(lane_plan.get("run_case_linking", True)) else "deterministic",
    }
    return result


def run_business_reasoning(
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
    context_bundle: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Run shadow-only business reasoning unless the lane is intentionally skipped."""
    lane = str((config.get("preclassification_result") or {}).get("lane") or "intake_llm")
    lane_plan = config.get("lane_stage_plan") or {}
    if not bool(lane_plan.get("run_business_reasoning", True)):
        return build_skipped_business_reasoning(
            lane=lane,
            intake_result=intake_result,
            reason=f"business_reasoning_skipped_for_{lane}_lane",
        )

    effective_context_bundle = _resolve_effective_context_bundle(snapshot, context_bundle, config)
    business_context_bundle = build_business_context_bundle(snapshot, intake_result, case_link_result)
    return run_shadow_business_reasoning(
        settings=config["settings"],
        snapshot=snapshot,
        intake_result=intake_result,
        case_link_result=case_link_result,
        context_bundle=effective_context_bundle,
        business_context_bundle=business_context_bundle,
        model=config["model"],
        verbose=config["verbose"],
    )


def draft_reply(
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    business_result: dict[str, Any] | None,
    context_bundle: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Run reply drafting in shadow mode with safe no-draft fallbacks."""
    lane = str((config.get("preclassification_result") or {}).get("lane") or "intake_llm")
    lane_plan = config.get("lane_stage_plan") or {}
    run_state = config.get("run_state") if isinstance(config.get("run_state"), dict) else {}
    run_id = str(config.get("run_id") or run_state.get("run_id") or "")
    if not bool(lane_plan.get("run_reply_drafter", True)):
        return annotate_reply_causal_observability(
            build_skipped_reply_draft(
                lane=lane,
                reason=f"reply_drafter_skipped_for_{lane}_lane",
            ),
            snapshot=snapshot,
            intake_result=intake_result,
            business_result=business_result,
            context_bundle=context_bundle,
            lane_plan=lane_plan,
            run_id=run_id,
        )
    if business_result is None:
        return annotate_reply_causal_observability(
            fallback_reply_drafter(reason="business_reasoning_missing"),
            snapshot=snapshot,
            intake_result=intake_result,
            business_result=business_result,
            context_bundle=context_bundle,
            lane_plan=lane_plan,
            run_id=run_id,
        )

    business_context_bundle = build_business_context_bundle(
        snapshot,
        intake_result,
        config.get("case_link_result") or {},
    )
    effective_context_bundle = _resolve_effective_context_bundle(snapshot, context_bundle, config)
    return run_shadow_reply_drafter(
        settings=config["settings"],
        snapshot=snapshot,
        intake_result=intake_result,
        business_result=business_result,
        business_context_bundle=business_context_bundle,
        context_bundle=effective_context_bundle,
        model=config["model"],
        verbose=config["verbose"],
        run_id=run_id,
    )


def plan_actions(
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
    business_result: dict[str, Any] | None,
    reply_result: dict[str, Any] | None,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the conservative operator-facing action plan."""
    canonical_decision = config.get("canonical_decision")
    if not isinstance(canonical_decision, dict):
        canonical_decision = None
    result = build_action_plan_result(
        intake_result,
        case_link_result,
        business_result,
        reply_result,
        _resolve_effective_mailbox_context_pack(config),
        canonical_decision=canonical_decision,
    )
    canonicalization_failure = config.get("canonicalization_failure")
    if isinstance(canonicalization_failure, dict):
        from canonical_action_decision import canonicalization_failure_review_state

        result["canonicalization_failure"] = canonicalization_failure
        result["workflow_state"] = canonicalization_failure_review_state(canonicalization_failure)
    if isinstance(canonical_decision, dict):
        result["canonical_decision"] = canonical_decision
    return result


def build_case_intelligence_layer(
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
    business_result: dict[str, Any] | None,
    reply_result: dict[str, Any] | None,
    action_plan_result: dict[str, Any] | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Build the case-first AI intelligence layer over the existing foundation stages, enriched with substrate layers."""
    cfg = config if isinstance(config, dict) else {}
    canonical_decision = cfg.get("canonical_decision")
    if not isinstance(canonical_decision, dict):
        canonical_decision = None
    effective_context_pack = _resolve_effective_mailbox_context_pack(cfg)
    settings_obj = cfg.get("settings")
    att_fetcher = cfg.get("attachment_fetcher")
    att_max = int(cfg.get("attachment_max_bytes") or 8_000_000)
    if not callable(att_fetcher) and settings_obj is not None:
        att_fetcher = _default_attachment_fetcher(settings_obj)
    cached = cfg.get("cached_attachment_intelligence")
    if isinstance(cached, dict) and cached.get("attachments") is not None:
        att_intel = refresh_attachment_intelligence_with_intake_context(
            cached,
            intake_result=intake_result,
            case_link_result=case_link_result,
        )
    else:
        att_intel = build_attachment_intelligence(
            snapshot,
            intake_result=intake_result,
            case_link_result=case_link_result,
            attachment_fetcher=att_fetcher if callable(att_fetcher) else None,
            attachment_max_bytes=att_max,
        )
    thread_mem = build_thread_memory(
        snapshot,
        intake_result=intake_result,
        case_link_result=case_link_result,
        business_result=business_result,
        existing_thread_memory=cfg.get("existing_thread_memory"),
    )
    intelligence = build_case_intelligence_result(
        snapshot=snapshot,
        intake_result=intake_result,
        case_link_result=case_link_result,
        business_result=business_result or {},
        reply_result=reply_result or {},
        action_plan_result=action_plan_result or {},
        canonical_decision=canonical_decision,
        feedback_memory_seed=cfg.get("feedback_memory_seed"),
        current_note_state=cfg.get("current_note_state"),
        attachment_intelligence=att_intel,
        thread_memory=thread_mem,
        case_context_pack=effective_context_pack,
        preclassification_result=cfg.get("preclassification_result")
        if isinstance(cfg.get("preclassification_result"), dict)
        else None,
    )
    preflight_hot_state = _resolve_preflight_hot_state(cfg)
    if isinstance(preflight_hot_state, dict) and preflight_hot_state:
        intelligence = apply_hot_state_to_case_intelligence(intelligence, preflight_hot_state)

    case_guidance_result: dict[str, Any] = {}
    settings_for_guidance = settings_obj if isinstance(settings_obj, Settings) else None
    if settings_for_guidance is not None and settings_for_guidance.case_guidance_enabled:
        cu_pre = intelligence.get("case_understanding") or {}
        case_id_hint = str(cu_pre.get("case_id") or "").strip()
        cns = cfg.get("current_note_state") if isinstance(cfg.get("current_note_state"), dict) else {}
        if not case_id_hint:
            case_id_hint = str(cns.get("case_id") or "").strip()
        desk_note_id_hint = str(cns.get("desk_note_id") or cns.get("note_id") or "").strip()
        remote_ctx: dict[str, Any] = {}
        used_remote_case = False
        used_remote_note = False
        if settings_for_guidance.case_guidance_remote_state_enabled:
            client = cfg.get("daszek_client")
            remote_ctx, flags = fetch_remote_state_for_guidance(
                client,
                settings=settings_for_guidance,
                case_id_hint=case_id_hint,
                desk_note_id_hint=desk_note_id_hint,
                remote_enabled=True,
            )
            used_remote_case = bool(flags.get("used_remote_case_state"))
            used_remote_note = bool(flags.get("used_remote_note_state"))
        prompt_input = build_case_guidance_prompt_input(
            snapshot=snapshot,
            intake_result=intake_result,
            case_link_result=case_link_result,
            base_intelligence=intelligence,
            attachment_intelligence=att_intel,
            thread_memory=thread_mem,
            remote_state_context=remote_ctx,
        )
        model_g = settings_for_guidance.case_guidance_model or settings_for_guidance.groq_model
        verbose_g = bool(cfg.get("verbose", False))
        try:
            guidance_context_bundle: dict[str, Any] | None = None
            if isinstance(effective_context_pack, dict) and effective_context_pack:
                guidance_context_bundle = {
                    "case_id": str(effective_context_pack.get("case_id") or "").strip(),
                    "engagement_id": str(effective_context_pack.get("engagement_id") or "").strip(),
                    "case_context_pack": effective_context_pack,
                }
            raw_gr = run_case_guidance_reasoning(
                settings=settings_for_guidance,
                prompt_input=prompt_input,
                context_bundle=guidance_context_bundle,
                model=model_g,
                verbose=verbose_g,
            )
            cg = raw_gr["case_guidance"]
            sc = raw_gr["stage_call"]
            fallback_used = bool(sc.get("fallback_used"))
            intelligence = merge_case_guidance_into_intelligence(intelligence, cg)
            case_guidance_result = {
                "case_guidance": intelligence.get("case_guidance"),
                "execution_metadata": {
                    "stage_name": "case_guidance",
                    "shadow_only": True,
                    "guidance_enabled": True,
                    "used_remote_case_state": used_remote_case,
                    "used_remote_note_state": used_remote_note,
                    "used_existing_thread_memory": bool(thread_mem.get("thread_id")),
                    "source_mode": (intelligence.get("case_guidance") or {}).get("source_mode"),
                    "model_name": sc.get("model_name"),
                    "attempt_count": sc.get("attempt_count"),
                    "fallback_used": fallback_used,
                    "latency_ms": sc.get("latency_ms"),
                    "parse_status": "received",
                },
            }
            intelligence["case_guidance_result"] = case_guidance_result
            intelligence = validate_case_intelligence_result(intelligence)
        except (GroqClientError, OSError, RuntimeError, TypeError, ValueError) as exc:
            fb = fallback_case_guidance(reason=sanitize_text(str(exc)), base_intelligence=intelligence)
            intelligence = merge_case_guidance_into_intelligence(intelligence, fb)
            case_guidance_result = {
                "case_guidance": intelligence.get("case_guidance"),
                "execution_metadata": {
                    "stage_name": "case_guidance",
                    "shadow_only": True,
                    "guidance_enabled": True,
                    "used_remote_case_state": used_remote_case,
                    "used_remote_note_state": used_remote_note,
                    "used_existing_thread_memory": bool(thread_mem.get("thread_id")),
                    "source_mode": "fallback",
                    "model_name": model_g,
                    "attempt_count": 0,
                    "fallback_used": True,
                    "error": sanitize_text(str(exc)),
                    "parse_status": "fallback",
                },
            }
            intelligence["case_guidance_result"] = case_guidance_result
            intelligence = validate_case_intelligence_result(intelligence)
    else:
        skipped = build_skipped_case_guidance(reason="case_guidance_disabled", base_intelligence=intelligence)
        intelligence = merge_case_guidance_into_intelligence(intelligence, skipped)
        case_guidance_result = {
            "case_guidance": intelligence.get("case_guidance"),
            "execution_metadata": {
                "stage_name": "case_guidance",
                "shadow_only": True,
                "guidance_enabled": False,
                "used_remote_case_state": False,
                "used_remote_note_state": False,
                "used_existing_thread_memory": bool(thread_mem.get("thread_id")),
                "source_mode": "skipped",
                "model_name": getattr(settings_for_guidance, "case_guidance_model", None)
                or getattr(settings_for_guidance, "groq_model", "")
                if settings_for_guidance
                else "",
                "attempt_count": 0,
                "fallback_used": False,
                "parse_status": "skipped",
            },
        }
        intelligence["case_guidance_result"] = case_guidance_result
        intelligence = validate_case_intelligence_result(intelligence)

    confidence_domains = build_confidence_domains(
        intake_result=intake_result,
        case_link_result=case_link_result,
        business_result=business_result,
        attachment_intelligence=att_intel,
        thread_memory=thread_mem,
        action_plan_result=action_plan_result,
        case_intelligence_result=intelligence,
    )
    cal_profile = cfg.get("calibration_profile") if isinstance(cfg.get("calibration_profile"), dict) else None
    calibrated_thresholds = merge_threshold_overrides(None, cal_profile)
    review_routing = route_review(
        confidence_domains,
        intake_result=intake_result,
        case_intelligence_result=intelligence,
        thresholds=calibrated_thresholds,
    )
    intelligence = apply_confidence_to_intelligence(
        intelligence,
        confidence_domains=confidence_domains,
        review_routing=review_routing,
    )
    intelligence["attachment_intelligence"] = att_intel
    intelligence["thread_memory"] = thread_mem
    intelligence["automation_policy"] = build_automation_policy(
        review_routing=review_routing,
        confidence_domains=confidence_domains,
        thresholds=calibrated_thresholds,
        action_plan=action_plan_result,
    )
    intelligence["calibration_meta"] = calibration_meta(cal_profile)

    decision_pipeline_enabled = bool(getattr(settings_for_guidance, "decision_pipeline_enabled", False)) or bool(
        cfg.get("decision_pipeline_enabled")
    )
    understanding_enabled = bool(getattr(settings_for_guidance, "understanding_output_enabled", False)) or bool(
        cfg.get("understanding_output_enabled")
    )
    service_playbook_enabled = bool(
        getattr(settings_for_guidance, "service_request_playbook_enabled", False)
    ) or bool(cfg.get("service_request_playbook_enabled"))
    action_v2_enabled = bool(getattr(settings_for_guidance, "action_proposal_v2_enabled", False)) or bool(
        cfg.get("action_proposal_v2_enabled")
    )
    dry_run_only = bool(getattr(settings_for_guidance, "decision_pipeline_dry_run_only", True))
    if "decision_pipeline_dry_run_only" in cfg:
        dry_run_only = bool(cfg.get("decision_pipeline_dry_run_only"))

    understanding_output: dict[str, Any] | None = None
    if understanding_enabled or decision_pipeline_enabled:
        understanding_output = build_understanding_output(
            snapshot=snapshot,
            intake_result=intake_result,
            case_link_result=case_link_result,
            business_result=business_result or {},
            intelligence=intelligence,
            thread_memory=thread_mem,
            attachment_intelligence=att_intel,
            case_context_pack=effective_context_pack,
        )
        understanding_output, _uo_errors = validate_understanding_invariants(understanding_output)
        intelligence["understanding_output"] = understanding_output
        # SLICE-2A: the validator's findings were discarded here, so a contract violation it had
        # to correct was unreportable. Bounded error CODES only (no message body, no case data)
        # into the canonical Brain 1 metadata envelope. An empty list is explicit, never omitted,
        # so "validator ran and found nothing" is distinguishable from "validator did not run".
        _uo_meta = intelligence.get("execution_metadata")
        if not isinstance(_uo_meta, dict):
            _uo_meta = {}
            intelligence["execution_metadata"] = _uo_meta
        _uo_meta["understanding_validation_errors"] = [str(code)[:120] for code in (_uo_errors or [])][:20]
        # SLICE-3A: the planner's structured hand-off needs to know HOW this Understanding was
        # produced, not just what it says. This is the only point where all three inputs coexist:
        # the Understanding, the reasoning metadata that fed it, and the validator's findings.
        _uo_meta["case_understanding_provenance"] = build_case_understanding_provenance(
            understanding_output=understanding_output,
            business_execution_metadata=(business_result or {}).get("execution_metadata"),
            validation_errors=_uo_errors,
        )

    if decision_pipeline_enabled:
        dp = run_decision_pipeline(
            snapshot=snapshot,
            intake_result=intake_result,
            case_link_result=case_link_result,
            business_result=business_result or {},
            intelligence=intelligence,
            understanding_output=understanding_output,
            playbook_service_request_enabled=service_playbook_enabled,
        )
        intelligence["decision_pipeline"] = dp
        cand = (dp.get("outputs") or {}).get("decision_candidate") if isinstance(dp.get("outputs"), dict) else {}
        if isinstance(cand, dict):
            intelligence["decision_candidate"] = cand

        # Policy + ActionProposal v2 attach: single path via attach_policy_and_proposals (PR-4)
        # in run_shared_downstream_stages after intelligence returns.

    event_log = cfg.get("event_log")
    if isinstance(event_log, EventLog):
        case_id = str((intelligence.get("case_understanding") or {}).get("case_id") or "").strip()
        if not case_id:
            case_id = str((case_link_result or {}).get("selected_case_key") or "").strip()
        source_message = snapshot.get("source_message") or {}
        emit_signal_received(event_log, snapshot=snapshot, case_id=case_id)
        emit_case_intelligence(
            event_log,
            case_id=case_id,
            intelligence_result=intelligence,
            source_signal_id=str(source_message.get("message_id") or "").strip(),
            thread_id=str(source_message.get("thread_id") or "").strip(),
        )
    return intelligence


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _default_attachment_fetcher(settings: Settings) -> Callable[[str, str], bytes] | None:
    if not bool(getattr(settings, "attachment_extraction_enabled", True)):
        return None
    if not (getattr(settings, "has_google_refresh_flow", False) or getattr(settings, "has_google_access_token", False)):
        return None

    def _fetch(message_id: str, attachment_id: str) -> bytes:
        from google_gmail_api import fetch_gmail_attachment_bytes

        return fetch_gmail_attachment_bytes(settings, message_id=message_id, attachment_id=attachment_id, verbose=False)

    return _fetch


def hydrate_intelligence_seam_config(run_state: dict[str, Any], snapshot: dict[str, Any], stage_config: dict[str, Any]) -> None:
    """Populate Node B thread memory plus bounded projection inputs before intelligence stages."""
    settings_obj = stage_config.get("settings")
    signal_mode = str(getattr(settings_obj, "signal_runtime_mode", "active") or "active").strip().lower()
    from intelligence_shadow_profile import apply_intelligence_shadow_profile

    apply_intelligence_shadow_profile(stage_config, settings=settings_obj, signal_runtime_mode=signal_mode)
    stage_config["attachment_max_bytes"] = int(
        getattr(settings_obj, "attachment_extraction_max_bytes", 8_000_000) or 8_000_000
    )
    controls = run_state.get("runtime_controls") or {}
    if bool(controls.get("attachments_metadata_only")):
        # CLI wins for this run: no binary attachment fetch / extraction for LLM intake path.
        stage_config["attachment_max_bytes"] = 0
        stage_config["attachment_fetcher"] = None
    mailbox_runtime = run_state.get("mailbox_memory_runtime")
    stage_config["mailbox_memory_runtime"] = mailbox_runtime
    client = run_state.get("daszek_client")
    stage_config["daszek_client"] = client
    source_message = snapshot.get("source_message") or {}
    thread_id = str(source_message.get("thread_id") or "").strip()
    message_id = str(source_message.get("message_id") or "").strip()
    existing_thread_memory: dict[str, Any] = {}
    if mailbox_runtime is not None and thread_id:
        fetch_thread_memory = getattr(mailbox_runtime, "fetch_thread_memory", None)
        if callable(fetch_thread_memory):
            existing_thread_memory = fetch_thread_memory(thread_id) or {}
            if isinstance(existing_thread_memory, dict) and existing_thread_memory.get("thread_id"):
                stage_config["existing_thread_memory"] = existing_thread_memory
    if client and mailbox_runtime is not None and thread_id and not existing_thread_memory:
        try:
            remote = client.get_v2_thread_memory(thread_id)
            if isinstance(remote, dict) and remote.get("thread_id"):
                persist_thread_memory = getattr(mailbox_runtime, "persist_thread_memory", None)
                if callable(persist_thread_memory):
                    migrated = persist_thread_memory(
                        remote,
                        case_id=str(remote.get("case_id") or "").strip(),
                        message_id=message_id,
                        source_kind="daszek_migration",
                        only_if_absent=True,
                    )
                    if isinstance(migrated, dict) and migrated.get("thread_id"):
                        stage_config["existing_thread_memory"] = migrated
        except (DaszekClientError, RequestException) as exc:
            logger.warning("Daszek thread-memory projection unavailable; continuing from Node B", extra={"x": {
                "step": "fetch_thread_memory_projection",
                "error_type": type(exc).__name__,
            }})
        except Exception as exc:  # noqa: BLE001
            raise IntakeError(
                "Failed to migrate thread memory into Node B",
                context={"step": "migrate_thread_memory_to_mailbox_memory"},
            ) from exc
    if client:
        try:
            cal = client.get_v2_calibration_profile()
            if isinstance(cal, dict):
                stage_config["calibration_profile"] = cal
        except (DaszekClientError, RequestException) as exc:
            logger.warning("Daszek calibration projection unavailable; continuing without it", extra={"x": {
                "step": "fetch_calibration_profile",
                "error_type": type(exc).__name__,
            }})
    if settings_obj is not None and not bool(controls.get("attachments_metadata_only")):
        stage_config["attachment_fetcher"] = _default_attachment_fetcher(settings_obj)


def ingest_mailbox_memory(
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    runtime = config.get("mailbox_memory_runtime")
    if runtime is None:
        return {
            "enabled": False,
            "execution_metadata": {
                "stage_name": "mailbox_memory",
                "parse_status": "disabled",
            },
        }
    try:
        hvac_signals = config.get("hvac_signals")
        result = runtime.ingest_message(
            snapshot=snapshot,
            intake_result=intake_result,
            case_link_result=case_link_result,
            attachment_fetcher=config.get("attachment_fetcher") if callable(config.get("attachment_fetcher")) else None,
            attachment_max_bytes=int(config.get("attachment_max_bytes") or 8_000_000),
            refresh_document_intelligence=bool(config.get("refresh_document_intelligence", False)),
            hvac_signals=hvac_signals if isinstance(hvac_signals, dict) else None,
        )
        payload = result.to_dict()
        payload["execution_metadata"] = {
            "stage_name": "mailbox_memory",
            "parse_status": "completed" if payload.get("enabled") else "disabled",
        }
        return sanitize_for_storage(payload)
    except Exception as exc:  # noqa: BLE001
        raise IntakeError(
            "Mailbox memory ingest failed — partial state possible",
            context={"step": "ingest_mailbox_memory"}
        ) from exc


def finalize_mailbox_memory(
    *,
    snapshot: dict[str, Any],
    business_result: dict[str, Any] | None,
    reply_result: dict[str, Any] | None,
    action_plan_result: dict[str, Any] | None,
    case_intelligence_result: dict[str, Any] | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    runtime = config.get("mailbox_memory_runtime")
    pre_result = config.get("mailbox_memory_result")
    if runtime is None or not isinstance(pre_result, dict) or not pre_result.get("enabled"):
        return pre_result if isinstance(pre_result, dict) else {
            "enabled": False,
            "execution_metadata": {"stage_name": "mailbox_memory", "parse_status": "disabled"},
        }
    source_message = snapshot.get("source_message") or {}
    try:
        result = runtime.finalize_case(
            case_id=str(pre_result.get("case_id") or ""),
            message_id=str(source_message.get("message_id") or ""),
            thread_id=str(source_message.get("thread_id") or ""),
            business_result=business_result or {},
            reply_result=reply_result or {},
            action_plan_result=action_plan_result or {},
            case_intelligence_result=case_intelligence_result or {},
        )
        payload = result.to_dict()
        payload["execution_metadata"] = {
            "stage_name": "mailbox_memory",
            "parse_status": "completed" if payload.get("enabled") else "disabled",
            "phase": "finalized",
        }
        return sanitize_for_storage(payload)
    except Exception as exc:  # noqa: BLE001
        raise IntakeError(
            "Mailbox memory finalize failed — partial state possible",
            context={"step": "finalize_mailbox_memory"}
        ) from exc


def _fetch_latest_case_snapshot_hot_state(
    *,
    case_id: str,
    mailbox_memory_runtime: Any,
) -> dict[str, Any]:
    if not str(case_id or "").strip() or mailbox_memory_runtime is None:
        return {}
    store = getattr(mailbox_memory_runtime, "store", None)
    if store is None:
        return {}
    fetcher = getattr(store, "fetch_latest_case_snapshot_version", None)
    if not callable(fetcher):
        return {}
    row = fetcher(str(case_id).strip())
    if not isinstance(row, dict):
        return {}
    hot = row.get("snapshot_json")
    if not isinstance(hot, dict) or not hot:
        return {}
    from case_snapshot_hot_state_contract import CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION

    case_block = hot.get("case") if isinstance(hot.get("case"), dict) else {}
    schema_version = str(hot.get("schema_version") or "")
    if schema_version != CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION and not str(case_block.get("case_id") or "").strip():
        return {}
    return sanitize_for_storage(hot)


def _hot_state_open_questions(hot_state: dict[str, Any]) -> list[str]:
    questions: list[str] = []
    for item in list(hot_state.get("open_loops") or []):
        if isinstance(item, dict):
            text = str(item.get("description") or item.get("summary") or item.get("loop_id") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            questions.append(text)
    return questions


def overlay_case_context_pack_with_hot_state(
    case_context_pack: dict[str, Any] | None,
    hot_state: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(hot_state, dict) or not hot_state:
        return sanitize_for_storage(case_context_pack or {})
    pack = dict(case_context_pack or {})
    snapshot = dict(pack.get("snapshot") or {}) if isinstance(pack.get("snapshot"), dict) else {}
    case_block = hot_state.get("case") if isinstance(hot_state.get("case"), dict) else {}
    case_id = str(pack.get("case_id") or snapshot.get("case_id") or case_block.get("case_id") or "").strip()
    if case_id:
        pack["case_id"] = case_id
        snapshot["case_id"] = case_id
    case_key = str(case_block.get("case_key") or "").strip()
    if case_key:
        snapshot["case_key"] = case_key
    case_family = str(case_block.get("case_family") or "").strip()
    if case_family:
        snapshot["case_family"] = case_family
    lifecycle_status = str(case_block.get("lifecycle_status") or "").strip()
    if lifecycle_status:
        snapshot["status"] = lifecycle_status
    operational_status = str(case_block.get("operational_status") or "").strip()
    if operational_status:
        snapshot["operational_status"] = operational_status
    waiting_for = str(case_block.get("waiting_for") or "").strip()
    if waiting_for:
        snapshot["waiting_for"] = waiting_for
    priority = str(case_block.get("priority") or "").strip()
    if priority:
        snapshot["priority"] = priority
    summary_text = str(case_block.get("summary_text") or "").strip()
    if summary_text:
        snapshot["summary_text"] = summary_text
    snapshot["open_questions"] = _hot_state_open_questions(hot_state)
    if isinstance(hot_state.get("key_facts"), list):
        snapshot["key_facts"] = list(hot_state.get("key_facts") or [])
    if isinstance(hot_state.get("active_conflicts"), list):
        snapshot["conflicting_facts"] = list(hot_state.get("active_conflicts") or [])
    if isinstance(hot_state.get("documents_summary"), list):
        snapshot["latest_documents"] = list(hot_state.get("documents_summary") or [])
    recommended_next_step = str(hot_state.get("recommended_next_step") or "").strip()
    if recommended_next_step:
        snapshot["recommended_next_action"] = recommended_next_step
    pack["snapshot"] = snapshot
    pack["case_snapshot_hot_state"] = hot_state
    next_action = dict(pack.get("next_action") or {}) if isinstance(pack.get("next_action"), dict) else {}
    if recommended_next_step and not str(next_action.get("next_action") or "").strip():
        next_action["next_action"] = recommended_next_step
    if next_action:
        pack["next_action"] = next_action
    return sanitize_for_storage(pack)


def merge_hot_state_into_mailbox_memory_result(
    mailbox_memory_result: dict[str, Any],
    hot_state: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(mailbox_memory_result, dict):
        return mailbox_memory_result
    if not isinstance(hot_state, dict) or not hot_state:
        return mailbox_memory_result
    merged = dict(mailbox_memory_result)
    merged["case_snapshot_hot_state"] = sanitize_for_storage(hot_state)
    merged["context_pack"] = overlay_case_context_pack_with_hot_state(
        merged.get("context_pack") if isinstance(merged.get("context_pack"), dict) else {},
        hot_state,
    )
    return sanitize_for_storage(merged)


def load_hot_state_preflight_for_stage_config(
    *,
    mailbox_memory_result: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(mailbox_memory_result, dict) or not mailbox_memory_result.get("enabled"):
        return {}
    hot_state = _fetch_latest_case_snapshot_hot_state(
        case_id=str(mailbox_memory_result.get("case_id") or ""),
        mailbox_memory_runtime=config.get("mailbox_memory_runtime"),
    )
    if not hot_state:
        return {}
    base_context_pack = _resolve_effective_mailbox_context_pack(config)
    if not isinstance(base_context_pack, dict) or not base_context_pack:
        base_context_pack = (
            mailbox_memory_result.get("context_pack") if isinstance(mailbox_memory_result.get("context_pack"), dict) else {}
        )
    config["case_snapshot_hot_state_preflight"] = hot_state
    config["mailbox_memory_context_pack_preflight"] = overlay_case_context_pack_with_hot_state(
        base_context_pack,
        hot_state,
    )
    return hot_state


def inject_latest_hot_state_for_resolved_case(
    *,
    mailbox_memory_result: dict[str, Any],
    case_intelligence_result: dict[str, Any],
    mailbox_memory_runtime: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load latest persisted CaseSnapshotHotState for the case (if any) before policy / preview."""
    if not isinstance(mailbox_memory_result, dict) or not mailbox_memory_result.get("enabled"):
        return mailbox_memory_result, case_intelligence_result
    case_id = str(mailbox_memory_result.get("case_id") or "").strip()
    if not case_id:
        return mailbox_memory_result, case_intelligence_result
    hot = _fetch_latest_case_snapshot_hot_state(
        case_id=case_id,
        mailbox_memory_runtime=mailbox_memory_runtime,
    )
    if not hot:
        return mailbox_memory_result, case_intelligence_result
    mb = merge_hot_state_into_mailbox_memory_result(mailbox_memory_result, hot)
    ci = apply_hot_state_to_case_intelligence(
        case_intelligence_result if isinstance(case_intelligence_result, dict) else {},
        hot,
    )
    return mb, ci


def _shared_shadow_stage_outputs(
    *,
    intake_output: dict[str, Any],
    preclassification_result: dict[str, Any],
    case_link_result: dict[str, Any],
    business_result: dict[str, Any] | None,
    reply_result: dict[str, Any] | None,
    action_plan_result: dict[str, Any] | None,
    case_intelligence_result: dict[str, Any] | None,
    mailbox_memory_result: dict[str, Any] | None,
    canonical_signal_id: str = "",
) -> dict[str, Any]:
    return {
        "intake_result_final": intake_output,
        "preclassification_result": preclassification_result,
        "case_link_result": case_link_result,
        "business_reasoning_result": business_result,
        "reply_draft_result": reply_result,
        "action_plan_result": action_plan_result,
        "case_intelligence_result": case_intelligence_result,
        "mailbox_memory_result": mailbox_memory_result,
        "canonical_signal_id": str(canonical_signal_id or "").strip(),
    }


def build_projection_preview(
    intake_output: dict[str, Any],
    *,
    preclassification_result: dict[str, Any],
    case_link_result: dict[str, Any],
    business_result: dict[str, Any] | None,
    reply_result: dict[str, Any] | None,
    action_plan_result: dict[str, Any] | None,
    case_intelligence_result: dict[str, Any] | None,
    mailbox_memory_result: dict[str, Any] | None,
    canonical_signal_id: str = "",
) -> dict[str, Any]:
    """Build the v1 preview plus v2 shadow metadata."""
    return build_dash_preview(
        intake_output,
        stage_outputs=_shared_shadow_stage_outputs(
            intake_output=intake_output,
            preclassification_result=preclassification_result,
            case_link_result=case_link_result,
            business_result=business_result,
            reply_result=reply_result,
            action_plan_result=action_plan_result,
            case_intelligence_result=case_intelligence_result,
            mailbox_memory_result=mailbox_memory_result,
            canonical_signal_id=canonical_signal_id,
        ),
    )


def build_v2_projection(
    intake_output: dict[str, Any],
    *,
    run_id: str,
    preclassification_result: dict[str, Any],
    case_link_result: dict[str, Any],
    business_result: dict[str, Any] | None,
    reply_result: dict[str, Any] | None,
    action_plan_result: dict[str, Any] | None,
    case_intelligence_result: dict[str, Any] | None,
    mailbox_memory_result: dict[str, Any] | None,
    canonical_signal_id: str = "",
) -> dict[str, Any]:
    """Build the shadow-only Daszek v2 projection contract (via unified snapshot transport)."""
    from projection_snapshot_transport import build_operator_projection_snapshot, v2_projection_from_snapshot

    stage_outputs = _shared_shadow_stage_outputs(
        intake_output=intake_output,
        preclassification_result=preclassification_result,
        case_link_result=case_link_result,
        business_result=business_result,
        reply_result=reply_result,
        action_plan_result=action_plan_result,
        case_intelligence_result=case_intelligence_result,
        mailbox_memory_result=mailbox_memory_result,
        canonical_signal_id=canonical_signal_id,
    )
    snapshot = build_operator_projection_snapshot(intake_output, stage_outputs=stage_outputs, run_id=run_id)
    return v2_projection_from_snapshot(snapshot)


def _build_preclassified_intake_candidate(snapshot: dict[str, Any], preclassification_result: dict[str, Any]) -> dict[str, Any]:
    lane = str(preclassification_result.get("lane") or "intake_llm")
    message = snapshot.get("source_message") or {}
    thread_context = snapshot.get("thread_context") or {}
    case_link_candidates = [
        {
            "case_key": str(item.get("case_key") or "").strip(),
            "case_type": str(item.get("case_type") or "").strip() or "message_context",
            "match_confidence": float(item.get("match_confidence") or 0.0),
        }
        for item in (snapshot.get("case_link_candidates") or [])[:3]
        if isinstance(item, dict) and str(item.get("case_key") or "").strip()
    ]
    extracted_references = message.get("reference_tokens") or {}
    if not isinstance(extracted_references, dict):
        extracted_references = {}

    decision_action = "ignore"
    business_area = "general_admin"
    primary_signal = {
        "code": "system_noise",
        "name": "System noise",
        "description": "Deterministic preclassifier routed this message away from the full intake lane.",
        "business_significance": "No operational action appears necessary.",
    }
    reason = "Deterministic preclassifier marked the message as obvious noise."
    review = {"required": False, "flags": []}
    priority = "low"

    if lane == "reference_only":
        decision_action = "mark_reference"
        primary_signal = {
            "code": "reference_information",
            "name": "Reference information",
            "description": "The mail looks informational and should stay visible as reference only.",
            "business_significance": "Informational evidence should remain auditable without creating active work.",
        }
        reason = "Deterministic preclassifier marked this mail as reference-only."
    elif lane == "review_direct":
        decision_action = "review"
        business_area = "internal_coordination"
        primary_signal = {
            "code": "manual_review_required",
            "name": "Manual review required",
            "description": "Forwarded or low-signal content requires operator interpretation.",
            "business_significance": "Manual review is safer than a confident automated action.",
        }
        flags = ["insufficient_thread_context"]
        if bool((snapshot.get("routing_hints") or {}).get("self_forward")):
            flags.append("self_forward_requires_meaning_inference")
        review = {"required": True, "flags": flags}
        priority = "medium"
        reason = "Deterministic preclassifier sent this message directly to review."

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
            "thread_summary": "; ".join(str(item).strip() for item in (thread_context.get("reasons") or []) if str(item).strip()) or "Deterministic preclassification path.",
            "linked_case_candidates": case_link_candidates,
        },
        "business_area": business_area,
        "primary_signal": primary_signal,
        "secondary_signals": [],
        "case_assessment": {
            "case_family": "unknown",
            "is_new_case": False,
            "state_detected": "none",
            "state_change": {"detected": False},
        },
        "decision": {
            "action": decision_action,
            "action_rationale": reason,
        },
        "priority": priority,
        "confidence": {
            "signal_confidence": float(preclassification_result.get("confidence") or 0.8),
            "case_link_confidence": 0.0,
            "decision_confidence": float(preclassification_result.get("confidence") or 0.8),
            "extraction_confidence": 0.65,
        },
        "review": review,
        "reason": reason,
        "extracted_data": {
            "entities": {
                "people": [],
                "organizations": [],
                "locations": [],
                "products": [],
            },
            "dates": [],
            "amounts": [],
            "references": {
                "invoice_numbers": list(extracted_references.get("invoice") or []),
                "shipment_numbers": list(extracted_references.get("shipment") or []),
                "order_numbers": list(extracted_references.get("order") or []),
                "transaction_numbers": list(extracted_references.get("transaction") or []),
                "case_ids": list(extracted_references.get("case") or []),
            },
            "deadlines": [],
        },
    }


def _build_spine_first_intake_candidate(
    snapshot: dict[str, Any],
    preclassification_result: dict[str, Any],
) -> dict[str, Any]:
    """Thin intake seed for signal-active spine (Epik 2) — no intake LLM before journal/reconcile."""
    lane = str(preclassification_result.get("lane") or "intake_llm")
    candidate = _build_preclassified_intake_candidate(snapshot, preclassification_result)
    if lane != "intake_llm":
        return candidate

    message = snapshot.get("source_message") or {}
    subject = str(message.get("subject") or "").strip()
    candidate["business_area"] = "operations"
    candidate["primary_signal"] = {
        "code": "gmail_message_observed",
        "name": "Wiadomość Gmail",
        "description": "Sygnał Gmail — enrich w reconcile (business reasoning), bez intake LLM na wejściu.",
        "business_significance": "Signal spine first; projection z pamięci i downstream.",
    }
    candidate["decision"] = {
        "action": "review",
        "action_rationale": "Signal-active spine: intake LLM deferred; reconcile builds memory and projection.",
    }
    candidate["priority"] = "medium"
    candidate["review"] = {"required": True, "flags": ["insufficient_thread_context"]}
    candidate["reason"] = f"Spine-first path for operational mail: {subject or message.get('message_id', '')}"
    candidate["case_assessment"] = {
        "case_family": "unknown",
        "is_new_case": True,
        "state_detected": "new_signal",
        "state_change": {"detected": True},
    }
    return candidate


def _build_spine_first_intake_validation_result(
    *,
    snapshot: dict[str, Any],
    preclassification_result: dict[str, Any],
    lane_stage_plan: dict[str, Any],
) -> dict[str, Any]:
    """Validation bundle compatible with process_snapshot after intake LLM path."""
    intake_output = _build_spine_first_intake_candidate(snapshot, preclassification_result)
    final_output_origin = "spine_first_preclassified"
    intake_result_final = validate_intake_result(
        intake_output,
        final_output_origin=final_output_origin,
        normalization_notes=[],
        repair_notes=[],
        guardrail_flags=[],
    )
    intake_reasoning_result = {
        "raw_output_text": "",
        "response_json": sanitize_for_storage(intake_output),
        "request_meta": {"final_inference_mode": final_output_origin, "spine_first": True},
        "input_variants": [],
        "execution_metadata": {
            "stage_name": "intake_reasoning",
            "skipped": True,
            "reason": "INTAKE_LLM_BEFORE_SIGNAL=0",
            "lane_stage_plan": lane_stage_plan,
        },
        "second_pass_applied": False,
    }
    return {
        "is_valid": True,
        "intake_reasoning_result": intake_reasoning_result,
        "intake_output": intake_output,
        "intake_result_final": intake_result_final,
        "guardrail_flags": [],
        "final_output_origin": final_output_origin,
        "validation_trace": SimpleNamespace(
            normalized_candidate=None,
            repair_applied=False,
            normalization_applied=False,
            normalization_notes=[],
            repair_notes=[],
            final_output_origin=final_output_origin,
        ),
        "validation": SimpleNamespace(parse_ok=True, schema_ok=True, semantic_ok=True, errors=[]),
        "original_action": str(intake_output.get("decision", {}).get("action") or "review"),
        "raw_valid": True,
        "normalized_valid": False,
        "repaired_valid": False,
    }


def _build_stage_record(
    *,
    message_id: str,
    preclassification_result: dict[str, Any],
    lane_stage_plan: dict[str, Any],
    intake_reasoning_result: dict[str, Any],
    intake_result_final: dict[str, Any] | None,
    case_link_result: dict[str, Any] | None,
    business_result: dict[str, Any] | None,
    reply_result: dict[str, Any] | None,
    action_plan_result: dict[str, Any] | None,
    case_intelligence_result: dict[str, Any] | None,
    mailbox_memory_result: dict[str, Any] | None,
    preview: dict[str, Any] | None,
    v2_projection: dict[str, Any] | None,
    review_decision: dict[str, Any],
) -> dict[str, Any]:
    v2_projection = v2_projection or {}
    return {
        "message_id": message_id,
        "preclassification_result": sanitize_for_storage(preclassification_result),
        "intake_result_raw": sanitize_for_storage(
            {
                "response_text": intake_reasoning_result.get("raw_output_text"),
                "response_json": intake_reasoning_result.get("response_json"),
                "execution_metadata": intake_reasoning_result.get("execution_metadata"),
            }
        ),
        "intake_result_final": sanitize_for_storage(intake_result_final),
        "case_link_result": sanitize_for_storage(case_link_result),
        "business_reasoning_result": sanitize_for_storage(business_result),
        "reply_draft_result": sanitize_for_storage(reply_result),
        "action_plan_result": sanitize_for_storage(action_plan_result),
        "case_intelligence_result": sanitize_for_storage(case_intelligence_result),
        "mailbox_memory_result": sanitize_for_storage(mailbox_memory_result),
        "case_guidance_result": sanitize_for_storage((case_intelligence_result or {}).get("case_guidance_result")),
        "projection_preview": sanitize_for_storage(preview),
        "signal_projection": sanitize_for_storage(v2_projection.get("signal_projection")),
        "case_patch": sanitize_for_storage(v2_projection.get("case_patch")),
        "desk_note_patch": sanitize_for_storage(v2_projection.get("desk_note_patch")),
        "decision_trace": sanitize_for_storage(v2_projection.get("decision_trace")),
        "review_decision": sanitize_for_storage(review_decision),
        "execution_metadata": sanitize_for_storage(
            {
                "preclassification_lane": preclassification_result.get("lane"),
                "lane_stage_plan": lane_stage_plan,
                "intake_reasoning": intake_reasoning_result.get("execution_metadata"),
                "case_linking": (case_link_result or {}).get("execution_metadata"),
                "business_reasoning": (business_result or {}).get("execution_metadata"),
                "reply_drafter": (reply_result or {}).get("execution_metadata"),
                "action_planner": (action_plan_result or {}).get("execution_metadata"),
                "case_intelligence": (case_intelligence_result or {}).get("execution_metadata"),
                "mailbox_memory": (mailbox_memory_result or {}).get("execution_metadata"),
                "case_guidance": ((case_intelligence_result or {}).get("case_guidance_result") or {}).get(
                    "execution_metadata"
                ),
            }
        ),
    }


def _stage_artifact_status(result: Any) -> str:
    if result is None:
        return "missing"
    if not isinstance(result, dict):
        return "completed"
    execution_metadata = result.get("execution_metadata") if isinstance(result.get("execution_metadata"), dict) else {}
    parse_status = str(execution_metadata.get("parse_status") or "").strip()
    if parse_status == "skipped_for_lane":
        return "skipped"
    if parse_status == "fallback":
        return "fallback"
    if execution_metadata.get("error"):
        return "error"
    if not result:
        return "missing"
    return "completed"


def _build_stage_artifact_entry(
    *,
    message_id: str,
    stage_name: str,
    lane: str,
    result: Any,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "stage_name": stage_name,
        "lane": lane,
        "status": _stage_artifact_status(result),
        "result": sanitize_for_storage(result),
    }


def _append_stage_artifacts(run_state: dict[str, Any], stage_record: dict[str, Any]) -> None:
    message_id = str(stage_record.get("message_id") or "").strip()
    lane = str((stage_record.get("preclassification_result") or {}).get("lane") or "intake_llm")
    artifact_map = {
        "preclassification_results": ("preclassification", stage_record.get("preclassification_result")),
        "intake_results_raw": ("intake_raw", stage_record.get("intake_result_raw")),
        "intake_results_final": ("intake_final", stage_record.get("intake_result_final")),
        "case_link_results": ("case_link", stage_record.get("case_link_result")),
        "business_reasoning_results": ("business_reasoning", stage_record.get("business_reasoning_result")),
        "reply_draft_results": ("reply_draft", stage_record.get("reply_draft_result")),
        "action_plan_results": ("action_plan", stage_record.get("action_plan_result")),
        "case_intelligence_results": ("case_intelligence", stage_record.get("case_intelligence_result")),
        "mailbox_memory_results": ("mailbox_memory", stage_record.get("mailbox_memory_result")),
        "case_guidance_results": ("case_guidance", stage_record.get("case_guidance_result")),
        "attachment_intelligence_results": ("attachment_intelligence", (stage_record.get("case_intelligence_result") or {}).get("attachment_intelligence")),
        "thread_memory_results": ("thread_memory", (stage_record.get("case_intelligence_result") or {}).get("thread_memory")),
        "projection_previews": ("projection_preview", stage_record.get("projection_preview")),
        "signal_projections": ("signal_projection", stage_record.get("signal_projection")),
        "case_patches": ("case_patch", stage_record.get("case_patch")),
        "desk_note_patches": ("desk_note_patch", stage_record.get("desk_note_patch")),
        "decision_traces": ("decision_trace", stage_record.get("decision_trace")),
        "review_decisions": ("review_decision", stage_record.get("review_decision")),
        "execution_metadata": ("execution_metadata", stage_record.get("execution_metadata")),
    }
    for artifact_key, (stage_name, payload) in artifact_map.items():
        append_jsonl(
            run_state["artifacts"][artifact_key],
            _build_stage_artifact_entry(
                message_id=message_id,
                stage_name=stage_name,
                lane=lane,
                result=payload,
            ),
        )


def _persist_stage_record(run_state: dict[str, Any], stage_record: dict[str, Any]) -> None:
    append_jsonl(run_state["stage_records_path"], stage_record)
    _append_stage_artifacts(run_state, stage_record)


def _record_gmail_raw_observation(
    *,
    settings: Settings,
    run_state: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    from gmail_signal_adapter import build_gmail_raw_observation
    from raw_observation_journal import RawObservationJournal

    observation = build_gmail_raw_observation(
        snapshot=snapshot,
        created_by_runtime="gmail_intake.process_snapshot",
    )
    store = _resolve_raw_observation_store(run_state)
    if store is None:
        return {"observation": observation, "append_result": None}
    journal = RawObservationJournal(
        store,
        jsonl_mirror_enabled=bool(getattr(settings, "signal_journal_jsonl_mirror_enabled", False)),
        jsonl_mirror_path=_raw_observation_jsonl_path(settings),
    )
    append_result = journal.append(observation)
    return {
        "observation": append_result.observation,
        "append_result": append_result,
    }


def _resolve_raw_observation_store(run_state: dict[str, Any]) -> Any | None:
    mailbox_runtime = run_state.get("mailbox_memory_runtime")
    if mailbox_runtime is not None:
        return getattr(mailbox_runtime, "store", None)
    return run_state.get("signal_store")


def _raw_observation_jsonl_path(settings: Settings) -> Path:
    blob_root = Path(getattr(settings, "mailbox_memory_blob_root")).resolve()
    target = blob_root.parent / "signal_runtime" / "raw_observations.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _build_gmail_triage_result(raw_observation: Any) -> dict[str, Any]:
    from observation_triage import triage_gmail_observation

    return sanitize_for_storage(triage_gmail_observation(raw_observation))


def select_live_period_items(
    *,
    settings: Settings,
    run_state: dict[str, Any],
    selector: dict[str, Any],
    query: str,
    limit: int,
    page_size: int,
    model: str | None,
    verbose: bool,
    gmail_source: str,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    next_page_token = ""

    while len(selected) < limit:
        if check_runtime_stop_conditions(run_state, stage="selection"):
            break
        remaining = limit - len(selected)
        try:
            payload = search_emails(
                settings,
                query=query,
                max_results=min(page_size, remaining),
                next_page_token=next_page_token or None,
                model=model,
                verbose=verbose,
                gmail_source=gmail_source,
            )
        except GroqClientError as exc:
            record_error(
                run_state,
                stage="selection",
                message_id="",
                error=str(exc),
                details={"query": query, "limit": limit, "page_size": page_size, "selected_so_far": len(selected)},
            )
            check_runtime_stop_conditions(run_state, stage="selection")
            break

        page_items = [item for item in payload["responses"] if isinstance(item, dict)]
        if not page_items:
            break

        selected.extend(page_items)
        next_page_token = str(payload.get("next_page_token") or "").strip()
        run_state["summary"]["items_selected"] = len(selected)
        write_json(
            run_state["artifacts"]["selection"],
            {
                "selector": selector,
                "selected_messages": sanitize_for_storage(selected),
                "selection_progress": {
                    "query": query,
                    "page_size": page_size,
                    "selected_so_far": len(selected),
                    "next_page_token_present": bool(next_page_token),
                },
            },
        )
        update_checkpoint(run_state, last_message_id=extract_message_id(page_items[-1]))

        if not next_page_token:
            break

    return selected[:limit]


def fetch_context_messages(
    settings: Settings,
    *,
    source_message: dict[str, Any],
    model: str | None,
    verbose: bool,
    context_limit: int,
    gmail_source: str,
) -> list[dict[str, Any]]:
    """Fetch a small set of additional context messages for the same subject."""
    if context_limit <= 0:
        return []

    source_message_id = extract_message_id(source_message)
    context_messages: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    thread_id = str(source_message.get("thread_id") or source_message.get("threadId") or "").strip()
    if thread_id:
        try:
            thread_messages = get_thread_messages(
                settings,
                thread_id=thread_id,
                model=model,
                verbose=verbose,
                gmail_source=gmail_source,
            )
        except GroqClientError:
            thread_messages = []

        for item in thread_messages:
            item_id = extract_message_id(item)
            if not item_id or item_id == source_message_id or item_id in seen_ids:
                continue
            context_messages.append(item)
            seen_ids.add(item_id)
            if len(context_messages) >= context_limit:
                return context_messages[:context_limit]

    query = build_related_context_query(source_message)
    if not query:
        return context_messages

    try:
        payload = search_emails(
            settings,
            query=query,
            max_results=max(context_limit + 2, 3),
            model=model,
            verbose=verbose,
            gmail_source=gmail_source,
        )
    except GroqClientError:
        return context_messages

    for item in payload["responses"]:
        item_id = extract_message_id(item)
        if not item_id or item_id == source_message_id or item_id in seen_ids:
            continue
        try:
            context_messages.append(
                read_email(
                    settings,
                    message_id=item_id,
                    model=model,
                    verbose=verbose,
                    gmail_source=gmail_source,
                )
            )
            seen_ids.add(item_id)
        except GroqClientError:
            continue
        if len(context_messages) >= context_limit:
            break

    return context_messages


def build_selector(args: argparse.Namespace, *, batch_items: list[Any] | None) -> dict[str, Any]:
    gmail_source = normalize_gmail_source(getattr(args, "gmail_source", DEFAULT_GMAIL_SOURCE))
    if args.command == "message":
        return {"type": "message", "message_id": args.message_id, "gmail_source": gmail_source}
    if args.command == "batch":
        return {
            "type": "batch",
            "batch_file": args.batch_file,
            "batch_mode": detect_batch_mode(batch_items or []),
            "gmail_source": gmail_source,
        }
    if args.command == "period":
        return {
            "type": "period",
            "query": args.query,
            "days": args.days,
            "limit": args.limit,
            "page_size": args.page_size,
            "gmail_source": gmail_source,
        }
    if args.command == "shadow-run":
        if args.batch_file:
            return {
                "type": "shadow_batch",
                "batch_file": args.batch_file,
                "batch_mode": detect_batch_mode(batch_items or []),
                "gmail_source": gmail_source,
            }
        return {
            "type": "shadow_period",
            "query": args.query,
            "days": args.days,
            "limit": args.limit,
            "page_size": args.page_size,
            "gmail_source": gmail_source,
        }
    if args.command == "rerun":
        return {"type": "rerun", "gmail_source": gmail_source}
    raise ValueError(f"Unsupported command: {args.command}")


def init_run_state(
    *,
    run_id: str,
    run_dir: Path,
    command: str,
    selector: dict[str, Any],
    mailbox: str,
    model: str,
    schema_path: str | None,
    source_run: str | None,
    push_daszek: bool,
    runtime_controls: dict[str, Any],
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest_record(
        run_id=run_id,
        command=command,
        selector=selector,
        mailbox=mailbox,
        model=model,
        schema_path=schema_path,
        source_run=source_run,
        push_daszek=push_daszek,
        runtime_controls=runtime_controls,
    )
    artifacts = build_run_artifact_paths(run_dir)

    run_state = {
        "run_id": run_id,
        "run_dir": run_dir,
        "artifacts": artifacts,
        "manifest": manifest,
        "manifest_path": artifacts["manifest"],
        "checkpoint_path": artifacts["checkpoint"],
        "summary_path": artifacts["summary"],
        "source_messages_path": artifacts["source_messages"],
        "intake_inputs_path": artifacts["intake_inputs"],
        "stage_records_path": artifacts["stage_records"],
        "model_raw_path": artifacts["model_raw_responses"],
        "model_normalized_path": artifacts["model_normalized_candidates"],
        "model_repair_path": artifacts["model_repair_attempts"],
        "validation_path": artifacts["validation_results"],
        "intake_outputs_path": artifacts["intake_outputs"],
        "dash_preview_path": artifacts["dash_preview"],
        "daszek_push_path": artifacts["daszek_push_results"],
        "daszek_v2_push_path": artifacts["daszek_v2_push_results"],
        "daszek_v3_feed_push_path": artifacts["daszek_v3_feed_push_results"],
        "errors_path": artifacts["errors"],
        "summary": empty_run_summary(),
        "projection_route_overlays": {},
        "runtime_controls": runtime_controls,
        "started_monotonic": time.monotonic(),
        "_record_error": record_error,
    }

    write_json(run_state["manifest_path"], manifest)
    initialize_checkpoint(run_state)
    return run_state


def attach_observability_runtime(run_state: dict[str, Any], settings: Settings, *, command_name: str) -> ObservabilityRuntime:
    runtime = ObservabilityRuntime(
        run_id=str(run_state["run_id"]),
        run_dir=Path(run_state["run_dir"]),
        command_name=command_name,
        enabled=bool(getattr(settings, "gmail_agent_otel_enabled", False)),
        local_mirror_enabled=bool(getattr(settings, "gmail_agent_otel_local_mirror_enabled", True)),
        service_name=str(getattr(settings, "otel_service_name", "") or "gmail-agent"),
        otlp_endpoint=str(getattr(settings, "otel_exporter_otlp_endpoint", "") or ""),
        otlp_headers=str(getattr(settings, "otel_exporter_otlp_headers", "") or ""),
    )
    run_state["observability"] = runtime
    run_state["telemetry_path"] = run_state["artifacts"].get("telemetry_events")
    _sync_observability_summary(run_state)
    write_json(run_state["manifest_path"], run_state["manifest"])
    return runtime


def _sync_observability_summary(run_state: dict[str, Any]) -> None:
    runtime = run_state.get("observability")
    if not isinstance(runtime, ObservabilityRuntime):
        return
    telemetry_summary = runtime.summary()
    run_state["summary"].update(telemetry_summary)
    run_state["manifest"]["telemetry"] = sanitize_for_storage(telemetry_summary)


@contextmanager
def observed_run_span(
    run_state: dict[str, Any],
    span_name: str,
    *,
    stage_name: str,
    case_id: str = "",
    message_id: str = "",
    thread_id: str = "",
    signal_id: str = "",
    trace_id: str = "",
    extra: dict[str, Any] | None = None,
):
    runtime = run_state.get("observability")
    if not isinstance(runtime, ObservabilityRuntime):
        with nullcontext():
            yield {}
        return
    with runtime.span(
        span_name,
        case_id=case_id,
        message_id=message_id,
        thread_id=thread_id,
        signal_id=signal_id,
        trace_id=trace_id,
        stage_name=stage_name,
        extra=extra,
    ) as span_ctx:
        yield span_ctx


def attach_daszek_client(run_state: dict[str, Any], settings: Settings) -> None:
    client = DaszekClient(settings, observability_runtime=run_state.get("observability"))
    client.login()
    client.list_tasks(refresh=True)
    run_state["daszek_client"] = client


def annotate_env_metadata(run_state: dict[str, Any], settings: Settings) -> None:
    """Persist env-source metadata and hygiene warnings in the run manifest."""
    local_env_files = [str(path.resolve()) for path in existing_env_candidates()]
    run_state["manifest"]["env_source"] = str(settings.env_path.resolve()) if settings.env_path else "environment_only"
    run_state["manifest"]["local_env_files"] = local_env_files
    write_json(run_state["manifest_path"], run_state["manifest"])


def perform_run_preflight(
    *,
    run_state: dict[str, Any],
    settings: Settings,
    require_google: bool,
    check_daszek: bool,
    model: str | None,
    verbose: bool,
    gmail_source: str,
) -> bool:
    """Run up-front access checks and persist their outcome in the manifest."""
    preflight = run_state["manifest"].get("preflight") or empty_preflight_summary()
    deprecated_env = TOOL_DIR / ".env.local"
    if deprecated_env.is_file():
        preflight["warnings"].append(
            "Legacy `tools/gmail_audit/.env.local` exists on disk but is never loaded. "
            "Merge any needed values into `tools/gmail_audit/.env` and delete `.env.local`."
        )
        preflight["status"] = PREFLIGHT_STATUS_WARNING

    google_auth_check = build_google_auth_check(settings, require_google=require_google)
    preflight["checks"]["config"] = build_doctor_config_check(settings, model_override=model)
    preflight["checks"]["otel"] = build_otel_check(settings)
    preflight["checks"]["ocr"] = build_ocr_check(settings)
    preflight["checks"]["pgvector"] = build_pgvector_check(settings)
    preflight["checks"]["vector_retrieval"] = build_vector_retrieval_readiness_check(settings)
    if bool(getattr(settings, "mailbox_memory_vector_enabled", False)):
        vr_pf = preflight["checks"]["vector_retrieval"]
        vps_pf = str(vr_pf.get("vector_path_status") or "")
        if vps_pf in {"vector_path_unavailable", "vector_path_failed"} or vr_pf.get("status") == CHECK_STATUS_FAILED:
            warn = (
                f"MAILBOX_MEMORY_VECTOR_ENABLED=1 but vector retrieval readiness is {vps_pf}: "
                f"{str(vr_pf.get('reason') or vr_pf.get('embedding_error') or '').strip()}"
            )
            preflight["warnings"].append(warn)
            if preflight["status"] == PREFLIGHT_STATUS_OK:
                preflight["status"] = PREFLIGHT_STATUS_WARNING
    preflight["checks"]["docling"] = build_docling_check(settings)
    preflight["checks"]["drive"] = build_google_drive_check(settings, check_access=False)
    preflight["checks"]["google_auth"] = google_auth_check
    preflight["checks"]["google_direct"] = {"status": CHECK_STATUS_SKIPPED}
    preflight["checks"]["gmail_source"] = {
        "status": CHECK_STATUS_OK,
        "value": normalize_gmail_source(gmail_source),
    }
    if require_google and google_auth_check["status"] == CHECK_STATUS_FAILED:
        preflight["status"] = PREFLIGHT_STATUS_FAILED
        mark_stop_reason(run_state, reason="preflight_failed", details={"check": "google_auth"})
        record_error(
            run_state,
            stage="preflight",
            message_id="",
            error=str(google_auth_check.get("error") or "Google auth preflight failed."),
        )
        run_state["manifest"]["preflight"] = sanitize_for_storage(preflight)
        write_json(run_state["manifest_path"], run_state["manifest"])
        return False

    if require_google:
        direct_check = run_google_direct_auth_check(settings)
        preflight["checks"]["google_direct"] = direct_check
        preflight["checks"]["google_auth"] = build_google_auth_check(
            settings,
            require_google=True,
        )
        if direct_check["status"] == CHECK_STATUS_FAILED:
            preflight["status"] = PREFLIGHT_STATUS_FAILED
            mark_stop_reason(run_state, reason="preflight_failed", details={"check": "google_direct"})
            record_error(
                run_state,
                stage="preflight",
                message_id="",
                error=str(direct_check.get("error") or "Direct Google auth check failed."),
            )
            run_state["manifest"]["preflight"] = sanitize_for_storage(preflight)
            write_json(run_state["manifest_path"], run_state["manifest"])
            return False
        try:
            profile = get_profile(
                settings,
                model=model,
                verbose=verbose,
                gmail_source=gmail_source,
            )
            preflight["checks"]["google_auth"] = build_google_auth_check(
                settings,
                require_google=True,
            )
            mailbox = infer_mailbox(profile)
            run_state["manifest"]["mailbox"] = mailbox
            preflight["checks"]["gmail"] = {
                "status": CHECK_STATUS_OK,
                "mailbox": mailbox,
                "source": normalize_gmail_source(gmail_source),
            }
        except GroqClientError as exc:
            preflight["status"] = PREFLIGHT_STATUS_FAILED
            preflight["checks"]["gmail"] = {
                "status": CHECK_STATUS_FAILED,
                "error": sanitize_text(str(exc)),
                "source": normalize_gmail_source(gmail_source),
            }
            mark_stop_reason(run_state, reason="preflight_failed", details={"check": "gmail"})
            record_error(run_state, stage="preflight", message_id="", error=str(exc))
            run_state["manifest"]["preflight"] = sanitize_for_storage(preflight)
            write_json(run_state["manifest_path"], run_state["manifest"])
            return False
    else:
        preflight["checks"]["gmail"] = {"status": CHECK_STATUS_SKIPPED}

    if check_daszek:
        try:
            attach_daszek_client(run_state, settings)
            task_count = len(run_state["daszek_client"].list_tasks())
            preflight["checks"]["daszek"] = {
                "status": CHECK_STATUS_OK,
                "v1_task_count": task_count,
                "task_count": task_count,
                "legacy_v1_tasks_note": "Narrow v1 /tasks list; v2 operator surface is separate (see doctor daszek_v2_operator_surface).",
                "v1_task_count_interpretation": (
                    "v1_task_count=0 does not imply an empty Daszek; v2 ingest/projections and desk read APIs are separate."
                ),
            }
        except DaszekClientError as exc:
            preflight["status"] = PREFLIGHT_STATUS_FAILED
            preflight["checks"]["daszek"] = {"status": CHECK_STATUS_FAILED, "error": sanitize_text(str(exc))}
            mark_stop_reason(run_state, reason="preflight_failed", details={"check": "daszek"})
            record_error(run_state, stage="daszek_push", message_id="", error=str(exc))
            run_state["manifest"]["preflight"] = sanitize_for_storage(preflight)
            write_json(run_state["manifest_path"], run_state["manifest"])
            return False
    else:
        preflight["checks"]["daszek"] = {"status": CHECK_STATUS_SKIPPED}

    run_state["manifest"]["preflight"] = sanitize_for_storage(preflight)
    write_json(run_state["manifest_path"], run_state["manifest"])
    return True


def initialize_checkpoint(run_state: dict[str, Any]) -> None:
    update_checkpoint(run_state)


def update_checkpoint(run_state: dict[str, Any], *, last_message_id: str | None = None) -> None:
    checkpoint = build_checkpoint_record(run_state, last_message_id=last_message_id or "")
    write_json(run_state["checkpoint_path"], checkpoint)


def mark_stop_reason(
    run_state: dict[str, Any],
    *,
    reason: str,
    details: dict[str, Any] | None = None,
) -> None:
    summary = run_state["summary"]
    if not summary["stop_reason"]:
        summary["stop_reason"] = reason
        summary["stop_details"] = sanitize_for_storage(details or {})
    summary["aborted"] = True


def check_runtime_stop_conditions(
    run_state: dict[str, Any],
    *,
    stage: str,
    message_id: str = "",
) -> bool:
    """Return True when the run should stop cleanly because a runtime limit was reached."""
    summary = run_state["summary"]
    controls = run_state.get("runtime_controls") or {}
    elapsed_seconds = round(max(0.0, time.monotonic() - float(run_state.get("started_monotonic") or 0.0)), 2)

    timebox_seconds = int(controls.get("timebox_seconds") or 0)
    if timebox_seconds > 0 and elapsed_seconds >= timebox_seconds:
        mark_stop_reason(
            run_state,
            reason="timebox_reached",
            details={
                "stage": stage,
                "message_id": message_id,
                "elapsed_seconds": elapsed_seconds,
                "timebox_seconds": timebox_seconds,
            },
        )
        return True

    max_failures = int(controls.get("max_failures") or 0)
    if max_failures > 0 and summary["items_failed"] >= max_failures:
        mark_stop_reason(
            run_state,
            reason="max_failures_reached",
            details={
                "stage": stage,
                "message_id": message_id,
                "items_failed": summary["items_failed"],
                "max_failures": max_failures,
            },
        )
        return True

    max_consecutive_failures = int(controls.get("max_consecutive_failures") or 0)
    if max_consecutive_failures > 0 and summary["consecutive_failures"] >= max_consecutive_failures:
        mark_stop_reason(
            run_state,
            reason="max_consecutive_failures_reached",
            details={
                "stage": stage,
                "message_id": message_id,
                "consecutive_failures": summary["consecutive_failures"],
                "max_consecutive_failures": max_consecutive_failures,
            },
        )
        return True

    return bool(summary["aborted"] and summary["stop_reason"])


def latest_failure_details(run_state: dict[str, Any], *, fallback_stage: str, fallback_message_id: str = "") -> dict[str, Any]:
    failed_items = run_state["summary"].get("failed_items") or []
    if failed_items:
        last_failure = failed_items[-1]
        return {
            "stage": str(last_failure.get("stage") or fallback_stage),
            "message_id": str(last_failure.get("message_id") or fallback_message_id),
            "error": str(last_failure.get("error") or ""),
        }
    return {
        "stage": fallback_stage,
        "message_id": fallback_message_id,
    }
def push_preview_to_daszek(
    *,
    run_state: dict[str, Any],
    preview: dict[str, Any],
    keep_going: bool,
    action_plan_result: dict[str, Any] | None = None,
    intake_result_final: dict[str, Any] | None = None,
    policy_report: dict[str, Any] | None = None,
) -> bool:
    client = run_state.get("daszek_client")
    if client is None:
        return True
    if not run_state["manifest"].get("daszek_push_requested"):
        return True

    policy = evaluate_live_push_policy(
        surface="v1_preview_tasks",
        manifest=run_state["manifest"],
        action_plan_result=action_plan_result,
        intake_result_final=intake_result_final,
        policy_report=policy_report,
    )
    append_jsonl(
        run_state["daszek_push_path"],
        sanitize_for_storage(
            {
                "record_type": "push_policy",
                "surface": "v1",
                "message_id": str(preview.get("message_id") or ""),
                "allowed": policy.allowed,
                "push_policy_reason": policy.push_policy_reason,
                "push_policy_detail": policy.push_policy_detail,
            }
        ),
    )
    if not policy.allowed:
        run_state["summary"]["items_v1_push_blocked_by_policy"] += 1
        return True

    try:
        results = client.push_preview(preview)
    except DaszekClientError as exc:
        run_state["summary"]["items_push_failed"] += 1
        run_state["summary"]["items_failed"] += 1
        record_error(
            run_state,
            stage="daszek_push",
            message_id=str(preview.get("message_id") or ""),
            error=str(exc),
            details={"decision_action": preview.get("decision_action")},
        )
        if check_runtime_stop_conditions(
            run_state,
            stage="daszek_push",
            message_id=str(preview.get("message_id") or ""),
        ):
            return False
        return keep_going

    for result in results:
        append_jsonl(
            run_state["daszek_push_path"],
            {
                "request_id": result.request_id,
                "status": result.status,
                "object_type": result.object_type,
                "message_id": result.message_id,
                "task_id": result.task_id,
                "push_policy_reason": policy.push_policy_reason,
                "details": sanitize_for_storage(result.details),
            },
        )
        if result.status == "created":
            run_state["summary"]["items_pushed"] += 1
        elif result.status == "skipped_existing":
            run_state["summary"]["items_push_skipped"] += 1

    return True


def record_error(
    run_state: dict[str, Any],
    *,
    stage: str,
    message_id: str,
    error: str,
    details: dict[str, Any] | None = None,
    category_override: str | None = None,
) -> None:
    summary = run_state["summary"]
    if stage not in summary["errors_by_stage"]:
        summary["errors_by_stage"][stage] = 0
    summary["errors_by_stage"][stage] += 1
    category = category_override or categorize_error(stage=stage, error=error)
    if category not in summary["errors_by_category"]:
        summary["errors_by_category"][category] = 0
    summary["errors_by_category"][category] += 1

    payload = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "message_id": message_id,
        "stage": stage,
        "category": category,
        "error": sanitize_text(error),
        "details": sanitize_for_storage(details or {}),
    }
    append_jsonl(run_state["errors_path"], payload)
    summary["consecutive_failures"] += 1
    summary["failed_items"].append(
        {
            "message_id": message_id,
            "stage": stage,
            "error": payload["error"],
        }
    )


def finalize_run(run_state: dict[str, Any]) -> int:
    # Ensure run artifacts remain serializable and safe to snapshot.
    # Runtime objects (DB clients, HTTP sessions, telemetry runtimes) must not leak into summary/manifest writes.
    manifest = run_state.setdefault("manifest", {})
    if run_state.get("daszek_client") is not None:
        manifest["daszek_push_connected"] = True
    else:
        preflight = manifest.get("preflight") if isinstance(manifest.get("preflight"), dict) else {}
        checks = preflight.get("checks") if isinstance(preflight.get("checks"), dict) else {}
        daszek_check = checks.get("daszek") if isinstance(checks.get("daszek"), dict) else {}
        if str(daszek_check.get("status") or "") == "ok":
            manifest["daszek_push_connected"] = True

    for key in ("mailbox_memory_runtime", "observability", "daszek_client"):
        run_state.pop(key, None)

    _sync_observability_summary(run_state)
    summary = run_state["summary"]
    validation_rows = load_jsonl(run_state["validation_path"], allow_missing=True)
    stage_records = load_jsonl(run_state["stage_records_path"], allow_missing=True)
    review_path = write_shadow_review_template(
        run_state["run_dir"],
        summary["valid_outputs"],
        validation_rows=validation_rows,
        stage_records=stage_records,
    )

    if summary["aborted"]:
        if summary["errors_by_category"].get("auth", 0) > 0 and summary["items_processed"] == 0:
            status = RUN_STATUS_FAILED_AUTH
        elif run_state["manifest"].get("preflight", {}).get("status") == PREFLIGHT_STATUS_FAILED:
            status = RUN_STATUS_FAILED_PREFLIGHT
        elif summary["items_valid"] > 0 or summary["items_processed"] > 0:
            status = RUN_STATUS_ABORTED
        else:
            status = RUN_STATUS_FAILED
    elif summary["items_failed"] > 0:
        status = RUN_STATUS_COMPLETED_WITH_ERRORS
    else:
        status = RUN_STATUS_COMPLETED

    run_state["manifest"]["status"] = status
    run_state["manifest"]["completed_at"] = datetime.now().astimezone().isoformat()
    write_json(run_state["manifest_path"], run_state["manifest"])

    summary_record = build_summary_record(run_state, review_template_path=review_path)
    write_json(run_state["summary_path"], summary_record)
    update_checkpoint(run_state)

    _emit_json(summary_record)
    print(f"[info] Run directory: {run_state['run_dir']}", file=sys.stderr)
    if run_state.get("runtime_controls", {}).get("projection_proof"):
        try:
            from projection_proof_report import write_projection_proof_report

            out_path = Path(run_state["run_dir"]) / "projection_proof_report.json"
            write_projection_proof_report(Path(run_state["run_dir"]), out_path=out_path)
            print(f"[info] Projection proof report: {out_path}", file=sys.stderr)
        except OSError as exc:
            print(f"[warn] projection proof report failed: {exc}", file=sys.stderr)

    if status == RUN_STATUS_COMPLETED:
        return 0
    return 1


def resolve_run_dir(*, run_id: str | None, run_dir: str | None) -> Path:
    if run_dir:
        path = Path(run_dir).resolve()
    elif run_id:
        path = (RUNS_DIR / run_id).resolve()
    else:
        raise OSError("Either run_id or run_dir must be provided.")

    if not path.is_dir():
        raise OSError(f"Run directory not found: {path}")
    return path


def load_batch_items(batch_file: str) -> list[Any]:
    path = Path(batch_file)
    if not path.is_file():
        raise OSError(f"Batch file not found: {path}")

    if path.suffix.lower() == ".jsonl":
        return load_jsonl(path)

    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise OSError(f"Invalid JSON in batch file: {path}") from exc
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("selected_messages", "messages", "responses", "items", "snapshots", "source_messages"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise OSError("Unsupported batch file shape. Expected a JSON list/object or JSONL file.")


def detect_batch_mode(items: list[Any]) -> str:
    if not items:
        return "empty"
    frozen = sum(1 for item in items if is_frozen_snapshot_item(item))
    if frozen == len(items):
        return "frozen"
    if frozen == 0:
        return "live_selection"
    return "mixed"


def is_frozen_snapshot_item(item: Any) -> bool:
    return isinstance(item, dict) and ("source_message" in item or "snapshot_version" in item)


def infer_mailbox(profile: dict[str, Any]) -> str:
    for key in ("email", "mailbox", "address", "user_email"):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def infer_mailbox_from_snapshots(snapshots: list[Any], *, fallback: str) -> str:
    for item in snapshots:
        if isinstance(item, dict):
            mailbox = item.get("mailbox")
            if isinstance(mailbox, str) and mailbox.strip():
                return mailbox.strip()
            source_message = item.get("source_message")
            if isinstance(source_message, dict):
                raw = source_message.get("raw")
                if isinstance(raw, dict):
                    for key in ("mailbox", "account", "email"):
                        value = raw.get(key)
                        if isinstance(value, str) and value.strip():
                            return value.strip()
    return fallback


def categorize_error(*, stage: str, error: str) -> str:
    """Map an error into a coarse operational category for summaries."""
    lowered = error.lower()
    if "413" in lowered or "request too large" in lowered:
        return "payload_too_large"
    if "429" in lowered or "too many requests" in lowered or "rate limit" in lowered:
        return "throttle"
    if "invalid json" in lowered or "json value must be an object" in lowered:
        return "parse"
    if "schema-invalid" in lowered or "required property" in lowered or "is not of type" in lowered:
        return "schema"
    if stage == "validation":
        return "validation"
    if stage == "preview":
        return "preview"
    if "missing required env" in lowered or "missing daszek_" in lowered or "config error" in lowered:
        return "config"
    if is_auth_error_message(error):
        return "auth"
    if any(token in lowered for token in ("timeout", "timed out", "failed to connect", "too many requests", "retry")):
        return "network"
    return "other"


def extract_message_id(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("message_id", "id"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        source_message = item.get("source_message")
        if isinstance(source_message, dict):
            value = source_message.get("message_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def make_run_id(command: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return f"{timestamp}-shadow-{command}-{uuid4().hex[:6]}"


def make_maintenance_run_id() -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return f"maintenance_{timestamp}_{uuid4().hex[:6]}"


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Value must be a positive integer.") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Value must be a positive integer.")
    return parsed


def non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Value must be a non-negative integer.") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("Value must be a non-negative integer.")
    return parsed


def non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Value must be a non-negative number.") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("Value must be a non-negative number.")
    return parsed


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _requires_google_token(command: str, *, batch_items: list[Any] | None = None) -> bool:
    if command in {"message", "period"}:
        return True
    if command == "shadow-run" and batch_items is None:
        return True
    if command == "batch" or (command == "shadow-run" and batch_items is not None):
        return any(not is_frozen_snapshot_item(item) for item in (batch_items or []))
    return False


# Late imports to break circular dependency: new modules import from gmail_intake
from gmail_intake_doctor import run_doctor_command  # noqa: E402, F811
from gmail_intake_process import process_snapshot  # noqa: E402, F811

if __name__ == "__main__":
    raise SystemExit(main())
