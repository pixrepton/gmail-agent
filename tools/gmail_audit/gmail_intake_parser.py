"""Argument parser for gmail_intake CLI commands.
Extracted from gmail_intake.py build_parser() for maintainability.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from artifact_contracts import RUN_ARTIFACT_FILENAMES
from cohort_proof import DEFAULT_GMAIL_COHORT_QUERY
from runtime_imports import DEFAULT_GMAIL_SOURCE

TOOL_DIR = Path(__file__).resolve().parent
RUNS_DIR = TOOL_DIR / "runs"
DEFAULT_SOURCE_MESSAGES_FILE = RUN_ARTIFACT_FILENAMES["source_messages"]

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


def _build_doctor_parser(subparsers: argparse._SubParsersAction) -> None:
    """Add the doctor subcommand for config/connectivity checks."""
    doctor = subparsers.add_parser("doctor", help="Check config, selected Gmail fetch access, and optional Daszek live-push access.")
    doctor.add_argument("--model", help="Override Groq model for this check.")
    doctor.add_argument("--verbose", action="store_true", help="Show basic diagnostics.")
    doctor.add_argument(
        "--gmail-source",
        default=DEFAULT_GMAIL_SOURCE,
        choices=("google_api", "groq_connector"),
        help="Gmail fetch source for the operational mailbox check. Default: google_api.",
    )
    doctor.add_argument("--skip-gmail", action="store_true", help="Skip the operational Gmail mailbox check.")
    doctor.add_argument("--check-daszek", action="store_true", help="Also verify Daszek live-push login and task listing.")
    doctor.add_argument(
        "--check-daszek-v2-read",
        action="store_true",
        help="With --check-daszek, also probe Daszek v2 GET /desk after login (legacy read scope check).",
    )
    doctor.add_argument(
        "--check-daszek-v3-feed",
        action="store_true",
        help="With --check-daszek, probe GET /v3/operational-feed-snapshots/latest (feed-first KPI).",
    )
    doctor.add_argument(
        "--check-drive",
        action="store_true",
        help="Also verify shared Google Drive OAuth readiness and bounded access to GOOGLE_DRIVE_ROOT_FOLDER_ID.",
    )
    doctor.add_argument(
        "--check-calendar",
        action="store_true",
        help="Also verify Google Calendar readiness and OAuth scopes.",
    )

def _build_run_parser(subparsers: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    """Add run subcommands: message, batch, period, shadow-run, rerun."""
    message = subparsers.add_parser(
        "message",
        parents=[common],
        help="Analyze one Gmail message by message id.",
    )
    message.add_argument("--message-id", required=True, help="Exact Gmail message id.")

    batch = subparsers.add_parser(
        "batch",
        parents=[common],
        help="Analyze a batch file containing message ids, selection payloads, or frozen source snapshots.",
    )
    batch.add_argument("--batch-file", required=True, help="Path to JSON or JSONL batch file.")

    period = subparsers.add_parser(
        "period",
        parents=[common],
        help="Analyze a bounded mailbox period selected by Gmail query.",
    )
    period.add_argument("--query", default="to:me -in:spam -in:trash", help="Base Gmail query for period selection.")
    period.add_argument("--days", type=positive_int, default=7, help="Limit the selection to the last N days.")
    period.add_argument("--limit", type=positive_int, default=25, help="Maximum number of messages to analyze.")
    period.add_argument(
        "--page-size",
        type=positive_int,
        default=25,
        help="Gmail search page size used while building the frozen selection.",
    )

    shadow = subparsers.add_parser(
        "shadow-run",
        parents=[common],
        help="Run a fully auditable shadow-mode cohort from live Gmail selection or a batch file.",
    )
    shadow.add_argument("--query", default="to:me -in:spam -in:trash", help="Base Gmail query for live shadow selection.")
    shadow.add_argument("--days", type=positive_int, default=7, help="Limit the live shadow selection to the last N days.")
    shadow.add_argument("--limit", type=positive_int, default=25, help="Maximum number of messages to analyze.")
    shadow.add_argument(
        "--page-size",
        type=positive_int,
        default=25,
        help="Gmail search page size used while building the frozen shadow selection.",
    )
    shadow.add_argument("--batch-file", help="Optional JSON or JSONL batch file.")

    rerun = subparsers.add_parser(
        "rerun",
        parents=[common],
        help="Re-run Groq intake decisions on frozen source snapshots from a previous run.",
    )
    rerun_source = rerun.add_mutually_exclusive_group(required=True)
    rerun_source.add_argument("--run-id", help="Existing run id under tools/gmail_audit/runs/.")
    rerun_source.add_argument("--run-dir", help="Absolute or relative path to an existing run directory.")
    rerun.add_argument(
        "--source-file",
        default=DEFAULT_SOURCE_MESSAGES_FILE,
        help="JSONL file inside the source run directory with frozen source snapshots.",
    )

def _build_proof_parser(subparsers: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    """Add the cohort-proof subcommand."""
    cohort_proof = subparsers.add_parser(
        "cohort-proof",
        parents=[common],
        help="Build a bounded Gmail + Drive cohort proof record for Daszek V3/FastAPI read surfaces.",
    )
    cohort_proof.add_argument("--query", default=DEFAULT_GMAIL_COHORT_QUERY, help="Gmail query when --live-gmail-cohort is used.")
    cohort_proof.add_argument(
        "--gmail-limit",
        type=positive_int,
        default=100,
        help="Maximum Gmail messages selected (live cohort) or mailbox cases summarized (memory-only).",
    )
    cohort_proof.add_argument("--page-size", type=positive_int, default=100, help="Gmail search page size for selection (live cohort only).")
    cohort_proof.add_argument("--drive-limit", type=positive_int, default=500, help="Maximum stored Drive documents inspected.")
    cohort_proof.add_argument(
        "--live-gmail-cohort",
        action="store_true",
        help="Call live Gmail search API for the cohort (read-only). Default is existing-memory-only (no live Gmail).",
    )
    cohort_proof.add_argument(
        "--ingest-selected",
        action="store_true",
        help="MUTATES mailbox memory: ingest selected Gmail messages before building context packs. Requires --live-gmail-cohort.",
    )
    cohort_proof.add_argument(
        "--existing-memory-only",
        action="store_true",
        help="Safety override: use only stored mailbox-memory cases even if --live-gmail-cohort was passed.",
    )
    cohort_proof.add_argument("--output-root", type=Path, default=RUNS_DIR / "cohort-proof", help="Directory for cohort proof JSON records.")
    cohort_proof.add_argument("--run-id", default="", help="Optional stable cohort run id. Defaults to a generated run id.")

def _build_backfill_parser(subparsers: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    """Add backfill subcommands: memory-backfill, gmail-bootstrap-history."""
    memory_backfill = subparsers.add_parser(
        "memory-backfill",
        parents=[common],
        help="Populate Python-owned mailbox memory from a previous run or a live Gmail selection.",
    )
    memory_backfill_source = memory_backfill.add_mutually_exclusive_group()
    memory_backfill_source.add_argument("--message-id", help="Live Gmail message id to ingest into mailbox memory.")
    memory_backfill_source.add_argument(
        "--message-ids-file",
        type=Path,
        help="Text file with one Gmail message id per line (# comments and blank lines ignored).",
    )
    memory_backfill_source.add_argument("--run-id", help="Existing run id whose frozen snapshots should be backfilled.")
    memory_backfill_source.add_argument("--run-dir", help="Absolute or relative path to a run directory whose snapshots should be backfilled.")
    memory_backfill.add_argument("--query", default="to:me -in:spam -in:trash", help="Base Gmail query when backfilling from live Gmail.")
    memory_backfill.add_argument("--days", type=positive_int, default=7, help="Limit the live selection to the last N days.")
    memory_backfill.add_argument("--limit", type=positive_int, default=10, help="Maximum number of live messages to ingest.")
    memory_backfill.add_argument("--page-size", type=positive_int, default=10, help="Gmail search page size used during live backfill.")
    memory_backfill.add_argument(
        "--refresh-document-intelligence",
        action="store_true",
        help="Bounded re-embed of stored mailbox + Drive chunk rows for touched cases (stored chunk_text; no Gmail re-download).",
    )
    memory_backfill.add_argument(
        "--proof-telemetry-dir",
        type=Path,
        default=None,
        help="When set, append command span(s) to <dir>/telemetry_events.jsonl (local mirror; reuse the same dir across a proof chain).",
    )

    gmail_bootstrap = subparsers.add_parser(
        "gmail-bootstrap-history",
        help="Production-safe Gmail historical metadata scan / bounded bootstrap into mailbox memory.",
    )
    gmail_bootstrap.add_argument("--schema-path", help="Optional intake JSON schema path for --selective-llm.")
    gmail_bootstrap.add_argument("--model", help="Override LLM model for --selective-llm.")
    gmail_bootstrap.add_argument("--verbose", action="store_true", help="Show basic diagnostics.")
    gmail_bootstrap.add_argument(
        "--gmail-source",
        default=DEFAULT_GMAIL_SOURCE,
        choices=("google_api",),
        help="Gmail fetch source. Historical bootstrap requires direct google_api metadata semantics.",
    )
    gmail_bootstrap.add_argument("--after", default="", help="Gmail query lower date bound (YYYY-MM-DD or YYYY/MM/DD).")
    gmail_bootstrap.add_argument("--before", default="", help="Gmail query upper date bound (YYYY-MM-DD or YYYY/MM/DD).")
    gmail_bootstrap.add_argument("--days-back", type=non_negative_int, default=0, help="Append newer_than:Nd when --after is not used.")
    gmail_bootstrap.add_argument("--limit", type=positive_int, default=100, help="Maximum metadata records to scan/select.")
    gmail_bootstrap.add_argument("--page-size", type=positive_int, default=100, help="Gmail metadata search page size.")
    gmail_bootstrap.add_argument("--max-threads", type=non_negative_int, default=0, help="Maximum distinct threads in the recommended batch. 0 disables.")
    gmail_bootstrap.add_argument(
        "--max-messages-per-thread",
        type=non_negative_int,
        default=0,
        help="Maximum messages per thread in the recommended batch. 0 disables.",
    )
    gmail_bootstrap.add_argument("--include-label", action="append", default=[], help="Require Gmail label; repeatable.")
    gmail_bootstrap.add_argument("--exclude-label", action="append", default=[], help="Exclude Gmail label; repeatable.")
    gmail_bootstrap.add_argument("--query", default="to:me -in:spam -in:trash", help="Base Gmail query for the historical window.")
    gmail_bootstrap.add_argument("--metadata-only", action="store_true", help="Scan metadata only; no body fetch and no mailbox-memory writes.")
    gmail_bootstrap.add_argument("--fetch-body", action="store_true", help="Fetch full body for selected candidates and allow bounded memory persistence.")
    gmail_bootstrap.add_argument("--fetch-attachments-metadata", action="store_true", help="Keep Gmail attachment metadata in selected snapshots.")
    gmail_bootstrap.add_argument(
        "--fetch-attachments-content",
        action="store_true",
        help="Fetch attachment bytes for selected candidates. Requires --max-attachment-bytes > 0.",
    )
    gmail_bootstrap.add_argument("--max-attachment-bytes", type=non_negative_int, default=0, help="Maximum attachment bytes fetched per attachment.")
    gmail_bootstrap.add_argument("--dry-run", action="store_true", help="Do not mutate mailbox memory or source cursors.")
    gmail_bootstrap.add_argument("--no-llm", action="store_true", default=None, help="Disable LLM enrichment. Default unless --selective-llm is used.")
    gmail_bootstrap.add_argument("--selective-llm", action="store_true", help="Allow capped LLM enrichment for selected operational candidates.")
    gmail_bootstrap.add_argument("--max-llm-calls", type=non_negative_int, default=0, help="Maximum LLM enrichment calls for this run.")
    gmail_bootstrap.add_argument("--max-llm-calls-per-thread", type=positive_int, default=1, help="Maximum LLM calls per Gmail thread.")
    gmail_bootstrap.add_argument("--max-consecutive-failures", type=non_negative_int, default=0, help="Stop after this many consecutive item failures. 0 disables.")
    gmail_bootstrap.add_argument("--timebox-seconds", type=non_negative_int, default=0, help="Stop cleanly after this many seconds. 0 disables.")
    gmail_bootstrap.add_argument(
        "--no-daszek-push",
        action="store_true",
        default=True,
        help="Explicit safety marker; historical bootstrap v1 never pushes live Daszek projections.",
    )
    gmail_bootstrap.add_argument("--proof-dir", type=Path, default=None, help="Directory for the redacted bootstrap proof pack.")
    gmail_bootstrap.add_argument(
        "--write-source-cursor",
        choices=("true", "false"),
        default="false",
        help="Reserved safety flag for bounded runs. Use --finalize-source-cursor for actual cursor writes.",
    )
    gmail_bootstrap.add_argument("--finalize-source-cursor", action="store_true", help="Finalize Gmail History API cursor as a separate guarded step.")
    gmail_bootstrap.add_argument("--bootstrap-run-id", default="", help="Bootstrap run id being finalized.")
    gmail_bootstrap.add_argument("--runtime-profile", default="", help="Expected active GMAIL_AGENT_RUNTIME_PROFILE; mismatch fails closed.")
    gmail_bootstrap.add_argument("--cursor-scope", default="default", help="Source cursor scope written by finalize. Default matches gmail-detect-changes.")
    gmail_bootstrap.add_argument("--run-id", default="", help="Optional explicit bootstrap run id.")
    gmail_bootstrap.add_argument(
        "--confirm-vps-node-b",
        action="store_true",
        help="Required explicit confirmation for any mutating Gmail historical bootstrap path on VPS/Node B.",
    )

def _build_helper_parsers(subparsers: argparse._SubParsersAction) -> None:
    """Add all remaining subcommands."""
    real_mail_discovery = subparsers.add_parser(
        "real-mail-discovery",
        help="No-side-effect discovery over operator-curated historical real-mail cases.",
    )
    real_mail_discovery.add_argument(
        "--input",
        type=Path,
        required=True,
        help="JSON/JSONL file with curated historical case records and operator expected outcomes.",
    )
    real_mail_discovery.add_argument(
        "--output-dir",
        type=Path,
        default=RUNS_DIR / "real-mail-intelligence-discovery",
        help="Directory for discovery proof artifacts.",
    )
    real_mail_discovery.add_argument("--run-id", default="", help="Optional stable discovery run id.")
    real_mail_discovery.add_argument("--min-cases", type=positive_int, default=10, help="Minimum discovery cohort size.")
    real_mail_discovery.add_argument("--max-cases", type=positive_int, default=15, help="Maximum discovery cohort size.")
    real_mail_discovery.add_argument(
        "--allow-small-sample",
        action="store_true",
        help="Test/dev override: allow fewer than --min-cases. Do not use as program closeout proof.",
    )
    evaluate = subparsers.add_parser("eval", help="Evaluate human annotations for an existing run.")
    eval_source = evaluate.add_mutually_exclusive_group(required=True)
    eval_source.add_argument("--run-id", help="Existing run id under tools/gmail_audit/runs/.")
    eval_source.add_argument("--run-dir", help="Absolute or relative path to an existing run directory.")
    evaluate.add_argument(
        "--annotations",
        help="Optional path to CSV annotations. Defaults to <run_dir>/human_annotations.csv.",
    )
    replay_v2 = subparsers.add_parser(
        "replay-v2",
        help="Replay saved v2 projection artifacts into the Daszek v2 ingest endpoint without rerunning Gmail/Groq.",
    )
    replay_v2_source = replay_v2.add_mutually_exclusive_group(required=True)
    replay_v2_source.add_argument("--run-id", help="Existing run id under tools/gmail_audit/runs/.")
    replay_v2_source.add_argument("--run-dir", help="Absolute or relative path to an existing run directory.")
    replay_v2.add_argument(
        "--source-file",
        default=RUN_ARTIFACT_FILENAMES["stage_records"],
        help="JSONL file inside the source run directory containing stage records with v2 projections.",
    )
    replay_v2.add_argument("--message-id", help="Replay only one saved message id.")
    replay_v2.add_argument("--limit", type=positive_int, help="Optional maximum number of payloads to replay.")
    replay_v2.add_argument("--verbose", action="store_true", help="Show per-item replay diagnostics.")
    push_memory_v2 = subparsers.add_parser(
        "push-memory-v2",
        help="Pick one mailbox-memory message and push a deterministic v2 projection to Daszek (no LLM).",
    )
    push_memory_v2.add_argument(
        "--order",
        choices=("oldest", "newest"),
        default="oldest",
        help="Which mailbox-memory message to push (default: oldest).",
    )
    maintain = subparsers.add_parser(
        "maintain-desk",
        help="Preview or apply deterministic desk-maintenance actions against the existing Daszek v2 state.",
    )
    maintain_mode = maintain.add_mutually_exclusive_group(required=True)
    maintain_mode.add_argument("--preview", action="store_true", help="Generate maintenance proposals without mutating Daszek state.")
    maintain_mode.add_argument("--apply", action="store_true", help="Apply maintenance proposals through the canonical Daszek v2 ingest path.")
    maintain.add_argument("--limit", type=positive_int, help="Optional maximum number of active desk/day notes to inspect.")
    maintain.add_argument("--case-id", help="Restrict maintenance to one case id.")
    maintain.add_argument("--note-id", help="Restrict maintenance to one desk note id.")
    maintain.add_argument("--verbose", action="store_true", help="Show maintenance diagnostics.")
    case_context = subparsers.add_parser(
        "case-context",
        help="Read-only mailbox-memory context pack for one case_id or message_id.",
    )
    case_context_source = case_context.add_mutually_exclusive_group(required=True)
    case_context_source.add_argument("--case-id", help="Mailbox-memory case id.")
    case_context_source.add_argument("--message-id", help="Gmail message id already present in mailbox memory.")
    case_context.add_argument("--query-text", default="", help="Optional retrieval hint for chunk ranking.")
    case_context.add_argument(
        "--neo4j-project",
        action="store_true",
        help="Projection-only bounded Neo4j pilot refresh for this case. Postgres remains source of truth.",
    )
    case_context.add_argument(
        "--neo4j-graph-aware",
        action="store_true",
        help="Bounded graph-aware retrieval from the optional Neo4j pilot graph.",
    )
    case_context.add_argument(
        "--neo4j-max-hops",
        type=positive_int,
        default=2,
        help="Maximum hop count for bounded Neo4j pilot neighborhood queries.",
    )
    case_context.add_argument(
        "--neo4j-limit",
        type=positive_int,
        default=10,
        help="Maximum number of bounded Neo4j pilot neighborhood paths to return.",
    )
    case_context.add_argument(
        "--neo4j-anchor-mode",
        choices=("auto", "document", "contact", "location"),
        default="auto",
        help="Bounded Neo4j pilot anchor mode for graph-aware neighborhood expansion.",
    )
    case_context.add_argument("--verbose", action="store_true", help="Show basic diagnostics.")
    case_context.add_argument("--vnext", action="store_true", help="Emit CaseContextPack vNext contract instead of the legacy context-pack dict.")
    case_context.add_argument(
        "--human-summary",
        action="store_true",
        help="With --vnext, print a short text summary instead of JSON (no raw Gmail body).",
    )
    case_context.add_argument("--evidence-limit", type=int, default=0, help="With --vnext, max evidence cards (0=default 32).")
    case_context.add_argument("--chunk-limit", type=int, default=0, help="With --vnext, max chunks feeding evidence cards (0=default 8).")
    case_context.add_argument("--conflict-limit", type=int, default=0, help="With --vnext, max conflicting_facts rows (0=default 48).")
    case_context.add_argument("--gap-limit", type=int, default=0, help="With --vnext, max completeness_gaps rows (0=default 48).")
    case_context.add_argument(
        "--proof-telemetry-dir",
        type=Path,
        default=None,
        help="When set, append command span to <dir>/telemetry_events.jsonl (local mirror).",
    )
    ap_list = subparsers.add_parser("action-proposal-list", help="List supervised action proposals from mailbox memory.")
    ap_list.add_argument("--case-id", default="", help="Optional case id filter.")
    ap_list.add_argument("--status", default="", help="Optional proposal status filter.")
    ap_list.add_argument("--limit", type=positive_int, default=50, help="Maximum rows.")
    ap_approve = subparsers.add_parser("action-proposal-approve", help="Approve an ActionProposal as an owner.")
    ap_approve.add_argument("--proposal-id", required=True)
    ap_approve.add_argument("--approved-by", required=True)
    ap_approve.add_argument("--reason", default="")
    ap_reject = subparsers.add_parser("action-proposal-reject", help="Reject an ActionProposal as an owner.")
    ap_reject.add_argument("--proposal-id", required=True)
    ap_reject.add_argument("--rejected-by", required=True)
    ap_reject.add_argument("--reason", default="")
    ap_execute = subparsers.add_parser("action-proposal-execute", help="Execute an approved ActionProposal through the policy gate.")
    ap_execute.add_argument("--proposal-id", required=True)
    ap_execute.add_argument("--executed-by", required=True)
    ap_execute.add_argument("--dry-run", action="store_true", help="Do not perform external writes.")
    calendar_ingest = subparsers.add_parser("calendar-ingest", help="Env-gated Google Calendar read + RawObservation/CanonicalSignal ingest.")
    calendar_ingest.add_argument("--time-min", default="")
    calendar_ingest.add_argument("--time-max", default="")
    calendar_ingest.add_argument("--limit", type=positive_int, default=50)
    calendar_ingest.add_argument("--dry-run", action="store_true")
    calendar_context = subparsers.add_parser("calendar-context", help="Read Calendar block for one mailbox-memory case.")
    calendar_context.add_argument("--case-id", required=True)
    docintel = subparsers.add_parser("document-intelligence", help="Run parser-text Document Intelligence V1 for a provided file/text fixture.")
    docintel.add_argument("--source-type", choices=("gmail_attachment", "drive_file"), default="gmail_attachment")
    docintel.add_argument("--source-id", required=True)
    docintel.add_argument("--case-id", default="")
    docintel.add_argument("--filename", required=True)
    docintel.add_argument("--mime-type", default="")
    docintel.add_argument("--text-file", type=Path, help="Optional UTF-8 text fixture extracted from a document.")
    docintel.add_argument("--persist", action="store_true", help="Persist into mailbox memory.")
    eval_summary = subparsers.add_parser("eval-summary", help="Aggregate AI quality metrics from feedback and outcomes.")
    eval_summary.add_argument("--window", choices=("last_7_days", "last_30_days", "all_time"), default="all_time")
    drive_ingest = subparsers.add_parser(
        "drive-ingest",
        help="Read-only bounded Google Drive ingest into shared mailbox memory substrate.",
    )
    drive_ingest.add_argument("--limit", type=positive_int, default=25, help="Maximum number of Drive items to process.")
    drive_ingest.add_argument("--root-folder-id", default="", help="Override GOOGLE_DRIVE_ROOT_FOLDER_ID for this run.")
    drive_ingest.add_argument("--page-token", default="", help="Optional Drive pagination token.")
    drive_ingest.add_argument("--run-id", default="", help="Optional bounded run id.")
    drive_ingest.add_argument(
        "--refresh-document-intelligence",
        action="store_true",
        help="After ingest, bounded re-embed of stored Drive chunk rows for affected cases (stored chunk_text; no re-download).",
    )
    drive_ingest.add_argument("--verbose", action="store_true", help="Show basic diagnostics.")
    drive_ingest.add_argument(
        "--proof-telemetry-dir",
        type=Path,
        default=None,
        help="When set, append command span to <dir>/telemetry_events.jsonl (local mirror).",
    )
    drive_case_context = subparsers.add_parser(
        "drive-case-context",
        help="Read-only case context pack enriched with Drive documents/facts/graph hints.",
    )
    drive_case_context.add_argument("--case-id", required=True, help="Mailbox-memory case id.")
    drive_case_context.add_argument("--query-text", default="", help="Optional retrieval hint for reference document ranking.")
    drive_case_context.add_argument(
        "--refresh-projection",
        action="store_true",
        help="Rebuild the stored snapshot before reading the context pack.",
    )
    drive_case_context.add_argument("--verbose", action="store_true", help="Show basic diagnostics.")
    drive_case_context.add_argument("--vnext", action="store_true", help="Emit CaseContextPack vNext contract instead of the legacy context-pack dict.")
    drive_case_context.add_argument(
        "--human-summary",
        action="store_true",
        help="With --vnext, print a short text summary instead of JSON (no raw Gmail body).",
    )
    drive_case_context.add_argument("--evidence-limit", type=int, default=0, help="With --vnext, max evidence cards (0=default 32).")
    drive_case_context.add_argument("--chunk-limit", type=int, default=0, help="With --vnext, max chunks (0=default 8).")
    drive_case_context.add_argument("--conflict-limit", type=int, default=0, help="With --vnext, max conflicting_facts (0=default 48).")
    drive_case_context.add_argument("--gap-limit", type=int, default=0, help="With --vnext, max completeness_gaps (0=default 48).")
    drive_case_context.add_argument(
        "--proof-telemetry-dir",
        type=Path,
        default=None,
        help="When set, append command span to <dir>/telemetry_events.jsonl (local mirror).",
    )
    drive_graph_rebuild = subparsers.add_parser(
        "drive-graph-rebuild",
        help="Rebuild operational graph nodes/edges from already ingested Drive documents.",
    )
    drive_graph_rebuild.add_argument("--limit", type=positive_int, default=200, help="Maximum number of Drive documents to rebuild.")
    drive_graph_rebuild.add_argument("--case-id", default="", help="Optional case id for a bounded rebuild.")
    drive_graph_rebuild.add_argument("--verbose", action="store_true", help="Show basic diagnostics.")
    signal_run = subparsers.add_parser(
        "signal-run",
        help="Run one bounded poll iteration for the unified signal runtime.",
    )
    signal_run.add_argument("--oneshot", action="store_true", help="Explicit no-op marker for operator clarity; signal-run is always one-shot.")
    signal_run.add_argument("--dry-run", action="store_true", help="Force shadow-mode processing for this invocation.")
    signal_run.add_argument("--push-daszek", action="store_true", help="Allow Daszek push policy evaluation for this run.")
    signal_run.add_argument(
        "--message-id",
        default="",
        help="Process one explicit Gmail message id (sequential ingress / Gate B row3).",
    )
    signal_run.add_argument(
        "--projection-proof",
        action="store_true",
        help="Write projection_proof_report.json for this run (Gate B classifier).",
    )
    signal_run.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue after non-fatal per-message failures (sequential ingress default).",
    )
    signal_run.add_argument(
        "--max-messages",
        type=non_negative_int,
        default=10,
        help="Maximum Gmail messages to fully process in this oneshot run (0 disables the limit). Default: 10.",
    )
    signal_run.add_argument(
        "--timebox-seconds",
        type=non_negative_int,
        default=300,
        help="Hard time limit for this oneshot run (0 disables). Default: 300.",
    )
    signal_run.add_argument("--verbose", action="store_true", help="Show basic diagnostics.")
    signal_worker = subparsers.add_parser(
        "signal-worker",
        help="Run the unified signal worker in continuous poll mode.",
    )
    signal_worker.add_argument("--loop", action="store_true", help="Explicit no-op marker for operator clarity; signal-worker always loops.")
    signal_worker.add_argument("--dry-run", action="store_true", help="Force shadow-mode processing for this worker session.")
    signal_worker.add_argument("--max-iterations", type=non_negative_int, default=0, help="Optional bounded number of poll iterations. 0 means run until interrupted.")
    signal_worker.add_argument("--push-daszek", action="store_true", help="Allow Daszek push policy evaluation for this worker session.")
    signal_worker.add_argument("--verbose", action="store_true", help="Show basic diagnostics.")
    event_spine_processor = subparsers.add_parser(
        "event-spine-processor",
        help="Poll and process unified_os_events (shadow/active; separate from signal-worker).",
    )
    event_spine_processor.add_argument(
        "--loop",
        action="store_true",
        help="Run continuous poll loop until interrupted.",
    )
    event_spine_processor.add_argument(
        "--max-iterations",
        type=non_negative_int,
        default=1,
        help="Max poll iterations (default 1). With --loop, 0 means run until interrupted.",
    )
    event_spine_processor.add_argument("--verbose", action="store_true", help="Show basic diagnostics.")
    signal_replay = subparsers.add_parser(
        "signal-replay",
        help="Replay one canonical signal from the durable signal journal.",
    )
    signal_replay.add_argument("--signal-id", required=True, help="Canonical signal_id to replay.")
    signal_rebuild = subparsers.add_parser(
        "signal-rebuild-case",
        help="Rebuild one case deterministically from the canonical signal journal.",
    )
    signal_rebuild.add_argument("--case-id", required=True, help="Mailbox-memory case id.")
    signal_rebuild.add_argument("--case-key-hint", default="", help="Optional case key hint when the journal lineage is keyed by case_key.")
    subparsers.add_parser(
        "agent-mcp-serve",
        help="Start stdio MCP server for agent runtime (get_snapshot, trigger_run, approve_hitl, list, turns).",
    )
    op_fb = subparsers.add_parser(
        "operator-feedback",
        help=(
            "Bridge operator payload into mailbox_memory_events: calibration=persist only; "
            "adjudication=persist v2_1_adjudication + truth-loop reconcile (requires Postgres mailbox memory)."
        ),
    )
    op_fb.add_argument(
        "--json-file",
        help="JSON file with operator payload (event_domain, adjudication_kind, target_refs, …). Else read stdin.",
    )
    op_fb.add_argument(
        "--run-id",
        default="operator-feedback-cli",
        help="Trace/run id stored on SignalRuntimeContext.run_state.",
    )
    bq_drain = subparsers.add_parser(
        "daszek-bridge-drain",
        help="Drain Daszek v2 bridge_queue.jsonl pending adjudication items via bridge_operator_feedback.",
    )
    bq_drain.add_argument(
        "--queue-path",
        help="Optional path to wp-content/uploads/daszek/v2/bridge_queue.jsonl on the operator host. If omitted, use Daszek v2 REST bridge API.",
    )
    bq_drain.add_argument("--remote", action="store_true", help="Use Daszek v2 REST bridge queue API instead of a local file.")
    bq_drain.add_argument("--max-items", type=positive_int, default=25, help="Maximum pending rows to process.")
    bq_drain.add_argument("--dry-run", action="store_true", help="List pending payloads without executing the bridge.")
    bq_drain.add_argument(
        "--run-id",
        default="daszek-bridge-drain",
        help="Trace/run id stored on SignalRuntimeContext.run_state.",
    )
    bq_drain.add_argument(
        "--domain",
        choices=("any", "adjudication", "action_decision"),
        default="any",
        help="Only drain pending bridge_queue rows with this domain (Gate B proof uses adjudication).",
    )
    gmail_detect = subparsers.add_parser(
        "gmail-detect-changes",
        help="Run one bounded Gmail History API poll and persist the durable cursor.",
    )
    gmail_detect.add_argument("--cursor-scope", default="default", help="Source cursor scope key.")
    gmail_detect.add_argument("--max-results", type=positive_int, default=100, help="Maximum Gmail history records to inspect.")
    gmail_detect.add_argument("--no-bootstrap", action="store_true", help="Do not seed the cursor from the current Gmail profile historyId.")
    gmail_detect.add_argument("--verbose", action="store_true", help="Show basic diagnostics.")
    drive_detect = subparsers.add_parser(
        "drive-detect-changes",
        help="Run one bounded Drive Changes API poll and persist the durable cursor.",
    )
    drive_detect.add_argument("--cursor-scope", default="default", help="Source cursor scope key.")
    drive_detect.add_argument("--max-results", type=positive_int, default=100, help="Maximum Drive change rows to inspect.")
    drive_detect.add_argument("--no-bootstrap", action="store_true", help="Do not seed the cursor from the current Drive start page token.")
    drive_detect.add_argument("--verbose", action="store_true", help="Show basic diagnostics.")
    # Business Dictionary CLI
    biz_dict = subparsers.add_parser(
        "bizdict-extract",
        help="Extract business terms from text/file/stdin into business_dictionary_terms (PG + optional Neo4j).",
    )
    biz_dict.add_argument("--text", default="", help="Direct text to extract terms from.")
    biz_dict.add_argument("--file", default="", help="Path to text file for term extraction.")
    biz_dict.add_argument("--source", default="", help="Source document identifier.")
    biz_dict.add_argument("--source-kind", default="cli", help="Source kind (cli, drive, gmail, manual).")
    biz_dict.add_argument("--dry-run", action="store_true", help="Extract only — do not persist.")
    biz_dict.add_argument("--neo4j", action="store_true", help="Also store in Neo4j graph.")
    biz_dict_search = subparsers.add_parser(
        "bizdict-search",
        help="Search business dictionary terms in PostgreSQL.",
    )
    biz_dict_search.add_argument("--query", default="", help="Search text.")
    biz_dict_search.add_argument("--category", default="", help="Filter by category (product|service|pricing|term|rule|template|contact).")
    biz_dict_search.add_argument("--limit", type=positive_int, default=50, help="Max results.")
    biz_dict_search.add_argument("--stats", action="store_true", help="Show dictionary statistics instead of search results.")
    biz_dict_search.add_argument("--delete", default="", help="Delete term by term_id.")
    biz_dict_sync = subparsers.add_parser(
        "bizdict-sync",
        help="Sync existing Drive documents into the business dictionary.",
    )
    biz_dict_sync.add_argument("--limit", type=positive_int, default=100, help="Max Drive documents to process.")
    biz_dict_sync.add_argument("--dry-run", action="store_true", help="Extract only — do not persist.")
    biz_dict_sync.add_argument("--neo4j", action="store_true", help="Also store in Neo4j graph.")
    biz_dict_outbox = subparsers.add_parser(
        "bizdict-outbox-process",
        help="Process pending sync_outbox entries — replicate PG terms to Neo4j.",
    )
    biz_dict_outbox.add_argument("--limit", type=positive_int, default=50, help="Max outbox entries to process.")
    biz_dict_outbox.add_argument("--dry-run", action="store_true", help="Show pending entries without processing.")
    # SLA Watcher CLI
    sla_watcher_parser = subparsers.add_parser(
        "sla-watcher",
        help="Check SLA violations for pending decisions and escalate if needed.",
    )
    sla_watcher_parser.add_argument("--oneshot", action="store_true", help="Run once and report.")
    sla_watcher_parser.add_argument("--loop", action="store_true", help="Run continuous loop (15 min interval).")
    sla_watcher_parser.add_argument("--verbose", action="store_true", help="Show details.")
    # Follow-up Guardian CLI (FG-04 live tick entrypoint)
    follow_up_parser = subparsers.add_parser(
        "follow-up-guardian",
        help="Propose follow-ups for SLA-stagnating cases (roadmap 3.1 / FG-04 oneshot).",
    )
    follow_up_parser.add_argument("--oneshot", action="store_true", help="Run once and report.")
    follow_up_parser.add_argument("--limit", type=int, default=200, help="Max recent snapshots to scan.")
    # Event Spine cleanup CLI
    os_cleanup = subparsers.add_parser(
        "os-events-cleanup",
        help="Delete old os_events older than TTL days.",
    )
    os_cleanup.add_argument("--days", type=int, default=30, help="TTL in days (default: 30).")
    os_cleanup.add_argument("--dry-run", action="store_true", help="Show how many would be deleted without deleting.")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Local shadow-mode Gmail Intake Intelligence runner for TOP-INSTAL. "
            "Safe default: preview-only artifacts under tools/gmail_audit/runs/. "
            "Live Daszek mutation happens only with explicit --push-daszek."
        )
    )

    common = argparse.ArgumentParser(add_help=False)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--schema-path",
        help="Optional path to intake JSON schema. Defaults to tools/gmail_audit/schemas/intake_output_v1.json.",
    )
    common.add_argument("--model", help="Override Groq model for this run.")
    common.add_argument("--verbose", action="store_true", help="Show basic diagnostics.")
    common.add_argument(
        "--gmail-source",
        default=DEFAULT_GMAIL_SOURCE,
        choices=("google_api", "groq_connector"),
        help="Gmail fetch source. Default: google_api. groq_connector stays optional/experimental.",
    )
    common.add_argument(
        "--context-limit",
        type=positive_int,
        default=3,
        help="Maximum number of additional context messages to fetch per item.",
    )
    common.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue processing other items after per-item failures.",
    )
    common.add_argument(
        "--timebox-seconds",
        type=non_negative_int,
        default=0,
        help="Stop the run cleanly after the given wall-clock seconds. 0 disables the timebox.",
    )
    common.add_argument(
        "--max-failures",
        type=non_negative_int,
        default=0,
        help="Stop the run cleanly after this many failed items. 0 disables the limit.",
    )
    common.add_argument(
        "--max-consecutive-failures",
        type=non_negative_int,
        default=0,
        help="Stop the run cleanly after this many consecutive failures. 0 disables the limit.",
    )
    common.add_argument(
        "--push-daszek",
        action="store_true",
        help="Opt in to live Daszek REST writes. Disabled by default; preview artifacts are always generated.",
    )
    common.add_argument(
        "--attachments-metadata-only",
        action="store_true",
        help=(
            "Skip downloading and text-extracting attachment bodies for this run; keep only attachment metadata "
            "(filename, mime, size, ids) in the LLM intake path. Does not change Gmail/Drive. "
            "When set, overrides ATTACHMENT_EXTRACTION_MAX_BYTES / enabled fetch for attachment bytes for this run only."
        ),
    )
    common.add_argument(
        "--llm-inter-item-delay-seconds",
        type=non_negative_float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "Optional wall-clock sleep between successive inbox items in live selection and frozen rerun loops "
            "(after the first item). Reduces burst pressure on the LLM provider (e.g. Groq 429s). Default: 0 (no delay)."
        ),
    )
    common.add_argument(
        "--projection-proof",
        action="store_true",
        help="After the run, write projection_proof_report.json summarizing Daszek v1/v2 policy outcomes for this run directory.",
    )
    subparsers = parser.add_subparsers(dest="command")

    _build_doctor_parser(subparsers)
    _build_run_parser(subparsers, common)
    _build_proof_parser(subparsers, common)
    _build_backfill_parser(subparsers, common)
    _build_helper_parsers(subparsers)

    parser.epilog = (
        "Default operating mode stays local-only: frozen snapshots, validation artifacts, review CSV, "
        "and Daszek preview payloads. Use doctor before any live or Gmail-backed run."
    )

    return parser
