#!/usr/bin/env python3
"""Audit B2: feedback_eligible in feed vs Daszek v2 desk note readback."""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import load_settings
from daszek_client import DaszekClient, DaszekClientError

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HANDOFF_FIXTURE = REPO_ROOT / "deploy/fixtures/b2_handoff_audit_cases.json"
GATE_B_HANDOFF_CASE = "case_062a7aa4ed7b"
GATE_B_HANDOFF_NOTE = "note_211248db920e"


def _eligible_cases(feed: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in feed.get("cases") or []:
        if not isinstance(row, dict):
            continue
        if row.get("feedback_eligible") is True and str(row.get("case_id") or "").strip():
            out.append(row)
    return out


def _cases_by_id(feed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in feed.get("cases") or []:
        if isinstance(row, dict):
            cid = str(row.get("case_id") or "").strip()
            if cid:
                index[cid] = row
    return index


def _select_sample(
    eligible: list[dict[str, Any]],
    *,
    case_ids: list[str],
    optional_case_ids: list[str] | None = None,
    sample_n: int,
    seed: int,
) -> tuple[list[dict[str, Any]], str]:
    if case_ids:
        by_id = {str(c.get("case_id") or "").strip(): c for c in eligible}
        picked: list[dict[str, Any]] = []
        missing: list[str] = []
        for cid in case_ids:
            row = by_id.get(cid)
            if row:
                picked.append(row)
            else:
                missing.append(cid)
        for cid in optional_case_ids or []:
            row = by_id.get(cid)
            if row:
                picked.append(row)
        if missing:
            raise SystemExit(f"handoff cases not in feed or not feedback_eligible: {missing}")
        return picked, "handoff_bounded"
    rng = random.Random(seed) if seed else random.Random()
    n = sample_n if sample_n > 0 else 10
    if len(eligible) <= n:
        return list(eligible), "random_full"
    return rng.sample(eligible, n), "random_sample"


def _note_exists(client: DaszekClient, note_id: str) -> tuple[bool, str]:
    try:
        detail = client.get_v2_note_detail(note_id)
    except DaszekClientError as exc:
        return False, str(exc)
    note = detail.get("note") if isinstance(detail, dict) else {}
    if isinstance(note, dict) and str(note.get("note_id") or note.get("desk_note_id") or "").strip():
        return True, "found"
    store = str(detail.get("store_readback") or "").strip().lower()
    if store == "found":
        return True, "store_readback=found"
    return False, "note payload empty"


def _load_feed(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if args.feed_json and args.feed_json.is_file():
        payload = json.loads(args.feed_json.read_text(encoding="utf-8"))
        feed = payload.get("feed") if isinstance(payload, dict) else {}
        return feed if isinstance(feed, dict) else {}, str(payload.get("snapshot_id") or "")
    from mailbox_memory_runtime import build_mailbox_memory_runtime
    from daszek_v3_operational_feed import build_operational_feed_from_mailbox_store

    settings = load_settings()
    runtime = build_mailbox_memory_runtime(settings)
    payload = build_operational_feed_from_mailbox_store(
        runtime.store,
        case_limit=50,
        task_limit=80,
    )
    feed = payload.get("feed") if isinstance(payload, dict) else {}
    return feed if isinstance(feed, dict) else {}, str(payload.get("snapshot_id") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--feed-json",
        type=Path,
        help="operational_feed_snapshot.json (default: build from mailbox memory)",
    )
    parser.add_argument("--sample", type=int, default=10, help="Max cases for random mode")
    parser.add_argument("--seed", type=int, default=0, help="RNG seed (0 = random)")
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="Audit only these case_ids from feed (repeatable). Overrides random sample.",
    )
    parser.add_argument(
        "--handoff-fixture",
        type=Path,
        default=None,
        help="JSON with case_ids list for bounded handoff audit",
    )
    parser.add_argument(
        "--handoff-bounded",
        action="store_true",
        help=f"Use {DEFAULT_HANDOFF_FIXTURE.relative_to(REPO_ROOT)} case_ids",
    )
    parser.add_argument(
        "--handoff-gateb",
        action="store_true",
        help=f"Shortcut: audit only {GATE_B_HANDOFF_CASE}",
    )
    parser.add_argument(
        "--require-gateb",
        action="store_true",
        help=f"Exit 0 only if {GATE_B_HANDOFF_CASE} readback OK (others may fail in handoff set)",
    )
    parser.add_argument("--out", type=Path, default=None, help="Write JSON report path")
    args = parser.parse_args()

    case_ids: list[str] = [str(c).strip() for c in args.case_id if str(c).strip()]
    optional_case_ids: list[str] = []
    if args.handoff_gateb:
        case_ids = [GATE_B_HANDOFF_CASE]
    elif args.handoff_bounded or args.handoff_fixture:
        fixture = args.handoff_fixture or (
            Path("/app/b2_handoff_audit_cases.json")
            if Path("/app/b2_handoff_audit_cases.json").is_file()
            else DEFAULT_HANDOFF_FIXTURE
        )
        if not fixture.is_file():
            print(f"ERROR: handoff fixture missing: {fixture}", file=sys.stderr)
            return 2
        spec = json.loads(fixture.read_text(encoding="utf-8"))
        if isinstance(spec.get("case_ids"), list):
            case_ids = [str(x).strip() for x in spec["case_ids"] if str(x).strip()]
        if isinstance(spec.get("optional_case_ids"), list):
            optional_case_ids = [str(x).strip() for x in spec["optional_case_ids"] if str(x).strip()]

    feed, snapshot_id = _load_feed(args)
    eligible = _eligible_cases(feed)
    if not eligible:
        print("WARN: no feedback_eligible cases in feed", file=sys.stderr)
        return 2

    try:
        sample, audit_mode = _select_sample(
            eligible,
            case_ids=case_ids,
            optional_case_ids=optional_case_ids,
            sample_n=args.sample,
            seed=args.seed,
        )
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    settings = load_settings()
    client = DaszekClient(settings)
    client.login()

    rows: list[dict[str, Any]] = []
    ok_count = 0
    gateb_ok = False
    for case in sample:
        case_id = str(case.get("case_id") or "").strip()
        note_id = str(case.get("v2_desk_note_id") or "").strip()
        row: dict[str, Any] = {
            "case_id": case_id,
            "v2_desk_note_id": note_id,
            "feedback_eligible": True,
        }
        if not note_id:
            row["ok"] = False
            row["reason"] = "missing v2_desk_note_id in feed"
        else:
            found, reason = _note_exists(client, note_id)
            row["ok"] = found
            row["reason"] = reason
            if found:
                ok_count += 1
        if case_id == GATE_B_HANDOFF_CASE and row.get("ok"):
            gateb_ok = True
            if note_id != GATE_B_HANDOFF_NOTE:
                row["warn"] = f"expected note {GATE_B_HANDOFF_NOTE}"
        rows.append(row)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "audit_mode": audit_mode,
        "snapshot_id": snapshot_id,
        "eligible_total": len(eligible),
        "sample_size": len(sample),
        "ok_count": ok_count,
        "ok_ratio": round(ok_count / len(sample), 3) if sample else 0.0,
        "gateb_handoff_ok": gateb_ok,
        "rows": rows,
    }
    out_path = args.out
    if out_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = "handoff" if case_ids else "random"
        out_path = Path(__file__).resolve().parent / "runs" / f"b2-readback-audit-{suffix}-{stamp}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"ok_count": ok_count, "sample": len(sample), "audit_mode": audit_mode, "out": str(out_path)}, ensure_ascii=False))

    if args.require_gateb or (case_ids and GATE_B_HANDOFF_CASE in case_ids):
        if gateb_ok:
            print("B2_HANDOFF_READBACK_AUDIT_OK", GATE_B_HANDOFF_CASE, GATE_B_HANDOFF_NOTE)
            return 0
        print(f"FAIL: Gate B handoff {GATE_B_HANDOFF_CASE} readback not OK", file=sys.stderr)
        return 1

    if ok_count < len(sample):
        return 1
    print("B2_FEED_READBACK_AUDIT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
