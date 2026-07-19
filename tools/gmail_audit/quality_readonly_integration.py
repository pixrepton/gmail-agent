"""End-to-end read-only quality pipeline: export → projection → optional feed slice → proof-pack."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from artifact_io import read_json, write_json, write_jsonl
from daszek_v3_operational_feed import build_operational_feed_snapshot
from eval_shadow_analytics import export_eval_shadow_analytics_from_csv
from feedback_analytics_export import (
    export_feedback_analytics_from_jsonl,
    export_feedback_analytics_from_store,
    write_feedback_analytics_jsonl,
)
from operator_feedback_runtime import persist_routed_event, route_operator_payload
from quality_readonly_projection import (
    build_quality_readonly_projection,
    sanitize_analytics_record_for_projection,
    write_quality_readonly_projection_json,
)


def _load_analytics_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load JSONL that is either sanitized analytics rows or mailbox/shell feedback rows."""
    from artifact_io import read_jsonl

    rows = [r for r in read_jsonl(path) if isinstance(r, dict)]
    if not rows:
        return []
    if rows[0].get("analytics_group") and rows[0].get("event_domain"):
        out: list[dict[str, Any]] = []
        for row in rows:
            rec = sanitize_analytics_record_for_projection(row)
            if rec:
                out.append(rec)
        return out
    exported, _ = export_feedback_analytics_from_jsonl(path)
    return exported

PROOF_PACK_README = """# Quality read-only local proof pack

**Scope:** local fixture / explicit file inputs only.

## What this proves (locally)

- Feedback/adjudication rows → sanitized `feedback_analytics.jsonl`
- Eval shadow CSV → sanitized `eval_analytics.jsonl`
- Merged `quality_snapshot.json` (read-only projection)
- Optional `operational_feed_with_quality.json` when base feed inputs provided
- `summary.json` with counts and invocation metadata

## What this does NOT prove

- Live Node B mailbox memory on VPS
- Gate B / 24h stability
- Daszek operator readback
- Production Gmail or customer data handling at scale

## Regenerate

From repo root (example):

```powershell
python tools/gmail_audit/quality_readonly_integration.py build-proof-pack --output-dir docs/proof-packs/quality-readonly-local-YYYY-MM-DD --use-fixture-store
```

Or with explicit JSONL inputs:

```powershell
python tools/gmail_audit/quality_readonly_integration.py build-proof-pack --output-dir ./out --feedback-jsonl feedback.jsonl --eval-jsonl eval.jsonl
```

## Mailbox read-only export (explicit)

```powershell
python tools/gmail_audit/quality_readonly_integration.py export-mailbox --output-jsonl feedback_analytics.jsonl --use-fixture-store
```
"""


@dataclass(slots=True)
class QualityPipelineResult:
    feedback_records: list[dict[str, Any]]
    eval_records: list[dict[str, Any]]
    quality_snapshot: dict[str, Any]
    operational_feed_with_quality: dict[str, Any] | None
    summary: dict[str, Any]


def build_fixture_mailbox_store() -> Any:
    """In-memory store with sample feedback + adjudication events (read-only seed)."""
    from mailbox_memory_store import InMemoryMailboxMemoryStore

    store = InMemoryMailboxMemoryStore()
    samples = [
        {
            "calibration_category": "wrong_topic",
            "case_id": "case_fixture_1",
            "event_id": "fe_fixture_1",
            "decision_candidate_id": "dc_fixture_1",
        },
        {
            "event_domain": "adjudication",
            "adjudication_kind": "invalidate_fact",
            "case_id": "case_fixture_1",
            "event_id": "ae_fixture_1",
            "source_signal_id": "sig_fixture_1",
        },
        {
            "calibration_category": "rejected_fact_claim",
            "case_id": "case_fixture_2",
            "event_id": "fe_fixture_2",
            "policy_decision_id": "pd_fixture_2",
        },
    ]
    for raw in samples:
        domain = str(raw.get("event_domain") or "calibration")
        if domain == "adjudication" or raw.get("adjudication_kind"):
            _, ev = route_operator_payload({**raw, "event_domain": "adjudication"})
            persist_routed_event(store, "adjudication", ev)
        else:
            _, ev = route_operator_payload(raw)
            persist_routed_event(store, "calibration", ev)
    return store


def run_mailbox_feedback_export(
    *,
    output_jsonl: Path,
    use_fixture_store: bool = False,
    limit: int = 5000,
) -> dict[str, Any]:
    """Explicit read-only mailbox pull; never runs unless ``use_fixture_store`` or caller supplies store."""
    if not use_fixture_store:
        raise ValueError("Mailbox export requires --use-fixture-store in this workspace (no implicit production DB)")
    store = build_fixture_mailbox_store()
    before = len(store.fetch_events())
    records, export_summary = export_feedback_analytics_from_store(store, limit=limit)
    after = len(store.fetch_events())
    write_feedback_analytics_jsonl(output_jsonl, records)
    return {
        "mailbox_events_before": before,
        "mailbox_events_after": after,
        "store_mutated": before != after,
        "export": export_summary.to_dict(),
        "output_jsonl": str(output_jsonl),
    }


def run_quality_pipeline(
    *,
    feedback_records: list[dict[str, Any]] | None = None,
    eval_records: list[dict[str, Any]] | None = None,
    feedback_jsonl: Path | None = None,
    eval_jsonl: Path | None = None,
    eval_csv: Path | None = None,
    attach_operational_feed: bool = False,
) -> QualityPipelineResult:
    fb = list(feedback_records or [])
    ev = list(eval_records or [])
    if feedback_jsonl:
        fb = _load_analytics_jsonl(feedback_jsonl)
    if eval_jsonl:
        ev = _load_analytics_jsonl(eval_jsonl)
    if eval_csv:
        ev_batch, _ = export_eval_shadow_analytics_from_csv(eval_csv)
        ev.extend(ev_batch)

    quality = build_quality_readonly_projection(fb, ev)
    feed_with_quality = None
    if attach_operational_feed:
        base = build_operational_feed_snapshot(
            cockpit={"desk": {"items": []}, "cases": {"items": []}},
            day={"sections": []},
            tasks=[],
            snapshot_id="quality-proof-feed",
            quality_readonly=quality,
        )
        feed_with_quality = base

    summary = {
        "feedback_record_count": len(fb),
        "eval_shadow_record_count": len(ev),
        "quality_source_summary": quality.get("source_summary"),
        "by_group": quality.get("by_group"),
        "by_domain": quality.get("by_domain"),
        "truth_mutation_summary": quality.get("truth_mutation_summary"),
        "not_proven": quality.get("not_proven"),
        "operational_feed_attached": feed_with_quality is not None,
    }
    return QualityPipelineResult(
        feedback_records=fb,
        eval_records=ev,
        quality_snapshot=quality,
        operational_feed_with_quality=feed_with_quality,
        summary=summary,
    )


def build_quality_readonly_proof_pack(
    output_dir: Path,
    *,
    feedback_jsonl: Path | None = None,
    eval_jsonl: Path | None = None,
    eval_csv: Path | None = None,
    use_fixture_store: bool = False,
    attach_operational_feed: bool = True,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    fb_path = output_dir / "feedback_analytics.jsonl"
    ev_path = output_dir / "eval_analytics.jsonl"
    quality_path = output_dir / "quality_snapshot.json"
    summary_path = output_dir / "summary.json"
    feed_path = output_dir / "operational_feed_with_quality.json"
    readme_path = output_dir / "README.md"
    invocation_path = output_dir / "invocation.json"

    invocation: dict[str, Any] = {
        "command": "quality_readonly_integration.build-proof-pack",
        "output_dir": str(output_dir),
        "use_fixture_store": use_fixture_store,
        "feedback_jsonl_input": str(feedback_jsonl) if feedback_jsonl else "",
        "eval_jsonl_input": str(eval_jsonl) if eval_jsonl else "",
        "eval_csv_input": str(eval_csv) if eval_csv else "",
    }

    if use_fixture_store and not feedback_jsonl:
        mailbox_meta = run_mailbox_feedback_export(output_jsonl=fb_path, use_fixture_store=True)
        invocation["mailbox_export"] = mailbox_meta
        feedback_jsonl = fb_path
    elif feedback_jsonl:
        fb_path = feedback_jsonl

    if eval_csv and not eval_jsonl:
        ev_records, ev_summary = export_eval_shadow_analytics_from_csv(eval_csv)
        write_jsonl(ev_path, ev_records)
        invocation["eval_export"] = ev_summary.to_dict()
        eval_jsonl = ev_path

    result = run_quality_pipeline(
        feedback_jsonl=feedback_jsonl,
        eval_jsonl=eval_jsonl,
        attach_operational_feed=attach_operational_feed,
    )

    if feedback_jsonl != fb_path:
        write_jsonl(fb_path, result.feedback_records)
    if eval_jsonl != ev_path:
        write_jsonl(ev_path, result.eval_records)

    write_quality_readonly_projection_json(quality_path, result.quality_snapshot)
    write_json(summary_path, result.summary)
    write_json(invocation_path, invocation)
    readme_path.write_text(PROOF_PACK_README, encoding="utf-8")

    if result.operational_feed_with_quality is not None:
        write_json(feed_path, result.operational_feed_with_quality)

    return {
        "output_dir": str(output_dir),
        "summary": result.summary,
        "artifacts": {
            "readme": str(readme_path),
            "feedback_analytics": str(fb_path),
            "eval_analytics": str(ev_path),
            "quality_snapshot": str(quality_path),
            "summary": str(summary_path),
            "invocation": str(invocation_path),
            "operational_feed_with_quality": str(feed_path) if result.operational_feed_with_quality else "",
        },
    }


def _default_proof_pack_dir() -> Path:
    stamp = date.today().isoformat()
    return Path("docs/proof-packs") / f"quality-readonly-local-{stamp}"


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quality read-only integration pipeline (local proof).")
    sub = parser.add_subparsers(dest="command", required=True)

    pack = sub.add_parser("build-proof-pack", help="Build local proof-pack directory.")
    pack.add_argument("--output-dir", type=Path, default=None)
    pack.add_argument("--feedback-jsonl", type=Path)
    pack.add_argument("--eval-jsonl", type=Path)
    pack.add_argument("--eval-csv", type=Path)
    pack.add_argument("--use-fixture-store", action="store_true", help="Seed in-memory mailbox + export feedback.")
    pack.add_argument("--no-operational-feed", action="store_true")

    mailbox = sub.add_parser("export-mailbox", help="Explicit mailbox read-only feedback export.")
    mailbox.add_argument("--output-jsonl", type=Path, required=True)
    mailbox.add_argument("--use-fixture-store", action="store_true", required=True)
    mailbox.add_argument("--limit", type=int, default=5000)

    args = parser.parse_args(argv)

    if args.command == "export-mailbox":
        meta = run_mailbox_feedback_export(
            output_jsonl=args.output_jsonl,
            use_fixture_store=True,
            limit=args.limit,
        )
        print(json.dumps(meta, indent=2, ensure_ascii=False))
        return 0

    out_dir = args.output_dir or _default_proof_pack_dir()
    meta = build_quality_readonly_proof_pack(
        out_dir,
        feedback_jsonl=args.feedback_jsonl,
        eval_jsonl=args.eval_jsonl,
        eval_csv=args.eval_csv,
        use_fixture_store=bool(args.use_fixture_store),
        attach_operational_feed=not args.no_operational_feed,
    )
    print(json.dumps(meta, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
