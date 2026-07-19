#!/usr/bin/env python3
"""Generate and classify a VPS/operator-shaped Gate B runtime proof pack."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any


CONTAINER_PROOF_DIR = "/app/gate-b-proof"
EXPECTED_IMPORT_SUFFIX = "/app/tools/gmail_audit/gmail_intake.py"


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _safe_read_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.is_file():
        return None, f"missing {path}"
    try:
        return _read_json(path), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid JSON in {path}: {exc}"


def _sha256_token(path: Path) -> str:
    if not path.is_file():
        return ""
    first = path.read_text(encoding="utf-8", errors="replace").strip().split()
    return first[0] if first else ""


def _row(status: str, reasons: list[str], artifacts: list[str]) -> dict[str, Any]:
    return {"status": status, "reasons": reasons, "artifacts": artifacts}


def classify_activation(proof_dir: Path) -> dict[str, Any]:
    activation_dir = proof_dir / "activation"
    host_sha = activation_dir / "host-gmail_intake.sha256"
    container_sha = activation_dir / "container-gmail_intake.sha256"
    host_schema_sha = activation_dir / "host-intake_schema.sha256"
    container_schema_sha = activation_dir / "container-intake_schema.sha256"
    import_path = activation_dir / "runtime-import.json"
    artifacts = [str(host_sha), str(container_sha), str(host_schema_sha), str(container_schema_sha), str(import_path)]
    reasons: list[str] = []

    host_token = _sha256_token(host_sha)
    container_token = _sha256_token(container_sha)
    host_schema_token = _sha256_token(host_schema_sha)
    container_schema_token = _sha256_token(container_schema_sha)
    if not host_token:
        reasons.append("missing host gmail_intake.py sha256")
    if not container_token:
        reasons.append("missing container gmail_intake.py sha256")
    if host_token and container_token and host_token != container_token:
        reasons.append("host/container gmail_intake.py hashes differ")
    if not host_schema_token:
        reasons.append("missing host intake_schema.py sha256")
    if not container_schema_token:
        reasons.append("missing container intake_schema.py sha256")
    if host_schema_token and container_schema_token and host_schema_token != container_schema_token:
        reasons.append("host/container intake_schema.py hashes differ")

    runtime_import, err = _safe_read_json(import_path)
    if err:
        reasons.append(err)
    else:
        import_file = str(runtime_import.get("container_import_path") or "")
        if not import_file.endswith(EXPECTED_IMPORT_SUFFIX):
            reasons.append(f"unexpected container import path: {import_file or 'missing'}")
        if runtime_import.get("artifact_mount_verified") is not True:
            reasons.append("artifact bind mount was not verified inside the worker container")

    return _row("green" if not reasons else "blocked", reasons, artifacts)


def _projection_summary(batch_doc: dict[str, Any]) -> dict[str, Any]:
    summary = batch_doc.get("summary")
    return summary if isinstance(summary, dict) else {}


def _row3_cohort_message_ids(batch_dir: Path) -> set[str] | None:
    path = batch_dir / "selected_message_ids.json"
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return None
    ids: list[Any] | None = None
    if isinstance(doc, list):
        ids = doc
    elif isinstance(doc, dict):
        raw = doc.get("message_ids")
        if isinstance(raw, list):
            ids = raw
    if not isinstance(ids, list):
        return None
    cleaned = {str(mid or "").strip() for mid in ids if str(mid or "").strip()}
    return cleaned or None


def _filter_items_to_cohort(batch_dir: Path, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cohort = _row3_cohort_message_ids(batch_dir)
    if not cohort:
        return items
    scoped: list[dict[str, Any]] = []
    for item in items:
        mid = str(item.get("message_id") or item.get("source_message_id") or "").strip()
        if mid in cohort:
            scoped.append(item)
    return scoped


def _unknown_projection_items(batch_doc: dict[str, Any], *, cohort_ids: set[str] | None = None) -> int:
    items = batch_doc.get("items")
    if not isinstance(items, list):
        return 0
    unknown = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        if cohort_ids is not None:
            mid = str(item.get("message_id") or item.get("source_message_id") or "").strip()
            if mid not in cohort_ids:
                continue
        if str(item.get("policy_status") or "") == "unknown":
            unknown += 1
    return unknown


def _row3_batch_dirs(proof_dir: Path) -> tuple[Path, ...]:
    return (proof_dir / "row3-1", proof_dir / "row3-3", proof_dir / "row3-10")


def _row3_selected_count(batch_dir: Path) -> int | None:
    path = batch_dir / "selected_message_ids.json"
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return None
    ids: list[Any] | None = None
    if isinstance(doc, list):
        ids = doc
    elif isinstance(doc, dict):
        raw = doc.get("message_ids")
        if isinstance(raw, list):
            ids = raw
    if not isinstance(ids, list):
        return None
    cleaned = [str(mid or "").strip() for mid in ids if str(mid or "").strip()]
    return len(cleaned) if cleaned else None


def _row3_cohort_minimums(proof_dir: Path) -> dict[str, int]:
    """Honor pinned --message-id cohorts: do not require 3/10 ids when only one was selected."""
    defaults = {"row3-1": 1, "row3-3": 3, "row3-10": 10}
    adjusted: dict[str, int] = {}
    for name, default_min in defaults.items():
        selected = _row3_selected_count(proof_dir / name)
        if selected is None:
            adjusted[name] = default_min
            continue
        adjusted[name] = min(default_min, selected) if selected > 0 else default_min
    return adjusted


def _projection_items_from_batch(batch_dir: Path, *, cohort_scoped: bool = False) -> list[dict[str, Any]]:
    batch_doc, err = _safe_read_json(batch_dir / "projection_proof_report.batch.json")
    if err or not isinstance(batch_doc, dict):
        return []
    items = batch_doc.get("items")
    rows = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    if cohort_scoped:
        return _filter_items_to_cohort(batch_dir, rows)
    return rows


def _batch_feed_primary(proof_summary: dict[str, Any]) -> bool:
    if str(proof_summary.get("primary_surface_mode") or "") == "feed_first":
        return True
    if int(proof_summary.get("v3_feed_push_ok") or 0) > 0:
        return True
    return str(proof_summary.get("daszek_feed_source") or "") == "engagement_snapshot_v2"


def _proof_dir_feed_primary(proof_dir: Path) -> bool:
    doctor, err = _safe_read_json(proof_dir / "doctor.json")
    if err or not isinstance(doctor, dict):
        return False
    checks = doctor.get("checks") if isinstance(doctor.get("checks"), dict) else {}
    cfg = checks.get("config") if isinstance(checks.get("config"), dict) else {}
    if cfg.get("daszek_operational_feed_auto_push_enabled") and not cfg.get("daszek_v2_push_enabled"):
        eng_feed = checks.get("daszek_engagement_feed")
        if not isinstance(eng_feed, dict):
            eng_feed = doctor.get("daszek_engagement_feed") if isinstance(doctor.get("daszek_engagement_feed"), dict) else {}
        if eng_feed.get("engagement_feed_enabled"):
            return True
        agent = checks.get("agent_runtime") if isinstance(checks.get("agent_runtime"), dict) else {}
        feed_source = str(
            (agent.get("primary_cutover") or {}).get("legacy_feed_env")
            or agent.get("legacy_feed_env")
            or ""
        ).strip()
        if feed_source == "engagement_snapshot_v2":
            return True
    return False


def _any_feed_primary_batch(proof_dir: Path) -> bool:
    if _proof_dir_feed_primary(proof_dir):
        return True
    for batch_dir in _row3_batch_dirs(proof_dir):
        batch_doc, err = _safe_read_json(batch_dir / "projection_proof_report.batch.json")
        if err or not isinstance(batch_doc, dict):
            continue
        if _batch_feed_primary(_projection_summary(batch_doc)):
            return True
    return False


def _is_legacy_adjudication_handoff_item(item: dict[str, Any]) -> bool:
    actions = item.get("allowed_operator_actions")
    action_values = {str(action or "").strip() for action in actions} if isinstance(actions, list) else set()
    return (
        bool(item.get("handoff_actionable"))
        and str(item.get("policy_status") or "") == "accepted_projection"
        and str(item.get("store_readback") or "") == "found"
        and bool(str(item.get("source_message_id") or "").strip())
        and bool(str(item.get("signal_id") or "").strip())
        and bool(str(item.get("case_id") or "").strip())
        and bool(str(item.get("note_id") or "").strip())
        and bool(str(item.get("title") or "").strip())
        and bool(item.get("operator_action_available"))
        and "zla_sprawa" in action_values
        and str(item.get("expected_bridge_domain") or "") == "adjudication"
        and str(item.get("expected_adjudication_kind") or "") == "reject_same_case"
    )


def _is_feed_handoff_item(item: dict[str, Any]) -> bool:
    has_business_anchor = bool(str(item.get("case_id") or "").strip()) or bool(str(item.get("engagement_id") or "").strip())
    return (
        bool(item.get("feed_handoff_actionable"))
        and str(item.get("policy_status") or "") == "accepted_projection"
        and str(item.get("surface") or "") == "v3_operational_feed"
        and bool(str(item.get("source_message_id") or "").strip())
        and bool(str(item.get("signal_id") or "").strip())
        and has_business_anchor
        and bool(str(item.get("snapshot_id") or "").strip())
        and bool(str(item.get("title") or "").strip())
    )


def _is_actionable_handoff_item(item: dict[str, Any]) -> bool:
    return _is_feed_handoff_item(item) or _is_legacy_adjudication_handoff_item(item)


def _collect_handoff_items(proof_dir: Path, *, predicate) -> list[dict[str, Any]]:
    actionable: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for batch_dir in _row3_batch_dirs(proof_dir):
        for item in _projection_items_from_batch(batch_dir):
            if not predicate(item):
                continue
            key = (
                str(item.get("message_id") or ""),
                str(item.get("snapshot_id") or item.get("note_id") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            clone = dict(item)
            clone["batch_dir"] = str(batch_dir)
            actionable.append(clone)
    return actionable


def _feed_handoff_items(proof_dir: Path) -> list[dict[str, Any]]:
    return _collect_handoff_items(proof_dir, predicate=_is_feed_handoff_item)


def _legacy_adjudication_handoff_items(proof_dir: Path) -> list[dict[str, Any]]:
    return _collect_handoff_items(proof_dir, predicate=_is_legacy_adjudication_handoff_item)


def _actionable_row3_handoff_items(proof_dir: Path) -> list[dict[str, Any]]:
    feed_items = _feed_handoff_items(proof_dir)
    if feed_items:
        return feed_items
    return _legacy_adjudication_handoff_items(proof_dir)


def _classify_row3_batch(batch_dir: Path, *, min_requested: int, label: str) -> tuple[list[str], list[str], list[str]]:
    summary_path = batch_dir / "sequential_summary.json"
    proof_path = batch_dir / "projection_proof_report.batch.json"
    artifacts = [str(summary_path), str(proof_path)]
    hard_reasons: list[str] = []
    soft_reasons: list[str] = []

    summary, err = _safe_read_json(summary_path)
    if err:
        hard_reasons.append(err)
        summary = {}
    requested = int(summary.get("requested_count") or 0)
    attempted = int(summary.get("attempted_count") or 0)
    failed = int(summary.get("failed_count") or 0)
    if requested < min_requested:
        hard_reasons.append(f"{label}: requested_count {requested} < {min_requested}")
    if attempted < min_requested:
        hard_reasons.append(f"{label}: attempted_count {attempted} < {min_requested}")
    if failed > 0:
        hard_reasons.append(f"{label}: failed_count {failed} > 0")

    batch_doc, err = _safe_read_json(proof_path)
    if err:
        hard_reasons.append(err)
        batch_doc = {}
    proof_summary = _projection_summary(batch_doc)
    feed_primary = _batch_feed_primary(proof_summary)
    accepted = int(proof_summary.get("v2_projection_accepted") or proof_summary.get("operator_projection_accepted") or 0)
    readback = int(proof_summary.get("v2_readback_found") or proof_summary.get("readback_found") or 0)
    v3_ok = int(proof_summary.get("v3_feed_push_ok") or 0)
    cohort_ids = _row3_cohort_message_ids(batch_dir)
    unknown = _unknown_projection_items(batch_doc, cohort_ids=cohort_ids)
    if feed_primary:
        if v3_ok <= 0:
            soft_reasons.append(f"{label}: v3_feed_push_ok is not positive")
    else:
        if accepted <= 0:
            soft_reasons.append(f"{label}: v2_projection_accepted is not positive")
        if readback <= 0:
            soft_reasons.append(f"{label}: v2_readback_found is not positive")
    if unknown > 0:
        scope = "cohort-scoped " if cohort_ids else ""
        soft_reasons.append(f"{label}: projection proof contains {unknown} {scope}unknown item(s)")

    return hard_reasons, soft_reasons, artifacts


def classify_row3(proof_dir: Path) -> dict[str, Any]:
    """Row3 = transport proof (v3 feed push), not operator handoff (see row4a)."""
    hard_reasons: list[str] = []
    soft_reasons: list[str] = []
    artifacts: list[str] = []
    minimums = _row3_cohort_minimums(proof_dir)
    for name in ("row3-1", "row3-3", "row3-10"):
        minimum = int(minimums.get(name) or 1)
        batch_hard, batch_soft, batch_artifacts = _classify_row3_batch(
            proof_dir / name,
            min_requested=minimum,
            label=name,
        )
        hard_reasons.extend(batch_hard)
        soft_reasons.extend(batch_soft)
        artifacts.extend(batch_artifacts)
    if hard_reasons:
        status = "blocked"
        reasons = hard_reasons + soft_reasons
    elif soft_reasons:
        status = "yellow"
        reasons = soft_reasons
    else:
        status = "green"
        reasons = []
    return _row(status, reasons, artifacts)


def _handoff_blockers(item: dict[str, Any]) -> list[str]:
    if _is_feed_handoff_item(item):
        checks = {
            "accepted v3 feed projection": str(item.get("policy_status") or "") == "accepted_projection",
            "source_message_id": bool(str(item.get("source_message_id") or "").strip()),
            "signal_id": bool(str(item.get("signal_id") or "").strip()),
            "case_id or engagement_id": bool(str(item.get("case_id") or "").strip())
            or bool(str(item.get("engagement_id") or "").strip()),
            "snapshot_id": bool(str(item.get("snapshot_id") or "").strip()),
            "title": bool(str(item.get("title") or "").strip()),
            "feed_handoff_actionable flag": bool(item.get("feed_handoff_actionable")),
        }
        return [name for name, ok in checks.items() if not ok]
    checks = {
        "accepted v2 projection": str(item.get("policy_status") or "") == "accepted_projection",
        "readback found": str(item.get("store_readback") or "") == "found",
        "source_message_id": bool(str(item.get("source_message_id") or "").strip()),
        "signal_id": bool(str(item.get("signal_id") or "").strip()),
        "case_id": bool(str(item.get("case_id") or "").strip()),
        "note_id": bool(str(item.get("note_id") or "").strip()),
        "title": bool(str(item.get("title") or "").strip()),
        "operator_action_available": bool(item.get("operator_action_available")),
        "handoff_actionable flag": bool(item.get("handoff_actionable")),
    }
    return [name for name, ok in checks.items() if not ok]


def build_operator_handoff(proof_dir: Path) -> dict[str, Any]:
    proof_dir = proof_dir.expanduser().resolve()
    feed_items = _feed_handoff_items(proof_dir)
    if feed_items:
        return {
            "actionable": True,
            "handoff_tier": "row4a",
            "proof_dir": str(proof_dir),
            "item": feed_items[0],
            "candidates_checked": sum(len(_projection_items_from_batch(batch_dir)) for batch_dir in _row3_batch_dirs(proof_dir)),
            "blockers": [],
        }
    legacy_items = _legacy_adjudication_handoff_items(proof_dir)
    if legacy_items:
        return {
            "actionable": True,
            "handoff_tier": "row4b",
            "proof_dir": str(proof_dir),
            "item": legacy_items[0],
            "candidates_checked": sum(len(_projection_items_from_batch(batch_dir)) for batch_dir in _row3_batch_dirs(proof_dir)),
            "blockers": [],
        }

    candidates: list[dict[str, Any]] = []
    for batch_dir in _row3_batch_dirs(proof_dir):
        for item in _projection_items_from_batch(batch_dir):
            if str(item.get("policy_status") or "") == "accepted_projection":
                clone = dict(item)
                clone["batch_dir"] = str(batch_dir)
                clone["blockers"] = _handoff_blockers(item)
                candidates.append(clone)
    return {
        "actionable": False,
        "proof_dir": str(proof_dir),
        "item": {},
        "candidates_checked": sum(len(_projection_items_from_batch(batch_dir)) for batch_dir in _row3_batch_dirs(proof_dir)),
        "blockers": ["no feed or legacy adjudication handoff anchor"],
        "candidates": candidates[:10],
    }


def render_operator_handoff_text(handoff: dict[str, Any]) -> str:
    proof_dir = str(handoff.get("proof_dir") or "")
    tier = str(handoff.get("handoff_tier") or "")
    if handoff.get("actionable") is not True:
        lines = [
            "Gate B - Row 4 operator handoff",
            "",
            "status: handoff not actionable",
            f"proof_dir: {proof_dir}",
            "",
            "Do not run Row 4.",
            "Do not ask the operator for a Daszek click yet.",
            "",
            "blockers:",
        ]
        for blocker in handoff.get("blockers") or []:
            lines.append(f"- {blocker}")
        candidates = handoff.get("candidates") if isinstance(handoff.get("candidates"), list) else []
        if candidates:
            lines.extend(["", "accepted candidates checked:"])
            for item in candidates[:5]:
                lines.append(
                    "- "
                    f"message_id={item.get('message_id') or ''} "
                    f"note_id={item.get('note_id') or ''} "
                    f"case_id={item.get('case_id') or ''} "
                    f"title={item.get('title') or ''} "
                    f"blockers={','.join(item.get('blockers') or [])}"
                )
        return "\n".join(lines) + "\n"

    item = handoff.get("item") if isinstance(handoff.get("item"), dict) else {}
    if tier == "row4a":
        case_id = str(item.get("case_id") or "").strip()
        engagement_id = str(item.get("engagement_id") or "").strip()
        handoff_mode = str(item.get("feed_handoff_mode") or ("case_ready" if case_id else "staging")).strip()
        locate_lines = []
        if case_id:
            locate_lines.append(f"Find case card by case_id: {case_id}")
        if engagement_id:
            locate_lines.append(f"Find desk item note_id: desk-{engagement_id}")
        if not locate_lines:
            locate_lines.append("Find visible card/title in operational feed")
        return "\n".join(
            [
                "Gate B - Row 4a operator handoff (feed-primary)",
                "",
                "status: actionable",
                f"proof_dir: {proof_dir}",
                f"handoff_mode: {handoff_mode}",
                "",
                "Open Daszek Cockpit / operational feed.",
                *locate_lines,
                f"snapshot_id: {item.get('snapshot_id') or ''}",
                f"engagement_id: {engagement_id}",
                f"Find visible card/title: {item.get('title') or ''}",
                f"source_message_id: {item.get('source_message_id') or item.get('message_id') or ''}",
                f"signal_id: {item.get('signal_id') or ''}",
                "",
                "Row4a verifies feed visibility only. Row4b adjudication (zla_sprawa) requires legacy note_* in WP storage.",
                "",
                "After visual confirmation, reply in chat: Row 4a ready.",
            ]
        ) + "\n"
    action = "zla_sprawa"
    return "\n".join(
        [
            "Gate B - Row 4b operator handoff (legacy adjudication)",
            "",
            "status: actionable",
            f"proof_dir: {proof_dir}",
            "",
            "Open Daszek.",
            "Go to: Biurko or Cockpit V3 / Sprawy z najbogatszym kontekstem.",
            f"Find visible card/title: {item.get('title') or ''}",
            f"note_id: {item.get('note_id') or ''}",
            f"case_id: {item.get('case_id') or ''}",
            f"source_message_id: {item.get('source_message_id') or item.get('message_id') or ''}",
            f"signal_id: {item.get('signal_id') or ''}",
            f"Click allowed action: {action}",
            "Expected bridge result: domain=adjudication",
            "Expected bridge result: adjudication_kind=reject_same_case",
            "",
            "After the click, reply in chat: Row 4 ready.",
        ]
    ) + "\n"


def _bridge_out_is_reconciled(out: dict[str, Any]) -> bool:
    summary = out.get("reconcile_summary")
    state = summary.get("processing_state") if isinstance(summary, dict) else ""
    return (
        out.get("truth_loop_executed") is True
        and out.get("reconcile_signal_ran") is True
        and str(state or "") == "reconciled"
    )


def classify_row4a(proof_dir: Path) -> dict[str, Any]:
    items = _feed_handoff_items(proof_dir)
    artifacts = [str(proof_dir / name / "projection_proof_report.batch.json") for name in ("row3-1", "row3-3", "row3-10")]
    if items:
        return _row("green", [], artifacts)
    if _any_feed_primary_batch(proof_dir):
        return _row(
            "blocked",
            [
                "row4a: feed-primary proof missing Row4a handoff anchor "
                "(source_message_id, signal_id, snapshot_id, title, and case_id or engagement_id)"
            ],
            artifacts,
        )
    return _row("green", ["row4a: not required in legacy v2 proof mode"], artifacts)


def _is_row3_stop_phase(proof_dir: Path) -> bool:
    """Row3-stop leaves row4 dry-run absent until operator continues."""
    return not (proof_dir / "row4" / "dry-run.json").is_file()


def classify_row4b(proof_dir: Path) -> dict[str, Any]:
    row4_dir = proof_dir / "row4"
    dry_path = row4_dir / "dry-run.json"
    drain_path = row4_dir / "drain.json"
    artifacts = [str(dry_path), str(drain_path)]
    reasons: list[str] = []

    dry, err = _safe_read_json(dry_path)
    if err:
        reasons.append(err)
        dry = {}
    items = dry.get("items")
    if dry.get("ok") is not True or dry.get("dry_run") is not True:
        reasons.append("row4 dry-run did not return ok=true and dry_run=true")
    if not isinstance(items, list) or not items:
        reasons.append("row4 dry-run did not show a real pending item")

    drain, err = _safe_read_json(drain_path)
    if err:
        reasons.append(err)
        drain = {}
    results = drain.get("results")
    if drain.get("ok") is not True:
        reasons.append("row4 drain did not return ok=true")
    if not isinstance(results, list) or not results:
        reasons.append("row4 drain did not process any item")
    else:
        failed = [r for r in results if isinstance(r, dict) and r.get("ok") is False]
        if failed:
            reasons.append(f"row4 drain has {len(failed)} failed result(s)")
        reconciled = 0
        for result in results:
            if not isinstance(result, dict):
                continue
            bridge_out = result.get("bridge_out")
            if isinstance(bridge_out, dict) and _bridge_out_is_reconciled(bridge_out):
                reconciled += 1
        if reconciled <= 0:
            reasons.append("row4 drain did not prove truth_loop + reconcile to reconciled state")

    return _row("green" if not reasons else "blocked", reasons, artifacts)


def classify_row4(proof_dir: Path) -> dict[str, Any]:
    """Backward-compatible alias for Row4b adjudication drain proof."""
    return classify_row4b(proof_dir)


def classify_gate_b_artifacts(proof_dir: Path) -> dict[str, Any]:
    proof_dir = proof_dir.expanduser().resolve()
    row4b = classify_row4b(proof_dir)
    rows = {
        "activation": classify_activation(proof_dir),
        "row3": classify_row3(proof_dir),
        "row4a": classify_row4a(proof_dir),
        "row4b": row4b,
        "row4": row4b,
    }
    activation_status = str(rows["activation"].get("status") or "")
    row3_status = str(rows["row3"].get("status") or "")
    row4a_status = str(rows["row4a"].get("status") or "")
    row4b_status = str(rows["row4b"].get("status") or "")
    row3_stop = _is_row3_stop_phase(proof_dir)
    core_green = (
        activation_status == "green"
        and row3_status == "green"
        and row4a_status == "green"
        and row4b_status == "green"
    )
    if core_green:
        status = "green"
        gate = "Gate B"
    elif activation_status == "green" and row3_status == "green" and row3_stop:
        status = "green"
        gate = "Gate B row3-stop transport proof (row4a/row4b pending)"
    elif activation_status == "green" and row3_status in {"green", "yellow"}:
        if row3_status == "yellow" or row4a_status != "green" or row4b_status != "green":
            status = "yellow"
            gate = "Gate B bounded proof (transport or operator handoff partial)"
        else:
            status = "green"
            gate = "Gate B"
    else:
        status = "blocked"
        gate = "Gate B blocked"
    return {
        "status": status,
        "proof_dir": str(proof_dir),
        "rows": rows,
        "gate": gate,
    }


def render_status_markdown(status: dict[str, Any]) -> str:
    lines = [
        f"status: {status.get('status')}",
        f"gate: {status.get('gate')}",
        f"proof_dir: {status.get('proof_dir')}",
        "",
        "rows:",
    ]
    rows = status.get("rows") if isinstance(status.get("rows"), dict) else {}
    for name, row in rows.items():
        lines.append(f"- {name}: {row.get('status')}")
        reasons = row.get("reasons") if isinstance(row.get("reasons"), list) else []
        for reason in reasons:
            lines.append(f"  reason: {reason}")
        artifacts = row.get("artifacts") if isinstance(row.get("artifacts"), list) else []
        for artifact in artifacts:
            lines.append(f"  artifact: {artifact}")
    return "\n".join(lines) + "\n"


def _row3_exclude_args(message_ids: list[str] | tuple[str, ...] | None) -> str:
    seen: set[str] = set()
    args: list[str] = []
    for raw in message_ids or []:
        mid = str(raw or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        args.append(f"--exclude-message-id {shlex.quote(mid)}")
    return (" " + " ".join(args)) if args else ""


def _row3_message_args(message_ids: list[str] | tuple[str, ...] | None) -> str:
    seen: set[str] = set()
    args: list[str] = []
    for raw in message_ids or []:
        mid = str(raw or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        args.append(f"--message-id {shlex.quote(mid)}")
    return (" " + " ".join(args)) if args else ""


def render_vps_runner_script(
    *,
    proof_dir: str,
    env_file: str,
    compose_file: str,
    service: str,
    phase: str = "all",
    row3_exclude_message_ids: list[str] | tuple[str, ...] | None = None,
    row3_message_ids: list[str] | tuple[str, ...] | None = None,
) -> str:
    """phase: all | row3-stop | row4-only ÔÇö split for chat-handoff before Row 4 (Daszek UI)."""
    if phase not in ("all", "row3-stop", "row4-only"):
        raise ValueError(f"invalid phase: {phase!r}")

    row3_excludes = _row3_exclude_args(row3_exclude_message_ids)
    row3_messages = _row3_message_args(row3_message_ids)

    return f"""#!/usr/bin/env bash
set -euo pipefail

ROOT="${{ROOT:-$(pwd)}}"
cd "$ROOT"

HOST_PYTHON="${{HOST_PYTHON:-python3}}"

ENV_FILE="${{ENV_FILE:-{env_file}}}"
COMPOSE_FILE="${{COMPOSE_FILE:-{compose_file}}}"
SERVICE="${{SERVICE:-{service}}}"
PROOF_DIR="${{PROOF_DIR:-{proof_dir}}}"
GATE_B_PHASE="{phase}"

mkdir -p "$PROOF_DIR"/activation "$PROOF_DIR"/logs "$PROOF_DIR"/row3-1 "$PROOF_DIR"/row3-3 "$PROOF_DIR"/row3-10 "$PROOF_DIR"/row4
# Docker rejects relative bind sources on some hosts ("invalid characters for a local volume name").
PROOF_DIR=$(cd "$PROOF_DIR" && pwd)
# Git Bash / MSYS: Docker Desktop on Windows needs a drive-letter path for bind mounts.
if command -v uname >/dev/null 2>&1; then
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*)
      if PROOF_DIR_WIN=$(cd "$PROOF_DIR" && pwd -W 2>/dev/null); then
        PROOF_DIR="${{PROOF_DIR_WIN//\\\\//}}"
      fi
      ;;
  esac
fi

classify_on_exit() {{
  set +e
  "$HOST_PYTHON" scripts/gate_b_runtime_proof.py classify --proof-dir "$PROOF_DIR" >/dev/null 2>&1
}}
trap classify_on_exit EXIT

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 2
fi
if [[ ! -f tools/gmail_audit/.env ]]; then
  echo "missing tools/gmail_audit/.env" >&2
  exit 2
fi

compose=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")

run_worker() {{
  "${{compose[@]}}" --profile worker run --rm \
    -e "LLM_BACKEND=${{LLM_BACKEND:-groq}}" \
    -e "LLM_PRIMARY_PROVIDER=${{LLM_PRIMARY_PROVIDER:-groq}}" \
    -e "LLM_FALLBACK_PROVIDERS=${{LLM_FALLBACK_PROVIDERS:-cerebras,nvidia,openrouter}}" \
    -e "LLM_STRUCTURED_PROVIDER_ALTERNATION=${{LLM_STRUCTURED_PROVIDER_ALTERNATION:-1}}" \
    -e "CASE_OS_RUNTIME_PROFILE=${{CASE_OS_RUNTIME_PROFILE:-full}}" \
    -e "INTAKE_LLM_BEFORE_SIGNAL=${{INTAKE_LLM_BEFORE_SIGNAL:-1}}" \
    -e "DOCLING_ENABLED=${{DOCLING_ENABLED:-0}}" \
    -e "ATTACHMENT_EXTRACTION_ENABLED=${{ATTACHMENT_EXTRACTION_ENABLED:-0}}" \
    -v "$PROOF_DIR:/app/gate-b-proof:rw" "$SERVICE" "$@"
}}

if [[ "$GATE_B_PHASE" != "row4-only" ]]; then
echo "[activation] rebuild and recreate image-baked worker"
"${{compose[@]}}" --profile worker up -d --build --force-recreate "$SERVICE" 2>&1 | tee "$PROOF_DIR/logs/activation-recreate.log"

echo "[activation] host/container hash and import proof"
sha256sum tools/gmail_audit/gmail_intake.py | tee "$PROOF_DIR/activation/host-gmail_intake.sha256"
sha256sum tools/gmail_audit/intake_schema.py | tee "$PROOF_DIR/activation/host-intake_schema.sha256"
run_worker sh -c 'set -eu
sha256sum /app/tools/gmail_audit/gmail_intake.py > /app/gate-b-proof/activation/container-gmail_intake.sha256
sha256sum /app/tools/gmail_audit/intake_schema.py > /app/gate-b-proof/activation/container-intake_schema.sha256
python - <<'"'"'PY'"'"'
import json
import sys
from pathlib import Path

# Match normal `python tools/gmail_audit/gmail_intake.py ÔÇŽ` semantics (flat gmail_audit imports).
sys.path.insert(0, "/app/tools/gmail_audit")
import gmail_intake as gi

payload = {{
    "container_import_path": gi.__file__,
    "artifact_mount_verified": Path("/app/gate-b-proof/activation").is_dir(),
}}
Path("/app/gate-b-proof/activation/runtime-import.json").write_text(json.dumps(payload, indent=2) + "\\n", encoding="utf-8")
PY'

echo "[preflight] doctor with Gmail, Drive, Daszek, v3 feed latest"
# When doctor exits non-zero (e.g. vector_retrieval degraded), set GATE_B_DOCTOR_NONFATAL=1 to continue Row 3 anyway.
run_worker python tools/gmail_audit/gmail_intake.py doctor --gmail-source google_api --check-drive --check-daszek --check-daszek-v3-feed --verbose > "$PROOF_DIR/doctor.json" 2> "$PROOF_DIR/logs/doctor.stderr.log" || [[ "${{GATE_B_DOCTOR_NONFATAL:-0}}" == "1" ]]

ROW3_RL=(--max-retries-per-message 5 --retry-base-delay 45 --retry-max-delay 600)
ROW3_FORCE=()
if [[ "${{GATE_B_ROW3_FORCE:-0}}" == "1" ]]; then
  ROW3_FORCE=(--force)
  echo "[row3] force mode: clearing stale batch artifacts" >&2
  for _rb in row3-1 row3-3 row3-10; do
    rm -f "$PROOF_DIR/$_rb"/sequential_summary.json \
      "$PROOF_DIR/$_rb"/projection_proof_report.batch.json \
      "$PROOF_DIR/$_rb"/selected_message_ids.json \
      "$PROOF_DIR/$_rb"/child_runs_index.jsonl \
      "$PROOF_DIR/$_rb"/child_summaries.jsonl \
      "$PROOF_DIR/$_rb"/failed_items.jsonl 2>/dev/null || true
  done
fi

echo "[row3] resolve curated cohort (mailbox_memory business candidates)"
ROW3_COHORT_ARGS=""
if [[ -n "${{GATE_B_ROW3_MESSAGE_IDS:-}}" ]]; then
  export GATE_B_ROW3_MESSAGE_IDS
  ROW3_COHORT_ARGS=$("$HOST_PYTHON" -c 'import os; ids=[x.strip() for x in os.environ.get("GATE_B_ROW3_MESSAGE_IDS","").replace(";", ",").split(",") if x.strip()]; print(" ".join(f"--message-id {{mid}}" for mid in ids))')
elif [[ -n "${{GATE_B_ROW3_COHORT_FILE:-}}" && -f "${{GATE_B_ROW3_COHORT_FILE}}" ]]; then
  export GATE_B_ROW3_COHORT_FILE
  ROW3_COHORT_ARGS=$("$HOST_PYTHON" -c 'import json, os; from pathlib import Path; d=json.loads(Path(os.environ["GATE_B_ROW3_COHORT_FILE"]).read_text(encoding="utf-8")); ids=d.get("message_ids") or []; print(" ".join(f"--message-id {{mid}}" for mid in ids))')
else
  MAILBOX_MEMORY_DATABASE_URL="${{MAILBOX_MEMORY_DATABASE_URL:-postgresql://mailbox_memory:memorka@127.0.0.1:54129/mailbox_memory}}" \\
    "$HOST_PYTHON" scripts/pick_gate_b_row3_cohort.py --limit 10 --output "$PROOF_DIR/row3_cohort.json"
  export PROOF_DIR
  ROW3_COHORT_ARGS=$("$HOST_PYTHON" scripts/gate_b_runtime_proof.py row3-cohort-args --proof-dir "$PROOF_DIR")
fi
if [[ -z "$ROW3_COHORT_ARGS" ]]; then
  echo "[row3] WARN: no curated cohort message ids; falling back to Gmail newer-than search" >&2
else
  echo "[row3] cohort cli args ready" >&2
fi

echo "[row3] one-message proof"
run_worker python scripts/sequential_gmail_ingress_daszek.py --newer-than 14d --limit 1 --delay 0 "${{ROW3_RL[@]}}" "${{ROW3_FORCE[@]}}" $ROW3_COHORT_ARGS{row3_messages}{row3_excludes} --push-daszek --projection-proof --keep-going --verbose --batch-dir /app/gate-b-proof/row3-1 2>&1 | tee "$PROOF_DIR/logs/row3-1.log" || [[ "${{GATE_B_ROW3_SEQUENTIAL_NONFATAL:-0}}" == "1" ]]

echo "[row3] three-message mini-cohort"
run_worker python scripts/sequential_gmail_ingress_daszek.py --newer-than 14d --limit 3 --delay 45 "${{ROW3_RL[@]}}" "${{ROW3_FORCE[@]}}" $ROW3_COHORT_ARGS{row3_messages}{row3_excludes} --push-daszek --projection-proof --keep-going --verbose --batch-dir /app/gate-b-proof/row3-3 2>&1 | tee "$PROOF_DIR/logs/row3-3.log" || [[ "${{GATE_B_ROW3_SEQUENTIAL_NONFATAL:-0}}" == "1" ]]

echo "[row3] ten-message proof"
run_worker python scripts/sequential_gmail_ingress_daszek.py --newer-than 14d --limit 10 --delay 45 "${{ROW3_RL[@]}}" "${{ROW3_FORCE[@]}}" $ROW3_COHORT_ARGS{row3_messages}{row3_excludes} --push-daszek --projection-proof --keep-going --verbose --batch-dir /app/gate-b-proof/row3-10 2>&1 | tee "$PROOF_DIR/logs/row3-10.log" || [[ "${{GATE_B_ROW3_SEQUENTIAL_NONFATAL:-0}}" == "1" ]]
fi

if [[ "$GATE_B_PHASE" == "row3-stop" ]]; then
  HANDOFF="$PROOF_DIR/OPERATOR_ROW4_HANDOFF.txt"
  "$HOST_PYTHON" scripts/gate_b_runtime_proof.py write-handoff --proof-dir "$PROOF_DIR" --output "$HANDOFF" || true
  echo ""
  echo "================================================================================"
  echo "[gate-b] PHASE=row3-stop COMPLETE ÔÇö STOP before Row 4"
  echo "[gate-b] Handoff file: $HANDOFF"
  if grep -q '^status: actionable' "$HANDOFF"; then
    echo "[gate-b] Operator handoff is actionable. Give the operator the handoff file instructions."
  else
    echo "[gate-b] Operator handoff is NOT actionable. Do not run Row 4."
  fi
  echo "================================================================================"
  echo ""
  trap - EXIT
  echo "[status] classify artifacts (interim ÔÇö row4 expected blocked)"
  "$HOST_PYTHON" scripts/gate_b_runtime_proof.py classify --proof-dir "$PROOF_DIR" || true
  exit 0
fi

if [[ "$GATE_B_PHASE" == "all" ]]; then
if [[ "${{GATE_B_SKIP_OPERATOR_PAUSE:-0}}" != "1" ]]; then
  echo "[operator] create one real Row 4 pending decision in Daszek from the Row 3 projection, then press Enter"
  read -r
fi
fi

if [[ "$GATE_B_PHASE" == "all" || "$GATE_B_PHASE" == "row4-only" ]]; then
echo "[row4] dry-run bridge drain must show real pending"
run_worker python tools/gmail_audit/gmail_intake.py daszek-bridge-drain --remote --domain adjudication --dry-run --max-items 1 > "$PROOF_DIR/row4/dry-run.json" 2> "$PROOF_DIR/logs/row4-dry-run.stderr.log"

echo "[row4] real bridge drain"
run_worker python tools/gmail_audit/gmail_intake.py daszek-bridge-drain --remote --domain adjudication --max-items 1 --run-id "gate-b-row4-$(date -u +%Y%m%dT%H%M%SZ)" > "$PROOF_DIR/row4/drain.json" 2> "$PROOF_DIR/logs/row4-drain.stderr.log"
fi

echo "[status] classify artifacts"
trap - EXIT
"$HOST_PYTHON" scripts/gate_b_runtime_proof.py classify --proof-dir "$PROOF_DIR"
"""


def write_vps_runner_script(
    path: Path,
    *,
    proof_dir: str,
    env_file: str,
    compose_file: str,
    service: str,
    phase: str = "all",
    row3_exclude_message_ids: list[str] | tuple[str, ...] | None = None,
    row3_message_ids: list[str] | tuple[str, ...] | None = None,
) -> Path:
    script = render_vps_runner_script(
        proof_dir=proof_dir,
        env_file=env_file,
        compose_file=compose_file,
        service=service,
        phase=phase,
        row3_exclude_message_ids=row3_exclude_message_ids,
        row3_message_ids=row3_message_ids,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script, encoding="utf-8", newline="\n")
    return path


def format_row3_cohort_cli_args(proof_dir: Path) -> str:
    """Print `--message-id` args from row3_cohort.json for bash runners."""
    path = proof_dir.expanduser().resolve() / "row3_cohort.json"
    if not path.is_file():
        return ""
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return ""
    ids = doc.get("message_ids") if isinstance(doc, dict) else []
    if not isinstance(ids, list):
        return ""
    out: list[str] = []
    seen: set[str] = set()
    for raw in ids:
        mid = str(raw or "").strip()
        if mid and mid not in seen:
            seen.add(mid)
            out.append(f"--message-id {shlex.quote(mid)}")
    return " ".join(out)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render-script", help="Render a VPS bash runner for Gate B Row 3/4 proof.")
    render.add_argument("--proof-dir", default="runs/gate-b-proof-$(date -u +%Y%m%dT%H%M%SZ)")
    render.add_argument("--env-file", default=".env.vps")
    render.add_argument("--compose-file", default="docker-compose.vps.yml")
    render.add_argument("--service", default="gmail-agent-worker")
    render.add_argument("--output", type=Path, default=None, help="Optional path to write the script.")
    render.add_argument(
        "--phase",
        choices=("all", "row3-stop", "row4-only"),
        default="all",
        help="all=single run; row3-stop=stop after Row 3 for Daszek UI handoff; row4-only=drain+classify using existing PROOF_DIR.",
    )
    render.add_argument(
        "--row3-exclude-message-id",
        action="append",
        default=[],
        metavar="MESSAGE_ID",
        help="Gmail message id to exclude from all Row 3 cohorts; repeatable.",
    )
    render.add_argument(
        "--row3-message-id",
        action="append",
        default=[],
        metavar="MESSAGE_ID",
        help="Explicit Gmail message id for Row 3 cohorts; repeatable. The 1/3/10 cohorts use the first N ids.",
    )

    classify = sub.add_parser("classify", help="Classify an existing proof directory and write STATUS.md.")
    classify.add_argument("--proof-dir", type=Path, required=True)
    classify.add_argument("--json", action="store_true", help="Print JSON instead of Markdown.")

    handoff = sub.add_parser("write-handoff", help="Write OPERATOR_ROW4_HANDOFF artifacts for Row 3.")
    handoff.add_argument("--proof-dir", type=Path, required=True)
    handoff.add_argument("--output", type=Path, required=True)
    handoff.add_argument("--json-output", type=Path, default=None)

    cohort_args = sub.add_parser("row3-cohort-args", help="Print --message-id args from row3_cohort.json.")
    cohort_args.add_argument("--proof-dir", type=Path, required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.command == "render-script":
        script = render_vps_runner_script(
            proof_dir=str(args.proof_dir),
            env_file=str(args.env_file),
            compose_file=str(args.compose_file),
            service=str(args.service),
            phase=str(args.phase),
            row3_exclude_message_ids=list(args.row3_exclude_message_id or []),
            row3_message_ids=list(args.row3_message_id or []),
        )
        if args.output:
            write_vps_runner_script(
                Path(args.output),
                proof_dir=str(args.proof_dir),
                env_file=str(args.env_file),
                compose_file=str(args.compose_file),
                service=str(args.service),
                phase=str(args.phase),
                row3_exclude_message_ids=list(args.row3_exclude_message_id or []),
                row3_message_ids=list(args.row3_message_id or []),
            )
        else:
            print(script, end="")
        return 0

    if args.command == "row3-cohort-args":
        print(format_row3_cohort_cli_args(Path(args.proof_dir)), end="")
        return 0

    if args.command == "classify":
        status = classify_gate_b_artifacts(Path(args.proof_dir))
        proof_dir = Path(args.proof_dir).expanduser().resolve()
        proof_dir.mkdir(parents=True, exist_ok=True)
        (proof_dir / "gate_b_status.json").write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        markdown = render_status_markdown(status)
        (proof_dir / "STATUS.md").write_text(markdown, encoding="utf-8")
        if args.json:
            print(json.dumps(status, indent=2, ensure_ascii=False))
        else:
            print(markdown, end="")
        return 0 if status["status"] in ("green", "yellow") else 1

    if args.command == "write-handoff":
        handoff = build_operator_handoff(Path(args.proof_dir))
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_operator_handoff_text(handoff), encoding="utf-8")
        json_output = Path(args.json_output) if args.json_output else output.with_suffix(".json")
        json_output.write_text(json.dumps(handoff, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 0 if handoff.get("actionable") is True else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
