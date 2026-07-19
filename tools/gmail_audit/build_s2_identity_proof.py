from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import psycopg


def as_text(value: Any) -> str:
    return str(value or "").strip()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_dt(value: str) -> datetime:
    text = as_text(value)
    if not text:
        raise ValueError("Missing timestamp.")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).astimezone(timezone.utc)


@dataclass(frozen=True)
class ProofRun:
    proof_dir: Path
    message_id: str
    signal_id: str
    engagement_id: str
    case_id: str
    snapshot_id: str
    title: str
    run_id: str
    started_at: datetime
    finished_at: datetime
    live_request_url: str
    live_request_status: int
    latest_snapshot_matches_handoff: bool
    latest_source_run_id: str
    matched_card_ids: list[str]
    matched_card_count: int
    latest_source_signal_ids: list[str]
    latest_source_message_id: str


def load_proof_run(proof_dir: Path) -> ProofRun:
    handoff = load_json(proof_dir / "OPERATOR_ROW4_HANDOFF.json")
    item = handoff["item"]
    exact = load_json(proof_dir / "exact-snapshot-membership.json")
    anchors = load_json(proof_dir / "browser" / "anchors.json")
    child = load_jsonl(proof_dir / "row3-1" / "child_summaries.jsonl")
    seq = load_json(proof_dir / "row3-1" / "sequential_summary.json")
    if not child:
        raise RuntimeError(f"{proof_dir}: missing child_summaries rows")
    run_id = as_text(exact.get("latest_source_run_id") or child[-1].get("run_id"))
    return ProofRun(
        proof_dir=proof_dir,
        message_id=as_text(item.get("message_id") or item.get("source_message_id")),
        signal_id=as_text(item.get("signal_id")),
        engagement_id=as_text(item.get("engagement_id")),
        case_id=as_text(item.get("case_id")),
        snapshot_id=as_text(item.get("snapshot_id")),
        title=as_text(item.get("title")),
        run_id=run_id,
        started_at=parse_dt(as_text(seq.get("started_at"))),
        finished_at=parse_dt(as_text(seq.get("finished_at"))),
        live_request_url=as_text(anchors.get("live_request_url")),
        live_request_status=int(anchors.get("live_request_status") or 0),
        latest_snapshot_matches_handoff=bool(exact.get("latest_matches_handoff_snapshot")),
        latest_source_run_id=as_text(exact.get("latest_source_run_id")),
        matched_card_ids=[as_text(exact.get("membership", {}).get("card_id"))] if as_text(exact.get("membership", {}).get("card_id")) else [],
        matched_card_count=1 if bool(exact.get("membership_found")) else 0,
        latest_source_signal_ids=[as_text(x) for x in exact.get("membership", {}).get("source_signal_ids", []) if as_text(x)],
        latest_source_message_id=as_text(exact.get("membership", {}).get("source_message_id")),
    )


def fetch_os_events(base_url: str, engagement_id: str) -> list[dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/engagements/{engagement_id}/os-events"
    with urlopen(url, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    items = payload.get("items")
    if not isinstance(items, list):
        raise RuntimeError(f"{url}: invalid os-events payload")
    return [row for row in items if isinstance(row, dict)]


def select_agent_run_trace_id(items: list[dict[str, Any]], proof_run: ProofRun) -> tuple[str, str]:
    floor = proof_run.started_at - timedelta(seconds=120)
    ceil = proof_run.finished_at + timedelta(seconds=120)
    matches: list[dict[str, Any]] = []
    for item in items:
        if as_text(item.get("event_type")) != "agent.run.started":
            continue
        if as_text(item.get("engagement_id")) != proof_run.engagement_id:
            continue
        occurred_at = parse_dt(as_text(item.get("occurred_at")))
        if floor <= occurred_at <= ceil:
            matches.append(item)
    if len(matches) != 1:
        raise RuntimeError(
            f"{proof_run.proof_dir}: expected exactly one agent.run.started in proof window, got {len(matches)}"
        )
    match = matches[0]
    return as_text(match.get("trace_id")), "os_events agent.run.started correlated by proof window"


def query_engagement_row(db_url: str, engagement_id: str, signal_id: str) -> dict[str, Any]:
    sql = """
        select
            engagement_id,
            case_id,
            version,
            last_trace_id,
            snapshot_data->>'signal_id' as signal_id,
            snapshot_data->>'trace_id' as snapshot_trace_id
        from operator_engagement_snapshots
        where engagement_id = %s and snapshot_data->>'signal_id' = %s
    """
    with psycopg.connect(db_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(sql, (engagement_id, signal_id))
            row = cur.fetchone()
    if not row:
        raise RuntimeError(f"operator_engagement_snapshots row not found for engagement_id={engagement_id} signal_id={signal_id}")
    return dict(row)


def query_uniqueness(db_url: str, signal_id: str) -> dict[str, Any]:
    sql_primary = """
        select engagement_id, case_id, version, last_trace_id,
               snapshot_data->>'signal_id' as signal_id,
               snapshot_data->>'trace_id' as snapshot_trace_id
        from operator_engagement_snapshots
        where snapshot_data->>'signal_id' = %s
        order by engagement_id
    """
    sql_legacy = """
        select engagement_id, case_id, version, last_trace_id,
               snapshot_data->>'signal_id' as signal_id,
               snapshot_data->>'trace_id' as snapshot_trace_id
        from operator_engagement_snapshots
        where snapshot_data->>'trace_id' = %s or last_trace_id = %s
        order by engagement_id
    """
    with psycopg.connect(db_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(sql_primary, (signal_id,))
            rows = [dict(row) for row in cur.fetchall()]
            mode = "canonical_signal_id"
            if not rows:
                cur.execute(sql_legacy, (signal_id, signal_id))
                rows = [dict(row) for row in cur.fetchall()]
                mode = "legacy_trace_fallback"
    return {
        "query_mode": mode,
        "signal_id": signal_id,
        "engagement_ids": [as_text(row.get("engagement_id")) for row in rows if as_text(row.get("engagement_id"))],
        "rows": rows,
        "count": len(rows),
    }


def read_latest_snapshot_cards(proof_run: ProofRun) -> list[dict[str, Any]]:
    network = load_json(proof_run.proof_dir / "browser" / "network.json")
    if not isinstance(network, list):
        raise RuntimeError(f"{proof_run.proof_dir}: invalid browser/network.json")
    latest_body = ""
    for row in network:
        if not isinstance(row, dict):
            continue
        url = as_text(row.get("url"))
        if "/wp-json/daszek/v3/operational-feed-snapshots/latest" not in url:
            continue
        if int(row.get("status") or 0) != 200:
            continue
        latest_body = as_text(row.get("body"))
    if not latest_body:
        raise RuntimeError(f"{proof_run.proof_dir}: latest snapshot response not found in browser/network.json")
    payload = json.loads(latest_body)
    desk = payload.get("snapshot", {}).get("feed", {}).get("desk", [])
    if not isinstance(desk, list):
        raise RuntimeError(f"{proof_run.proof_dir}: invalid desk payload in latest snapshot")
    return [row for row in desk if isinstance(row, dict)]


def matched_cards_for_signal(cards: list[dict[str, Any]], signal_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in cards
        if signal_id in [as_text(item) for item in row.get("source_signal_ids", []) if as_text(item)]
    ]


def assert_cards_do_not_embed_trace(cards: list[dict[str, Any]], trace_ids: list[str]) -> None:
    forbidden = {trace_id for trace_id in trace_ids if trace_id}
    if not forbidden:
        return
    for row in cards:
        signals = {as_text(item) for item in row.get("source_signal_ids", []) if as_text(item)}
        overlap = signals & forbidden
        if overlap:
            raise RuntimeError(f"source_signal_ids contains technical trace_id(s): {sorted(overlap)}")


def load_pre_replay_baseline(current_proof_dir: Path) -> dict[str, Any]:
    path = current_proof_dir / "pre-replay-baseline.json"
    if not path.is_file():
        raise RuntimeError(f"{current_proof_dir}: missing pre-replay-baseline.json")
    return load_json(path)


def build_identity_record(
    *,
    label: str,
    proof_run: ProofRun,
    signal_runtime_trace_id: str,
    signal_runtime_trace_source: str,
    agent_run_trace_id: str,
    agent_run_trace_source: str,
    current_db_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "label": label,
        "proof_dir": str(proof_run.proof_dir),
        "message_id": proof_run.message_id,
        "signal_id": proof_run.signal_id,
        "engagement_id": proof_run.engagement_id,
        "case_id": proof_run.case_id,
        "snapshot_id": proof_run.snapshot_id,
        "run_id": proof_run.run_id,
        "signal_runtime_trace_id": signal_runtime_trace_id,
        "signal_runtime_trace_source": signal_runtime_trace_source,
        "agent_run_trace_id": agent_run_trace_id,
        "agent_run_trace_source": agent_run_trace_source,
        "latest_snapshot_matches_handoff": proof_run.latest_snapshot_matches_handoff,
        "latest_source_run_id": proof_run.latest_source_run_id,
        "matched_card_count": proof_run.matched_card_count,
        "matched_card_ids": proof_run.matched_card_ids,
        "live_request_url": proof_run.live_request_url,
        "live_request_status": proof_run.live_request_status,
    }
    if current_db_row is not None:
        record["current_db_row"] = current_db_row
    return record


def build_contract_map(previous_proof_dir: Path, current_proof_dir: Path) -> dict[str, Any]:
    return {
        "message_id": {
            "producer": "Gmail source message / source_ref.message_id",
            "persistence": [f"{previous_proof_dir}\\OPERATOR_ROW4_HANDOFF.json", f"{current_proof_dir}\\OPERATOR_ROW4_HANDOFF.json"],
            "consumers": ["handoff", "feed card source_message_id", "browser proof"],
            "correlation": "exact equality across replay",
        },
        "signal_id": {
            "producer": "canonical signal journal / signal_projection.signal_id",
            "persistence": ["stage_records.jsonl", "operator_engagement_snapshots.snapshot_data.signal_id"],
            "consumers": ["source_signal_ids", "projection proof", "uniqueness query"],
            "correlation": "stable across replay",
        },
        "trace_id": {
            "producer": "runtime attempt / engagement snapshot last_trace_id",
            "persistence": ["operator_engagement_snapshots.last_trace_id", "os_events.trace_id for agent_run_trace_id"],
            "consumers": ["observability", "identity proof only"],
            "correlation": "must change across replay and never replace signal_id in source_signal_ids",
        },
        "run_id": {
            "producer": "sequential runner child summary",
            "persistence": ["row3-1/child_summaries.jsonl", "latest snapshot source.source_run_id"],
            "consumers": ["proof correlation", "latest snapshot verification"],
            "correlation": "must change across replay",
        },
        "engagement_id": {
            "producer": "staging engagement resolver",
            "persistence": ["handoff", "operator_engagement_snapshots", "feed desk card"],
            "consumers": ["browser click", "live Node B detail", "uniqueness check"],
            "correlation": "stable across replay for same signal_id",
        },
        "case_id": {
            "producer": "materialization only",
            "persistence": ["handoff", "operator_engagement_snapshots"],
            "consumers": ["materialized case detail only"],
            "correlation": "empty for staging Row4a proof",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proof-dir-a", required=True)
    parser.add_argument("--proof-dir-b", required=True)
    parser.add_argument("--node-b-url", default=os.environ.get("GMAIL_AGENT_NODEB_URL", "http://127.0.0.1:8766"))
    parser.add_argument("--db-url", default=os.environ.get("MAILBOX_MEMORY_DATABASE_URL", ""))
    args = parser.parse_args(argv)

    if not args.db_url:
        raise RuntimeError("MAILBOX_MEMORY_DATABASE_URL / --db-url is required")

    proof_dir_a = Path(args.proof_dir_a).resolve()
    proof_dir_b = Path(args.proof_dir_b).resolve()
    run_a = load_proof_run(proof_dir_a)
    run_b = load_proof_run(proof_dir_b)
    baseline = load_pre_replay_baseline(proof_dir_b)

    if as_text(baseline.get("signal_id")) != run_a.signal_id:
        raise RuntimeError("pre-replay baseline signal_id does not match proof-dir-a signal_id")
    if as_text(baseline.get("engagement_id")) != run_a.engagement_id:
        raise RuntimeError("pre-replay baseline engagement_id does not match proof-dir-a engagement_id")

    os_events = fetch_os_events(args.node_b_url, run_b.engagement_id)
    agent_trace_a, agent_trace_a_source = select_agent_run_trace_id(os_events, run_a)
    agent_trace_b, agent_trace_b_source = select_agent_run_trace_id(os_events, run_b)

    baseline_row = baseline.get("engagement_row") if isinstance(baseline.get("engagement_row"), dict) else {}
    signal_trace_a = as_text(baseline_row.get("last_trace_id"))
    if not signal_trace_a:
        raise RuntimeError("pre-replay baseline missing engagement_row.last_trace_id")
    current_row = query_engagement_row(args.db_url, run_b.engagement_id, run_b.signal_id)
    signal_trace_b = as_text(current_row.get("last_trace_id"))
    if not signal_trace_b:
        raise RuntimeError("current operator engagement row missing last_trace_id")
    if signal_trace_a == signal_trace_b:
        raise RuntimeError("signal_runtime_trace_id did not change across replay")

    latest_cards_a = matched_cards_for_signal(read_latest_snapshot_cards(run_a), run_a.signal_id)
    latest_cards_b = matched_cards_for_signal(read_latest_snapshot_cards(run_b), run_b.signal_id)
    uniqueness = query_uniqueness(args.db_url, run_b.signal_id)

    assert_cards_do_not_embed_trace(latest_cards_a, [signal_trace_a, agent_trace_a])
    assert_cards_do_not_embed_trace(latest_cards_b, [signal_trace_b, agent_trace_b])

    identity_a = build_identity_record(
        label="A",
        proof_run=run_a,
        signal_runtime_trace_id=signal_trace_a,
        signal_runtime_trace_source=f"{proof_dir_b}\\pre-replay-baseline.json engagement_row.last_trace_id",
        agent_run_trace_id=agent_trace_a,
        agent_run_trace_source=agent_trace_a_source,
    )
    identity_b = build_identity_record(
        label="B",
        proof_run=run_b,
        signal_runtime_trace_id=signal_trace_b,
        signal_runtime_trace_source="operator_engagement_snapshots.last_trace_id after replay",
        agent_run_trace_id=agent_trace_b,
        agent_run_trace_source=agent_trace_b_source,
        current_db_row=current_row,
    )

    comparison = {
        "baseline_proof_dir": str(proof_dir_a),
        "current_proof_dir": str(proof_dir_b),
        "message_id_equal": identity_a["message_id"] == identity_b["message_id"],
        "signal_id_equal": identity_a["signal_id"] == identity_b["signal_id"],
        "engagement_id_equal": identity_a["engagement_id"] == identity_b["engagement_id"],
        "case_id_equal": identity_a["case_id"] == identity_b["case_id"],
        "run_id_different": identity_a["run_id"] != identity_b["run_id"],
        "signal_runtime_trace_id_different": identity_a["signal_runtime_trace_id"] != identity_b["signal_runtime_trace_id"],
        "agent_run_trace_id_different": identity_a["agent_run_trace_id"] != identity_b["agent_run_trace_id"],
        "latest_snapshot_points_to_same_engagement_after_b": run_b.engagement_id == identity_b["engagement_id"],
        "cards_for_signal_in_run_a_latest": len(latest_cards_a),
        "cards_for_signal_in_run_b_latest": len(latest_cards_b),
        "snapshot_ids_different": identity_a["snapshot_id"] != identity_b["snapshot_id"],
        "source_signal_ids_stable": all(
            run_b.signal_id in [as_text(item) for item in row.get("source_signal_ids", []) if as_text(item)] for row in latest_cards_b
        ),
        "source_message_id_stable": all(as_text(row.get("source_message_id")) == run_b.message_id for row in latest_cards_b),
    }

    uniqueness_payload = {
        **uniqueness,
        "matched_cards_in_latest": [
            {
                "note_id": as_text(row.get("note_id")),
                "engagement_id": as_text(row.get("engagement_id")),
                "case_id": as_text(row.get("case_id")),
                "source_message_id": as_text(row.get("source_message_id")),
                "source_signal_ids": [as_text(x) for x in row.get("source_signal_ids", []) if as_text(x)],
                "title": as_text(row.get("title")),
            }
            for row in latest_cards_b
        ],
        "matched_cards_count": len(latest_cards_b),
    }

    contract_map = build_contract_map(proof_dir_a, proof_dir_b)
    final_summary = load_json(proof_dir_b / "final-summary.json")
    final_summary["identity"] = {
        "baseline_proof_dir": str(proof_dir_a),
        "current_proof_dir": str(proof_dir_b),
        "identity_run_a_path": str(proof_dir_b / "identity-run-a.json"),
        "identity_run_b_path": str(proof_dir_b / "identity-run-b.json"),
        "identity_replay_comparison_path": str(proof_dir_b / "identity-replay-comparison.json"),
        "identity_database_uniqueness_path": str(proof_dir_b / "identity-database-uniqueness.json"),
    }

    write_json(proof_dir_b / "identity-run-a.json", identity_a)
    write_json(proof_dir_b / "identity-run-b.json", identity_b)
    write_json(proof_dir_b / "identity-replay-comparison.json", comparison)
    write_json(proof_dir_b / "identity-database-uniqueness.json", uniqueness_payload)
    write_json(proof_dir_b / "identity-contract-map.json", contract_map)
    write_json(proof_dir_b / "final-summary.json", final_summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
