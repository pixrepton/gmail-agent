#!/usr/bin/env python3
"""Bounded local Docker proof for Daszek 1.3.3 (HITL + live feed + Skrzat + Move 5).

Requires:
  - daszek-local-wordpress on :8090
  - gmail-agent-nodeb-api on :8765
  - tools/gmail_audit/.env with DASZEK_BASE_URL, LOGIN, PASSWORD, tokens
  - mailbox memory Postgres for live feed + Skrzat paths

Stdout on success: DASZEK_LOCAL_133_PROOF_OK
Writes JSON report to runs/daszek-local-133-proof-<ts>/report.json
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

TOOL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
REPO = TOOL_DIR.parents[1]
WORKSPACE = REPO.parent
FIXTURE_PATH = WORKSPACE / "daszek" / "fixtures" / "v3" / "operational_feed_agent_runtime.json"

if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agent_runtime.mcp_service import AgentMcpService  # noqa: E402
from agent_runtime.settings import load_agent_runtime_settings  # noqa: E402
from agent_runtime.snapshot_delta import apply_snapshot_delta  # noqa: E402
from agent_runtime.store import PostgresOperatorEngagementStore, build_initial_snapshot  # noqa: E402
from config import load_settings  # noqa: E402
from llm_contracts.engagement_snapshot_v2 import ActionItem  # noqa: E402

PROOF_ENGAGEMENT_ID = "eng_local_hitl_proof_133"
PROOF_CASE_ID = "case_local_hitl_proof_133"
KNOWN_LIVE_FEED_PREFIX = "eng-feed-9cc5eb2c"
FALLBACK_SKRZAT_CASE = "case_bdff44af363c"


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def _token(settings: Any) -> str:
    service = str(getattr(settings, "daszek_node_b_service_token", "") or "").strip()
    if service:
        return service
    bridge = str(getattr(settings, "daszek_bridge_token", "") or "").strip()
    if bridge:
        return bridge
    raise RuntimeError("Missing DASZEK_NODE_B_SERVICE_TOKEN or DASZEK_BRIDGE_TOKEN")


def _node_b_token(settings: Any) -> str:
    raw = os.getenv("NODE_B_REGISTRY_TOKEN", "").strip()
    if raw:
        return raw
    return _token(settings)


def _base_url(settings: Any) -> str:
    return str(settings.daszek_base_url or "http://127.0.0.1:8090").rstrip("/")


def _node_b_base() -> str:
    return (os.getenv("NODE_B_REGISTRY_BASE_URL") or "http://127.0.0.1:8765").rstrip("/")


def _session_login(base: str, login: str, password: str) -> requests.Session:
    sess = requests.Session()
    r = sess.post(
        f"{base}/wp-json/daszek/v1/login",
        json={"login": login, "password": password},
        timeout=30,
    )
    data = r.json() if r.content else {}
    if r.status_code >= 400 or not data.get("ok"):
        raise RuntimeError(f"Daszek login failed HTTP {r.status_code}: {data}")
    csrf = str(data.get("csrf_token") or "")
    if not csrf:
        raise RuntimeError("Daszek login missing csrf_token")
    sess.headers["X-CSRF-Token"] = csrf
    return sess


def _seed_hitl_engagement(settings: Any) -> dict[str, Any]:
    url = str(getattr(settings, "mailbox_memory_database_url", "") or os.getenv("MAILBOX_MEMORY_DATABASE_URL", "")).strip()
    if not url:
        raise RuntimeError("MAILBOX_MEMORY_DATABASE_URL required to seed HITL engagement")

    store = PostgresOperatorEngagementStore(url)
    store.bootstrap()
    existing = store.load_snapshot(PROOF_ENGAGEMENT_ID)
    if existing is not None:
        snap = existing
        seeded = False
        if not bool(snap.hitl_gate.required):
            snap = apply_snapshot_delta(
                snap,
                {
                    "hitl_gate": {"required": True, "reason": "local_proof_reset"},
                    "actions": [
                        ActionItem(id="draft_reply", enabled=True, payload_pl="Proof draft reply").model_dump(
                            mode="python"
                        )
                    ],
                },
            )
            store.save_snapshot(snap, expected_version=int(existing.version))
            snap = store.load_snapshot(PROOF_ENGAGEMENT_ID)
            reset = True
        else:
            reset = False
    else:
        snap = build_initial_snapshot(
            case_id=PROOF_CASE_ID,
            engagement_id=PROOF_ENGAGEMENT_ID,
            trace_id=f"proof-{_utc_ts()}",
        )
        snap = apply_snapshot_delta(
            snap,
            {
                "hitl_gate": {"required": True, "reason": "local_proof_seed"},
                "actions": [
                    ActionItem(id="draft_reply", enabled=True, payload_pl="Proof draft reply").model_dump(
                        mode="python"
                    )
                ],
            },
        )
        store.insert_snapshot(snap)
        seeded = True
        reset = False

    return {
        "seeded": seeded,
        "reset_gate": reset if existing is not None else False,
        "engagement_id": PROOF_ENGAGEMENT_ID,
        "case_id": PROOF_CASE_ID,
        "version": int(snap.version),
        "hitl_required": bool(snap.hitl_gate.required),
    }


def _build_proof_feed_snapshot() -> dict[str, Any]:
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    sid = f"proof-133-{_utc_ts()}"
    raw["snapshot_id"] = sid
    raw["generated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    feed = raw.setdefault("feed", {})
    for desk in feed.get("desk") or []:
        desk["engagement_id"] = PROOF_ENGAGEMENT_ID
        desk["case_id"] = PROOF_CASE_ID
        desk["hitl_required"] = True
    for case in feed.get("cases") or []:
        case["engagement_id"] = PROOF_ENGAGEMENT_ID
        case["case_id"] = PROOF_CASE_ID
    details = feed.setdefault("case_details", {})
    key = "agent-case-radlin-1"
    if key in details:
        details[PROOF_CASE_ID] = details.pop(key)
        details[PROOF_CASE_ID]["case_id"] = PROOF_CASE_ID
        details[PROOF_CASE_ID]["engagement_id"] = PROOF_ENGAGEMENT_ID
        details[PROOF_CASE_ID].setdefault("hitl_gate", {"required": True, "reason": "local_proof"})
    return raw


def _post_feed_snapshot(base: str, token: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    r = requests.post(
        f"{base}/wp-json/daszek/v3/operational-feed-snapshots",
        json=snapshot,
        headers={
            "Content-Type": "application/json",
            "X-Daszek-Bridge-Token": token,
            "Referer": f"{base}/daszek/",
        },
        timeout=120,
    )
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise RuntimeError(f"Feed POST failed HTTP {r.status_code}: {data}")
    return data if isinstance(data, dict) else {"raw": data}


def _get_feed_latest(sess: requests.Session, base: str) -> dict[str, Any]:
    r = sess.get(f"{base}/wp-json/daszek/v3/operational-feed-snapshots/latest", timeout=60)
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise RuntimeError(f"Feed GET latest failed HTTP {r.status_code}: {data}")
    return data


def _hitl_approve_via_daszek(sess: requests.Session, base: str) -> dict[str, Any]:
    r = sess.post(
        f"{base}/wp-json/daszek/v2/agent-hitl/approve",
        json={
            "engagement_id": PROOF_ENGAGEMENT_ID,
            "case_id": PROOF_CASE_ID,
            "action_id": "draft_reply",
            "operator_id": "konrad",
        },
        timeout=60,
    )
    data = r.json() if r.content else {}
    if r.status_code >= 400 or not data.get("ok", True):
        raise RuntimeError(f"HITL approve failed HTTP {r.status_code}: {data}")
    return data


def _fetch_os_events_node_b(token: str, engagement_id: str) -> dict[str, Any]:
    r = requests.get(
        f"{_node_b_base()}/engagements/{engagement_id}/os-events",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise RuntimeError(f"Node B os-events GET failed HTTP {r.status_code}: {data}")
    return data if isinstance(data, dict) else {"raw": data}


def _fetch_os_events_daszek(sess: requests.Session, base: str, engagement_id: str) -> dict[str, Any]:
    r = sess.get(
        f"{base}/wp-json/daszek/v3/engagements/{engagement_id}/os-events",
        timeout=30,
    )
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise RuntimeError(f"Daszek os-events proxy failed HTTP {r.status_code}: {data}")
    return data if isinstance(data, dict) else {"raw": data}


def _verify_w0_os_event_projection(
    *,
    approve_payload: dict[str, Any],
    node_b_payload: dict[str, Any],
    daszek_payload: dict[str, Any],
) -> dict[str, Any]:
    os_event_id = str(approve_payload.get("os_event_id") or "").strip()
    items_nb = node_b_payload.get("items") if isinstance(node_b_payload.get("items"), list) else []
    items_dz = daszek_payload.get("items") if isinstance(daszek_payload.get("items"), list) else []

    def _has_approved(items: list[Any]) -> bool:
        return any(
            isinstance(row, dict) and str(row.get("event_type") or "") == "gmail.hitl.approved"
            for row in items
        )

    ok_nb = _has_approved(items_nb)
    ok_dz = _has_approved(items_dz)
    ok_read_only = node_b_payload.get("read_only") is True and daszek_payload.get("read_only") is True
    ok = bool(os_event_id) and ok_nb and ok_dz and ok_read_only
    return {
        "ok": ok,
        "os_event_id": os_event_id,
        "node_b_approved": ok_nb,
        "daszek_approved": ok_dz,
        "read_only_flags": ok_read_only,
        "node_b_count": len(items_nb),
        "daszek_count": len(items_dz),
    }


def _hitl_send_via_daszek(sess: requests.Session, base: str) -> dict[str, Any]:
    r = sess.post(
        f"{base}/wp-json/daszek/v2/agent-hitl/send",
        json={
            "engagement_id": PROOF_ENGAGEMENT_ID,
            "case_id": PROOF_CASE_ID,
            "action_id": "draft_reply",
            "operator_id": "konrad",
        },
        timeout=60,
    )
    data = r.json() if r.content else {}
    if r.status_code >= 400 or not data.get("ok"):
        raise RuntimeError(f"HITL send failed HTTP {r.status_code}: {data}")
    return data


def _operator_snapshot(settings: Any) -> dict[str, Any]:
    url = str(getattr(settings, "mailbox_memory_database_url", "") or os.getenv("MAILBOX_MEMORY_DATABASE_URL", "")).strip()
    store = PostgresOperatorEngagementStore(url)
    snap = store.load_snapshot(PROOF_ENGAGEMENT_ID)
    if snap is None:
        raise RuntimeError(f"Missing operator engagement {PROOF_ENGAGEMENT_ID}")
    return snap.model_dump(mode="python")


def _extract_hitl_execution(drain_payload: dict[str, Any]) -> dict[str, Any] | None:
    for row in drain_payload.get("results") or []:
        if not isinstance(row, dict):
            continue
        bridge_out = row.get("bridge_out") if isinstance(row.get("bridge_out"), dict) else {}
        execution = bridge_out.get("execution")
        if isinstance(execution, dict):
            return execution
    return None


def _drain_bridge(settings: Any) -> dict[str, Any]:
    import argparse
    from io import StringIO
    from contextlib import redirect_stdout

    from daszek_bridge_queue_drain import run_daszek_bridge_drain

    args = argparse.Namespace(
        remote=True,
        queue_path="",
        max_items=25,
        dry_run=False,
        run_id=f"proof-133-{_utc_ts()}",
        domain="agent_hitl",
    )
    buf = StringIO()
    with redirect_stdout(buf):
        rc = run_daszek_bridge_drain(args)
    raw = buf.getvalue().strip()
    try:
        payload = json.loads(raw) if raw else {"raw": raw}
    except json.JSONDecodeError:
        payload = {"raw": raw}
    payload["exit_code"] = rc
    if rc != 0:
        raise RuntimeError(f"Bridge drain failed: {payload}")
    return payload


def _node_b_health(token: str) -> dict[str, Any]:
    r = requests.get(
        f"{_node_b_base()}/health",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    return r.json() if r.content else {}


def _verify_move5_policy(settings: Any) -> dict[str, Any]:
    from daszek_engagement_feed import engagement_feed_source_enabled
    from gmail_intake import daszek_legacy_v2_push_allowed

    feed_on = engagement_feed_source_enabled(settings)
    v2_env = bool(getattr(settings, "daszek_v2_push_enabled", False))
    allowed = daszek_legacy_v2_push_allowed(settings, {"manifest": {"daszek_v2_push_enabled": True}})
    return {
        "engagement_feed_source_enabled": feed_on,
        "daszek_v2_push_enabled_effective": v2_env,
        "daszek_legacy_v2_push_allowed": allowed,
        "ok": (not allowed) if feed_on else True,
    }


def _push_live_feed(settings: Any, sess: requests.Session, base: str) -> dict[str, Any]:
    from daszek_client import DaszekClient
    from daszek_engagement_feed import build_engagement_feed_for_cel, engagement_feed_source_enabled
    from mailbox_memory_runtime import build_mailbox_memory_runtime

    runtime = build_mailbox_memory_runtime(settings)
    if runtime is None or not engagement_feed_source_enabled(settings):
        return {"ok": False, "skipped": True, "reason": "no_runtime_or_feed_source"}

    runtime.bootstrap()
    live = build_engagement_feed_for_cel(runtime.store, settings, case_limit=20)
    client = DaszekClient(settings)
    live_resp = client.post_v3_operational_feed_snapshot(live)
    snapshot_id = str(live.get("snapshot_id") or "")
    latest = _get_feed_latest(sess, base)
    snap = latest.get("snapshot") if isinstance(latest.get("snapshot"), dict) else latest
    feed = (snap.get("feed") or {}) if isinstance(snap, dict) else {}
    cases = [c for c in (feed.get("cases") or []) if isinstance(c, dict)]
    desk = [d for d in (feed.get("desk") or []) if isinstance(d, dict)]
    latest_sid = str(snap.get("snapshot_id") or "") if isinstance(snap, dict) else ""
    case_ids = [str(c.get("case_id") or "") for c in cases if str(c.get("case_id") or "")]
    readback_ok = (
        latest_sid == snapshot_id
        or latest_sid.startswith("eng-feed-")
        or snapshot_id.startswith("eng-feed-")
    ) and len(desk) >= 1 and len(cases) >= 1
    return {
        "ok": readback_ok,
        "snapshot_id": snapshot_id,
        "response_snapshot_id": live_resp.get("snapshot_id"),
        "latest_snapshot_id": latest_sid,
        "feed_source": "engagement_snapshot_v2",
        "desk_count": len(desk),
        "cases_count": len(cases),
        "case_ids_sample": case_ids[:5],
        "known_live_feed_prefix_match": latest_sid.startswith(KNOWN_LIVE_FEED_PREFIX)
        or snapshot_id.startswith(KNOWN_LIVE_FEED_PREFIX),
    }


def _pick_skrzat_case(settings: Any, candidates: list[str]) -> str:
    from mailbox_memory_runtime import build_mailbox_memory_runtime

    preferred = [
        FALLBACK_SKRZAT_CASE,
        "case_df647a3cdf49",
        "case_cc586afac772",
        "case_e651dbd344ac",
    ]
    ordered: list[str] = []
    for cid in preferred + candidates:
        cid = str(cid or "").strip()
        if cid and cid not in ordered and not cid.startswith("case_local_hitl"):
            ordered.append(cid)

    runtime = build_mailbox_memory_runtime(settings)
    if runtime is None:
        return ""
    runtime.bootstrap()
    for cid in ordered:
        pack = runtime.get_context_pack(case_id=cid, query_text="")
        resolved = ""
        if isinstance(pack, dict):
            resolved = str(pack.get("case_id") or "").strip()
        else:
            resolved = str(getattr(pack, "case_id", "") or "").strip()
        if resolved:
            return resolved
    return ""


def _run_skrzat(settings: Any, case_id: str) -> dict[str, Any]:
    from bounded_skrzat_proof import run_skrzat_bounded_proof

    return run_skrzat_bounded_proof(case_id=case_id, settings=settings)


def main() -> int:
    ts = _utc_ts()
    out_dir = TOOL_DIR / "runs" / f"daszek-local-133-proof-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "proof": "daszek_local_133",
        "timestamp_utc": ts,
        "environment": "local_docker",
        "daszek_version": "1.3.3",
        "steps": {},
        "ok": False,
    }

    settings = load_settings(require_groq=False, require_google=False)
    base = _base_url(settings)
    login = str(settings.daszek_login or "konrad").strip()
    password = str(settings.daszek_password or "")
    if not password:
        print("ERROR: DASZEK_PASSWORD required", file=sys.stderr)
        return 2

    token = _token(settings)
    nb_token = _node_b_token(settings)

    try:
        report["steps"]["node_b_health"] = _node_b_health(nb_token)
        report["steps"]["move5_policy"] = _verify_move5_policy(settings)
        if not report["steps"]["move5_policy"].get("ok"):
            raise RuntimeError("Move 5 policy failed: legacy v2 push still allowed with engagement feed")

        sess = _session_login(base, login, password)
        report["steps"]["live_feed_push"] = _push_live_feed(settings, sess, base)
        if not report["steps"]["live_feed_push"].get("ok") and not report["steps"]["live_feed_push"].get("skipped"):
            raise RuntimeError(f"Live feed push/readback failed: {report['steps']['live_feed_push']}")

        live_cases = report["steps"]["live_feed_push"].get("case_ids_sample") or []
        skrzat_case = _pick_skrzat_case(settings, live_cases)
        if not skrzat_case:
            raise RuntimeError(f"No mailbox case for Skrzat proof (candidates={live_cases})")
        report["steps"]["skrzat_bounded"] = _run_skrzat(settings, skrzat_case)
        report["steps"]["skrzat_bounded"]["picked_case_id"] = skrzat_case
        if not report["steps"]["skrzat_bounded"].get("ok"):
            raise RuntimeError(f"Skrzat bounded proof failed: {report['steps']['skrzat_bounded']}")

        report["steps"]["seed_hitl"] = _seed_hitl_engagement(settings)

        snapshot = _build_proof_feed_snapshot()
        report["steps"]["feed_post_fixture"] = _post_feed_snapshot(base, token, snapshot)

        latest = _get_feed_latest(sess, base)
        snap = latest.get("snapshot") if isinstance(latest.get("snapshot"), dict) else latest
        report["steps"]["feed_latest"] = {
            "ok": bool(latest.get("ok")),
            "snapshot_id": snap.get("snapshot_id") if isinstance(snap, dict) else None,
            "desk_count": len((snap.get("feed") or {}).get("desk") or []) if isinstance(snap, dict) else 0,
            "cases_count": len((snap.get("feed") or {}).get("cases") or []) if isinstance(snap, dict) else 0,
            "hitl_desk": any(
                bool(d.get("hitl_required"))
                for d in ((snap.get("feed") or {}).get("desk") or [])
                if str(d.get("engagement_id") or "") == PROOF_ENGAGEMENT_ID
            )
            if isinstance(snap, dict)
            else False,
        }
        if not report["steps"]["feed_latest"]["hitl_desk"]:
            raise RuntimeError("Feed latest missing hitl_required desk row for proof engagement")

        report["steps"]["hitl_approve"] = _hitl_approve_via_daszek(sess, base)
        report["steps"]["os_events_node_b"] = _fetch_os_events_node_b(nb_token, PROOF_ENGAGEMENT_ID)
        report["steps"]["os_events_daszek"] = _fetch_os_events_daszek(sess, base, PROOF_ENGAGEMENT_ID)
        report["steps"]["os_event_w0"] = _verify_w0_os_event_projection(
            approve_payload=report["steps"]["hitl_approve"],
            node_b_payload=report["steps"]["os_events_node_b"],
            daszek_payload=report["steps"]["os_events_daszek"],
        )
        if not report["steps"]["os_event_w0"].get("ok"):
            raise RuntimeError(f"W0 os_event projection failed: {report['steps']['os_event_w0']}")
        nb_after_approve = _operator_snapshot(settings)
        hitl_cleared = not bool((nb_after_approve.get("hitl_gate") or {}).get("required"))
        report["steps"]["operator_store_after_approve"] = {
            "version": nb_after_approve.get("version"),
            "hitl_gate_required": (nb_after_approve.get("hitl_gate") or {}).get("required"),
        }
        if not hitl_cleared:
            raise RuntimeError("Node B hitl_gate still required after approve")

        prev_execute = os.environ.get("AGENT_HITL_EXECUTE_SEND")
        os.environ["AGENT_HITL_EXECUTE_SEND"] = "1"
        try:
            report["steps"]["hitl_send"] = _hitl_send_via_daszek(sess, base)
            report["steps"]["bridge_drain"] = _drain_bridge(settings)
        finally:
            if prev_execute is None:
                os.environ.pop("AGENT_HITL_EXECUTE_SEND", None)
            else:
                os.environ["AGENT_HITL_EXECUTE_SEND"] = prev_execute

        nb_final = _operator_snapshot(settings)
        report["steps"]["operator_store_final"] = {
            "version": nb_final.get("version"),
            "hitl_gate_required": (nb_final.get("hitl_gate") or {}).get("required"),
        }

        execution = _extract_hitl_execution(report["steps"].get("bridge_drain") or {})
        report["steps"]["hitl_execute_send"] = execution
        if not isinstance(execution, dict) or not execution.get("executed"):
            raise RuntimeError(f"HITL execute send not recorded: {execution}")

        report["ok"] = True
        (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": True, "report_dir": str(out_dir), "proof_engagement": PROOF_ENGAGEMENT_ID}, indent=2))
        print("DASZEK_OS_EVENT_W0_PROOF_OK")
        print("DASZEK_LOCAL_133_PROOF_OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
        (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
        print(f"DASZEK_LOCAL_133_PROOF_FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
