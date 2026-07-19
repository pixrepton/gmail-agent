#!/usr/bin/env python3
"""
Sequential single-message Gmail ingress through full intake + Daszek push.

Run from repo root (e.g. inside gmail-agent-worker container: WORKDIR /app).
Reduces Groq burst vs batch period/shadow runs by sleeping between messages.

Example (on VPS host):
  docker compose --env-file .env.vps -f docker-compose.vps.yml --profile worker run --rm \\
    gmail-agent-worker python scripts/sequential_gmail_ingress_daszek.py --limit 28 --delay 12

With projection proof (per-child ``projection_proof_report.json`` + batch rollup):

  python scripts/sequential_gmail_ingress_daszek.py \\
    --newer-than 14d --limit 8 --delay 30 --push-daszek --projection-proof --keep-going --verbose

Retry / resume (Groq 429 friendly):

  python scripts/sequential_gmail_ingress_daszek.py ... \\
    --max-retries-per-message 2 --retry-base-delay 30 --retry-max-delay 300

Resume an interrupted batch (same ``--batch-dir``):

  python scripts/sequential_gmail_ingress_daszek.py ... --batch-dir tools/gmail_audit/runs/sequential-XXX --resume

Re-process every message id (ignore checkpoint skips):

  python scripts/sequential_gmail_ingress_daszek.py ... --batch-dir ... --force

429 retries use ``--max-retries-per-message`` / backoff flags. Legacy ``--retry-on-429`` prints a compatibility warning only (no-op).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _extract_mid(item: object) -> str:
    if isinstance(item, dict):
        for key in ("message_id", "id"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _exclude_message_ids_set(cli_values: list[str] | None) -> set[str]:
    """CLI repeats + SEQUENTIAL_EXCLUDE_MESSAGE_IDS (comma/semicolon separated)."""
    out: set[str] = set()
    for raw in cli_values or []:
        s = str(raw or "").strip()
        if s:
            out.add(s)
    env_raw = os.getenv("SEQUENTIAL_EXCLUDE_MESSAGE_IDS", "").strip()
    if env_raw:
        for part in env_raw.replace(";", ",").split(","):
            s = part.strip()
            if s:
                out.add(s)
    return out


def _message_ids_list(cli_values: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in cli_values or []:
        mid = str(raw or "").strip()
        if mid and mid not in out:
            out.append(mid)
    return out


def _selection_fetch_limit(*, requested_limit: int, exclude_ids: set[str]) -> int:
    requested = max(1, int(requested_limit))
    if not exclude_ids:
        return min(requested, 500)
    # Fetch a bounded surplus before exclude filtering so --limit 1 + one excluded
    # first candidate can still select a replacement message.
    return min(500, max(requested * 2, requested + len(exclude_ids) + 10))


def _atomic_write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _running_inside_app_container() -> bool:
    repo_root = _repo_root()
    return repo_root.as_posix().startswith("/app/") or repo_root.as_posix() == "/app"


def _resolve_batch_dir(raw_value: str) -> Path:
    raw = str(raw_value or "").strip()
    if not raw:
        raise ValueError("batch dir must not be empty")
    normalized = raw.replace("\\", "/")
    if _running_inside_app_container() and ":" in normalized and not normalized.startswith("/app/"):
        return Path("/app/gate-b-proof") / Path(normalized).name
    return Path(raw).expanduser().resolve()


def _append_index_row(batch_dir: Path, row: dict) -> None:
    batch_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False) + "\n"
    path = batch_dir / "child_runs_index.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _run_dir_from_summary(tool_runs: Path, summary: dict) -> Path | None:
    rid = str(summary.get("run_id") or "").strip()
    if not rid:
        return None
    p = tool_runs / rid
    return p if p.is_dir() else None


def main() -> int:
    root = _repo_root()
    tool_dir = root / "tools" / "gmail_audit"
    sys.path.insert(0, str(tool_dir))

    from config import load_settings  # noqa: PLC0415
    from gmail_fetch import build_period_query, search_emails  # noqa: PLC0415
    from sequential_ingress_helpers import (  # noqa: PLC0415
        aggregate_projection_batch_summary,
        build_gmail_intake_message_command,
        build_sequential_operator_summary,
        compute_retry_delay,
        extract_run_dir_from_text,
        is_rate_limit_signal,
        load_completed_message_ids,
        load_failed_items_records,
        make_child_runs_index_row,
        make_failed_item_record,
        parse_first_json_object,
        parse_newer_than_days,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=28, help="Max messages (default 28).")
    parser.add_argument("--days", type=int, default=None, help="newer_than:Nd segment for query (default 14 if no --newer-than).")
    parser.add_argument(
        "--newer-than",
        default=None,
        metavar="Nd",
        help="Gmail newer_than segment, e.g. 14d or newer_than:14d (sets days for query).",
    )
    parser.add_argument("--query", default="to:me -in:spam -in:trash", help="Base Gmail query.")
    parser.add_argument(
        "--delay",
        type=float,
        default=12.0,
        metavar="SEC",
        help="Sleep between finished message runs (Groq rate limit). Default 12.",
    )
    parser.add_argument("--gmail-source", default="google_api", choices=("google_api", "groq_connector"))
    parser.add_argument("--dry-run", action="store_true", help="Only list message ids, no intake.")
    parser.add_argument("--verbose-selection", action="store_true", help="Print selection meta to stderr.")
    parser.add_argument("--push-daszek", action="store_true", help="Pass --push-daszek to each gmail_intake message run.")
    parser.add_argument(
        "--projection-proof",
        action="store_true",
        help="Pass --projection-proof to each child run and write batch projection_proof_report under batch dir.",
    )
    parser.add_argument("--keep-going", action="store_true", help="Pass --keep-going to gmail_intake (default on).")
    parser.add_argument("--verbose", action="store_true", help="Pass --verbose to gmail_intake.")
    parser.add_argument(
        "--no-keep-going",
        action="store_true",
        help="Do not pass --keep-going to child runs (overrides default).",
    )
    parser.add_argument(
        "--max-retries-per-message",
        type=int,
        default=2,
        metavar="N",
        help="Extra retries after first failure when rate-limited (default 2 => up to 3 attempts per message).",
    )
    parser.add_argument(
        "--retry-base-delay",
        type=float,
        default=30.0,
        metavar="SEC",
        help="Base seconds for exponential backoff before retry after throttle (default 30).",
    )
    parser.add_argument(
        "--retry-max-delay",
        type=float,
        default=300.0,
        metavar="SEC",
        help="Cap seconds for backoff sleep (default 300).",
    )
    parser.add_argument(
        "--batch-dir",
        default=None,
        metavar="PATH",
        help="Directory for batch artifacts (default: runs/sequential-<UTC stamp>).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue from existing batch dir: skip completed message ids from child_runs_index.jsonl.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore checkpoint skips; process all selected message ids again.",
    )
    parser.add_argument(
        "--exclude-message-id",
        action="append",
        default=[],
        metavar="MESSAGE_ID",
        help="Skip this Gmail message id (repeatable). Merged with SEQUENTIAL_EXCLUDE_MESSAGE_IDS env (comma/semicolon CSV).",
    )
    parser.add_argument(
        "--message-id",
        action="append",
        default=[],
        metavar="MESSAGE_ID",
        help="Explicit Gmail message id for this cohort; repeatable. When set, Gmail search selection is skipped.",
    )
    parser.add_argument(
        "--retry-on-429",
        action="store_true",
        help=(
            "Legacy compatibility no-op: 429 retry/backoff is controlled only by --max-retries-per-message, "
            "--retry-base-delay, and --retry-max-delay. Emits WARN on stderr when set."
        ),
    )
    args = parser.parse_args()

    if args.retry_on_429:
        print(
            "WARN: --retry-on-429 is a compatibility no-op; 429 retry is controlled by "
            "--max-retries-per-message and retry backoff flags.",
            file=sys.stderr,
            flush=True,
        )

    days = args.days
    if args.newer_than:
        parsed = parse_newer_than_days(args.newer_than)
        if parsed is None:
            print(f"Invalid --newer-than value: {args.newer_than!r}", file=sys.stderr)
            return 2
        days = parsed
    if days is None:
        days = 14

    keep_going = not bool(args.no_keep_going)
    max_attempts = max(1, int(args.max_retries_per_message) + 1)
    runs_root = tool_dir / "runs"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if args.batch_dir:
        batch_dir = _resolve_batch_dir(str(args.batch_dir))
    else:
        batch_dir = runs_root / f"sequential-{stamp}"

    batch_existed = batch_dir.exists()
    resume_effective = bool(args.resume)

    exclude_ids = _exclude_message_ids_set(args.exclude_message_id)
    explicit_message_ids = _message_ids_list(args.message_id)
    fetch_limit = _selection_fetch_limit(requested_limit=args.limit, exclude_ids=exclude_ids)

    settings = load_settings(require_groq=True, require_google=True)
    query = build_period_query(args.query, days=int(days))

    ids_from_query: list[str] = []
    payload: dict[str, object] = {}
    if explicit_message_ids:
        ids_from_query = list(explicit_message_ids)
    else:
        payload = search_emails(
            settings,
            query=query,
            max_results=fetch_limit,
            gmail_source=args.gmail_source,
        )
        for item in payload.get("responses") or []:
            mid = _extract_mid(item)
            if mid and mid not in ids_from_query:
                ids_from_query.append(mid)
            if len(ids_from_query) >= fetch_limit:
                break

    if exclude_ids:
        before_n = len(ids_from_query)
        ids_from_query = [m for m in ids_from_query if m not in exclude_ids]
        dropped = before_n - len(ids_from_query)
        if dropped and args.verbose_selection:
            print(
                json.dumps(
                    {"exclude_message_ids": sorted(exclude_ids), "dropped_from_query": dropped},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )
    ids_from_query = ids_from_query[: max(1, int(args.limit))]

    all_selected_ids = list(ids_from_query)
    if (
        resume_effective
        and not explicit_message_ids
        and (batch_dir / "selected_message_ids.json").is_file()
    ):
        try:
            loaded = json.loads((batch_dir / "selected_message_ids.json").read_text(encoding="utf-8"))
            if isinstance(loaded, list) and loaded:
                all_selected_ids = [str(x).strip() for x in loaded if str(x).strip()]
        except (OSError, json.JSONDecodeError):
            pass

    if exclude_ids:
        all_selected_ids = [m for m in all_selected_ids if m not in exclude_ids]
    all_selected_ids = all_selected_ids[: max(1, int(args.limit))]

    completed: set[str] = set()
    if resume_effective:
        completed = load_completed_message_ids(batch_dir, force=bool(args.force))
        if completed:
            print(f"[info] Resume: skipping {len(completed)} completed message ids", file=sys.stderr, flush=True)

    ids = [m for m in all_selected_ids if m not in completed]

    if args.verbose_selection:
        print(
            json.dumps(
                {
                    "query": query,
                    "selected_query": len(ids_from_query),
                    "batch_total": len(all_selected_ids),
                    "remaining": len(ids),
                    "next_page_token_present": bool(str(payload.get("next_page_token") or "").strip()),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )

    intake_py = tool_dir / "gmail_intake.py"
    if not intake_py.is_file():
        print(f"Missing {intake_py}", file=sys.stderr)
        return 2

    child_summaries: list[dict] = []
    proof_items: list[dict] = []
    groq_429_detected_count = 0
    failed_items_records: list[dict] = []

    if resume_effective and not args.dry_run:
        cs_path = batch_dir / "child_summaries.jsonl"
        if cs_path.is_file():
            try:
                text = cs_path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    child_summaries.append(obj)
        pp_batch = batch_dir / "projection_proof_report.batch.json"
        if args.projection_proof and pp_batch.is_file():
            try:
                doc = json.loads(pp_batch.read_text(encoding="utf-8"))
                raw_items = doc.get("items") if isinstance(doc, dict) else []
                proof_items = [dict(r) for r in (raw_items or []) if isinstance(r, dict)]
            except (OSError, json.JSONDecodeError):
                proof_items = []
        failed_items_records = load_failed_items_records(batch_dir)

    started_at = stamp
    if resume_effective and (batch_dir / "sequential_meta.json").is_file():
        try:
            prev = json.loads((batch_dir / "sequential_meta.json").read_text(encoding="utf-8"))
            if isinstance(prev, dict) and str(prev.get("started_at") or "").strip():
                started_at = str(prev["started_at"]).strip()
        except (OSError, json.JSONDecodeError):
            pass

    started_at_iso = datetime.now(timezone.utc).isoformat()
    if resume_effective and (batch_dir / "sequential_meta.json").is_file():
        try:
            meta_prev = json.loads((batch_dir / "sequential_meta.json").read_text(encoding="utf-8"))
            if isinstance(meta_prev, dict) and str(meta_prev.get("started_at_iso") or "").strip():
                started_at_iso = str(meta_prev["started_at_iso"]).strip()
        except (OSError, json.JSONDecodeError):
            pass

    def _merge_proof_for_run(rd: Path | None) -> None:
        if not rd or not args.projection_proof:
            return
        proof_path = rd / "projection_proof_report.json"
        if proof_path.is_file():
            try:
                doc = json.loads(proof_path.read_text(encoding="utf-8"))
                for row in doc.get("items") or []:
                    if isinstance(row, dict):
                        proof_items.append(dict(row))
            except (OSError, json.JSONDecodeError):
                pass

    def _write_failed_items_jsonl() -> None:
        batch_dir.mkdir(parents=True, exist_ok=True)
        path = batch_dir / "failed_items.jsonl"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in failed_items_records),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _write_rollup_partial() -> None:
        finished_at_iso = datetime.now(timezone.utc).isoformat()
        if args.dry_run:
            rollup_core = aggregate_projection_batch_summary(child_summaries=[], proof_items=[])
            rollup_core["batch_dir"] = str(batch_dir.resolve())
            rollup_core["child_summaries_count"] = 0
            rollup_core["proof_items_count"] = 0
            full_summary = build_sequential_operator_summary(
                rollup_core=rollup_core,
                batch_dir=batch_dir,
                requested_message_ids=list(all_selected_ids),
                dry_run=True,
                started_at=started_at_iso,
                finished_at=finished_at_iso,
                groq_429_detected_count=0,
                projection_proof_enabled=bool(args.projection_proof),
                proof_items=[],
            )
            _atomic_write_json(batch_dir / "sequential_summary.json", full_summary)
            if args.projection_proof:
                merged = {
                    "summary": full_summary,
                    "items": [],
                    "source": "sequential_gmail_ingress_daszek",
                    "projection_breakdown": full_summary.get("projection_breakdown"),
                    "note": "dry_run selection only; no child runs ÔÇö v2/readback counts are zero by design",
                }
                _atomic_write_json(batch_dir / "projection_proof_report.batch.json", merged)
            return

        rollup_core = aggregate_projection_batch_summary(child_summaries=child_summaries, proof_items=proof_items)
        rollup_core["batch_dir"] = str(batch_dir.resolve())
        rollup_core["child_summaries_count"] = len(child_summaries)
        rollup_core["proof_items_count"] = len(proof_items)
        full_summary = build_sequential_operator_summary(
            rollup_core=rollup_core,
            batch_dir=batch_dir,
            requested_message_ids=list(all_selected_ids),
            dry_run=False,
            started_at=started_at_iso,
            finished_at=finished_at_iso,
            groq_429_detected_count=groq_429_detected_count,
            projection_proof_enabled=bool(args.projection_proof),
            proof_items=proof_items,
        )
        _atomic_write_json(batch_dir / "sequential_summary.json", full_summary)
        with (batch_dir / "child_summaries.jsonl").open("w", encoding="utf-8") as fh:
            for s in child_summaries:
                fh.write(json.dumps(s, ensure_ascii=False) + "\n")
        _write_failed_items_jsonl()
        if args.projection_proof:
            merged = {
                "summary": full_summary,
                "items": proof_items,
                "source": "sequential_gmail_ingress_daszek",
                "projection_breakdown": full_summary.get("projection_breakdown"),
            }
            _atomic_write_json(batch_dir / "projection_proof_report.batch.json", merged)

    if not args.dry_run:
        batch_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "started_at": started_at,
            "started_at_iso": started_at_iso,
            "query": query,
            "message_ids": all_selected_ids,
            "gmail_source": args.gmail_source,
            "push_daszek": bool(args.push_daszek),
            "projection_proof": bool(args.projection_proof),
            "max_retries_per_message": int(args.max_retries_per_message),
            "retry_base_delay": float(args.retry_base_delay),
            "retry_max_delay": float(args.retry_max_delay),
            "batch_dir": str(batch_dir.resolve()),
            "resume_effective": resume_effective,
            "force": bool(args.force),
            "exclude_message_ids": sorted(exclude_ids),
            "explicit_message_ids": list(explicit_message_ids),
        }
        _atomic_write_json(batch_dir / "sequential_meta.json", meta)
        _atomic_write_json(batch_dir / "selected_message_ids.json", all_selected_ids)

    exit_code = 0

    try:
        for i, mid in enumerate(ids, 1):
            print(f"=== [{i}/{len(ids)}] message_id={mid} ===", flush=True)
            if args.dry_run:
                continue

            attempt = 0
            terminal = False
            while not terminal and attempt < max_attempts:
                attempt += 1
                cmd = build_gmail_intake_message_command(
                    python_executable=sys.executable,
                    intake_py=str(intake_py),
                    message_id=mid,
                    gmail_source=args.gmail_source,
                    push_daszek=bool(args.push_daszek),
                    projection_proof=bool(args.projection_proof),
                    keep_going=keep_going,
                    verbose=bool(args.verbose),
                )
                proc = subprocess.run(cmd, cwd=str(tool_dir), capture_output=True, text=True, encoding="utf-8", errors="replace")
                out = proc.stdout or ""
                err = proc.stderr or ""
                if out:
                    print(out, end="" if out.endswith("\n") else "\n", flush=True)
                if err:
                    print(err, end="" if err.endswith("\n") else "\n", file=sys.stderr, flush=True)

                last_summary = parse_first_json_object(out)
                last_summary = last_summary if isinstance(last_summary, dict) else None

                rd_path = extract_run_dir_from_text(
                    stdout=out,
                    stderr=err,
                    runs_root=runs_root,
                    parsed_summary=last_summary,
                )
                rid = ""
                rdir_s = ""
                if last_summary:
                    rid = str(last_summary.get("run_id") or "").strip()
                if rd_path:
                    rdir_s = str(rd_path.resolve())
                    if not rid:
                        rid = rd_path.name

                rate = is_rate_limit_signal(
                    returncode=int(proc.returncode),
                    stdout=out,
                    stderr=err,
                    parsed_summary=last_summary,
                )
                if rate:
                    groq_429_detected_count += 1

                if rate and attempt < max_attempts:
                    delay = compute_retry_delay(
                        attempt=attempt,
                        base=float(args.retry_base_delay),
                        cap=float(args.retry_max_delay),
                    )
                    print(
                        f"[warn] Rate limit for {mid}, sleeping {delay:.1f}s before retry {attempt + 1}/{max_attempts}",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(delay)
                    _append_index_row(
                        batch_dir,
                        make_child_runs_index_row(
                            message_id=mid,
                            attempt=attempt,
                            returncode=proc.returncode,
                            run_id=rid,
                            run_dir=rdir_s,
                            parsed_summary_present=last_summary is not None,
                            rate_limited=True,
                            final=False,
                            final_status="retry",
                        ),
                    )
                    continue

                # Terminal attempt for this message
                terminal = True

                rd_for_proof = rd_path
                if isinstance(last_summary, dict):
                    child_summaries.append(last_summary)
                    rd_for_proof = rd_path or _run_dir_from_summary(runs_root, last_summary)
                    _merge_proof_for_run(rd_for_proof)

                if rate and attempt >= max_attempts:
                    final_status = "rate_limit_exhausted"
                elif isinstance(last_summary, dict):
                    final_status = str(last_summary.get("status") or "completed")
                else:
                    final_status = "failed"

                proof_rel = ""
                if rd_for_proof and args.projection_proof:
                    pp = rd_for_proof / "projection_proof_report.json"
                    if pp.is_file():
                        proof_rel = str(pp.resolve())

                _append_index_row(
                    batch_dir,
                    make_child_runs_index_row(
                        message_id=mid,
                        attempt=attempt,
                        returncode=proc.returncode,
                        run_id=rid,
                        run_dir=rdir_s,
                        parsed_summary_present=last_summary is not None,
                        rate_limited=rate,
                        final=True,
                        final_status=final_status,
                    ),
                )

                if proc.returncode != 0 and not rate:
                    print(f"WARN: gmail_intake.py exit {proc.returncode} for {mid}", file=sys.stderr)

                ok_summary = isinstance(last_summary, dict) and str(last_summary.get("status") or "") in {
                    "completed",
                    "completed_with_errors",
                }
                if ok_summary:
                    failed_items_records = [r for r in failed_items_records if r.get("message_id") != mid]
                else:
                    failed_items_records = [r for r in failed_items_records if r.get("message_id") != mid]
                    failed_items_records.append(
                        make_failed_item_record(
                            message_id=mid,
                            returncode=proc.returncode,
                            stdout_parse_ok=isinstance(last_summary, dict),
                            stderr_tail=err,
                            stdout_tail=out,
                            rate_limit_hint=rate and attempt >= max_attempts,
                            run_dir=rdir_s,
                            projection_proof_path=proof_rel,
                            final_status=final_status,
                            terminal_attempt=attempt,
                        )
                    )

                _write_rollup_partial()

                if i < len(ids) and args.delay > 0:
                    time.sleep(args.delay)

    except KeyboardInterrupt:
        print("\n[warn] Interrupted ÔÇö writing partial batch artifacts", file=sys.stderr, flush=True)
        exit_code = 130
    finally:
        _write_rollup_partial()
        if bool(args.projection_proof):
            bp = batch_dir / "projection_proof_report.batch.json"
            if not bp.is_file():
                finished_at_iso = datetime.now(timezone.utc).isoformat()
                rollup_core = aggregate_projection_batch_summary(
                    child_summaries=child_summaries if not args.dry_run else [],
                    proof_items=proof_items if not args.dry_run else [],
                )
                rollup_core["batch_dir"] = str(batch_dir.resolve())
                rollup_core["child_summaries_count"] = len(child_summaries) if not args.dry_run else 0
                rollup_core["proof_items_count"] = len(proof_items) if not args.dry_run else 0
                full_summary = build_sequential_operator_summary(
                    rollup_core=rollup_core,
                    batch_dir=batch_dir,
                    requested_message_ids=list(all_selected_ids),
                    dry_run=bool(args.dry_run),
                    started_at=started_at_iso,
                    finished_at=finished_at_iso,
                    groq_429_detected_count=groq_429_detected_count if not args.dry_run else 0,
                    projection_proof_enabled=True,
                    proof_items=proof_items if not args.dry_run else [],
                )
                merged = {
                    "summary": full_summary,
                    "items": proof_items if not args.dry_run else [],
                    "source": "sequential_gmail_ingress_daszek",
                    "projection_breakdown": full_summary.get("projection_breakdown"),
                    "note": "emergency rollup: prior write did not produce projection_proof_report.batch.json",
                }
                _atomic_write_json(bp, merged)
        print(f"[info] Sequential batch artifacts: {batch_dir}", file=sys.stderr, flush=True)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
