"""Build projection_proof_report.json from gmail_audit run artifacts."""

from __future__ import annotations

import argparse
import glob as glob_module
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from artifact_io import read_jsonl

_UI_MANUAL_NOTE = "record accepted/readback found; browser/UI verification remains manual"
_UI_READBACK_DISABLED_NOTE = (
    "readback not enabled or not recorded for this message; store/API proof only; UI verification remains manual"
)
_V3_ENDPOINT = "/wp-json/daszek/v3/operational-feed-snapshots"
_FEED_PRIMARY_SOURCE = "engagement_snapshot_v2"
_SUSPECT_MOJIBAKE_TOKENS = ("Ã", "Â", "Ä", "Å", "â", "Ă", "Ĺ", "Ë", "™", "„", "˛", "˘")


def _normalize_title_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    def suspicious_score(candidate: str) -> int:
        return sum(candidate.count(token) for token in _SUSPECT_MOJIBAKE_TOKENS)

    best = text
    best_score = suspicious_score(best)
    for codec in ("cp1250", "cp1252", "latin1"):
        candidate = best
        for _ in range(2):
            try:
                repaired = candidate.encode(codec).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                break
            repaired = repaired.strip()
            if repaired and suspicious_score(repaired) < suspicious_score(candidate):
                candidate = repaired
                continue
            break
        candidate_score = suspicious_score(candidate)
        if candidate_score < best_score:
            best = candidate
            best_score = candidate_score
    return best


def _load_run_manifest(run_dir: Path) -> dict[str, Any]:
    for name in ("run_manifest.json", "manifest.json"):
        path = run_dir / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _feed_primary_mode(manifest: dict[str, Any]) -> bool:
    agent = manifest.get("agent_runtime") if isinstance(manifest.get("agent_runtime"), dict) else {}
    feed_source = str(agent.get("daszek_feed_source") or "").strip()
    if feed_source == _FEED_PRIMARY_SOURCE:
        return True
    legacy_allowed = agent.get("daszek_legacy_v2_push_allowed")
    auto_push = manifest.get("daszek_operational_feed_auto_push_enabled")
    if legacy_allowed is False and bool(auto_push):
        return True
    return False


def _latest_v3_feed_row(rows: list[Any], message_id: str, record_type: str) -> dict[str, Any] | None:
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        if str(row.get("message_id") or "") != message_id:
            continue
        if str(row.get("record_type") or "") == record_type:
            return row
    return None


def _latest_v1_policy(rows: list[Any], message_id: str) -> dict[str, Any] | None:
    latest = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("message_id") or "") != message_id:
            continue
        if row.get("record_type") != "push_policy":
            continue
        if str(row.get("surface") or "") == "v1":
            latest = row
    return latest


def _latest_v2_policy(rows: list[Any], message_id: str) -> dict[str, Any] | None:
    latest = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("message_id") or "") != message_id:
            continue
        if row.get("record_type") != "push_policy":
            continue
        surf = str(row.get("surface") or "")
        if surf in {"v2", "v2_operator_projection"}:
            latest = row
    return latest


def _latest_projection_skip(rows: list[Any], message_id: str) -> dict[str, Any] | None:
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        if str(row.get("message_id") or "") != message_id:
            continue
        if row.get("record_type") == "projection_skip":
            return row
    return None


def _latest_projection_failure(rows: list[Any], message_id: str) -> dict[str, Any] | None:
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        if str(row.get("message_id") or "") != message_id:
            continue
        if row.get("record_type") == "projection_failure":
            return row
    return None


def _latest_ingest(rows: list[Any], message_id: str) -> dict[str, Any] | None:
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        if str(row.get("message_id") or "") != message_id:
            continue
        if row.get("status") == "ingested":
            return row
    return None


def _latest_readback(rows: list[Any], message_id: str) -> dict[str, Any] | None:
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        if str(row.get("message_id") or "") != message_id:
            continue
        if row.get("record_type") == "v2_readback":
            return row
    return None


def _rollup_proof_summary(items: list[dict[str, Any]], *, feed_primary_config: bool = False) -> dict[str, Any]:
    """Aggregate counters for operator batch / proof closure (forward-compatible keys)."""
    by_status = dict(Counter(str(row.get("policy_status") or "unknown") for row in items))

    v2_accepted = sum(
        1
        for row in items
        if str(row.get("policy_status") or "") == "accepted_projection" and str(row.get("surface") or "") == "v2_ingest"
    )
    v1_accepted = sum(
        1
        for row in items
        if str(row.get("policy_status") or "") == "accepted_projection" and str(row.get("surface") or "") == "v1_tasks"
    )

    v2_blocked = sum(
        1
        for row in items
        if str(row.get("policy_status") or "") == "blocked_policy" and str(row.get("surface") or "") == "v2_ingest"
    )
    v1_blocked = sum(
        1
        for row in items
        if str(row.get("policy_status") or "") == "blocked_policy" and str(row.get("surface") or "") == "v1_tasks"
    )

    rb_found = sum(1 for row in items if str(row.get("store_readback") or "") == "found")
    rb_missing = sum(
        1
        for row in items
        if str(row.get("policy_status") or "") == "accepted_projection"
        and str(row.get("surface") or "") == "v2_ingest"
        and str(row.get("store_readback") or "") != "found"
    )

    ui_expected = sum(1 for row in items if bool(row.get("ui_visibility_expected")))
    handoff_actionable = sum(1 for row in items if bool(row.get("handoff_actionable")))
    feed_handoff_actionable = sum(1 for row in items if bool(row.get("feed_handoff_actionable")))

    v3_accepted = sum(
        1
        for row in items
        if str(row.get("policy_status") or "") == "accepted_projection"
        and str(row.get("surface") or "") == "v3_operational_feed"
    )
    v3_failed = sum(
        1
        for row in items
        if str(row.get("policy_status") or "") == "projection_failed"
        and str(row.get("surface") or "") == "v3_operational_feed"
    )
    v3_skipped = sum(
        1
        for row in items
        if str(row.get("surface") or "") == "v3_operational_feed"
        and str(row.get("policy_status") or "").startswith("skipped_")
    )
    snapshot_ids = [
        str(row.get("snapshot_id") or "").strip()
        for row in items
        if str(row.get("surface") or "") == "v3_operational_feed" and str(row.get("snapshot_id") or "").strip()
    ]

    feed_primary = feed_primary_config or v3_accepted > 0
    primary_surface_mode = "feed_first" if feed_primary else ("legacy_v2" if v2_accepted > 0 else "none")
    technical_transport = "daszek_v3_operational_feed" if feed_primary else "daszek_v2_compat"

    manual = sum(
        1
        for row in items
        if str(row.get("policy_status") or "")
        in {"blocked_policy", "projection_failed", "unknown", "skipped_missing_v2_projection"}
    )
    manual += rb_missing

    return {
        "product_surface": "Daszek V3 operator projection",
        "technical_transport": technical_transport,
        "primary_surface_mode": primary_surface_mode,
        "daszek_feed_source": _FEED_PRIMARY_SOURCE if feed_primary else "legacy_projection_v3",
        "aggregates_by_policy_status": by_status,
        "message_count": len(items),
        "operator_projection_accepted": v2_accepted + v3_accepted,
        "operator_projection_blocked_policy": v2_blocked,
        "operator_projection_skipped": int(by_status.get("skipped_config_disabled", 0)) + v3_skipped,
        "operator_projection_failed": int(by_status.get("projection_failed", 0)),
        "readback_found": rb_found,
        "readback_missing": rb_missing,
        "v2_projection_accepted": v2_accepted,
        "v1_projection_accepted": v1_accepted,
        "v2_projection_blocked_policy": v2_blocked,
        "v1_projection_blocked_policy": v1_blocked,
        "v2_projection_skipped": int(by_status.get("skipped_config_disabled", 0)),
        "v2_projection_failed": int(by_status.get("projection_failed", 0)),
        "v2_readback_found": rb_found,
        "v2_readback_missing": rb_missing,
        "v3_feed_push_ok": v3_accepted,
        "v3_feed_push_failed": v3_failed,
        "v3_feed_push_skipped": v3_skipped,
        "v3_feed_snapshot_ids": snapshot_ids,
        "ui_visibility_expected": ui_expected,
        "operator_handoff_actionable": handoff_actionable,
        "feed_handoff_actionable": feed_handoff_actionable,
        "manual_intervention_required": manual,
    }


def _engagement_id_from_stage(stage_row: dict[str, Any]) -> str:
    signal_projection = stage_row.get("signal_projection") if isinstance(stage_row.get("signal_projection"), dict) else {}
    case_patch = stage_row.get("case_patch") if isinstance(stage_row.get("case_patch"), dict) else {}
    desk_patch = stage_row.get("desk_note_patch") if isinstance(stage_row.get("desk_note_patch"), dict) else {}
    projection_preview = stage_row.get("projection_preview") if isinstance(stage_row.get("projection_preview"), dict) else {}
    cir = stage_row.get("case_intelligence_result") if isinstance(stage_row.get("case_intelligence_result"), dict) else {}
    signal_projection_agent = (
        signal_projection.get("agent_runtime") if isinstance(signal_projection.get("agent_runtime"), dict) else {}
    )
    candidates = [
        desk_patch.get("engagement_id"),
        case_patch.get("engagement_id"),
        case_patch.get("agent_engagement_id"),
        projection_preview.get("engagement_id"),
        signal_projection.get("engagement_id"),
        signal_projection_agent.get("engagement_id"),
        cir.get("engagement_id"),
    ]
    for key in ("mailbox_memory_result", "mailbox_memory_results", "reconcile_signal_result", "agent_reconcile_result"):
        block = stage_row.get(key)
        if isinstance(block, dict):
            candidates.append(block.get("engagement_id"))
            preview = block.get("preview")
            if isinstance(preview, dict):
                candidates.append(preview.get("engagement_id"))
            mm = block.get("mailbox_memory")
            if isinstance(mm, dict):
                candidates.append(mm.get("engagement_id"))
    for raw in candidates:
        text = str(raw or "").strip()
        if text:
            return text
    return ""


def _title_from_stage(stage_row: dict[str, Any], *, message_id: str) -> str:
    desk_patch = stage_row.get("desk_note_patch") if isinstance(stage_row.get("desk_note_patch"), dict) else {}
    title = _normalize_title_text(desk_patch.get("title") or desk_patch.get("title_pl") or "")
    if title:
        return title
    for key in ("intake_result", "intake_results_final", "business_reasoning_result"):
        block = stage_row.get(key)
        if not isinstance(block, dict):
            continue
        for field in ("subject", "title", "title_pl"):
            text = _normalize_title_text(block.get(field) or "")
            if text:
                return text
    return f"Wiadomosc Gmail: {message_id}" if message_id else ""


def _feed_title_from_stage(stage_row: dict[str, Any], *, case_id: str) -> str:
    signal_projection = (
        stage_row.get("signal_projection") if isinstance(stage_row.get("signal_projection"), dict) else {}
    )
    agent_runtime = signal_projection.get("agent_runtime") if isinstance(signal_projection.get("agent_runtime"), dict) else {}
    hvac_profile = agent_runtime.get("hvac_profile") if isinstance(agent_runtime.get("hvac_profile"), dict) else {}
    location = hvac_profile.get("location") if isinstance(hvac_profile.get("location"), dict) else {}

    parts: list[str] = []
    city = _normalize_title_text(location.get("city") or "")
    if city:
        parts.append(city)
    heated_area = hvac_profile.get("heated_area_m2")
    heated_text = str(heated_area or "").strip()
    if heated_text:
        parts.append(f"{heated_text} m²")
    if parts:
        return " — ".join(parts)
    if case_id:
        return f"Sprawa {case_id}"
    return ""


def _feed_title_for_handoff(
    stage_row: dict[str, Any] | None,
    *,
    message_id: str,
    case_id: str,
    v3_row: dict[str, Any] | None,
) -> str:
    if isinstance(v3_row, dict):
        for field in ("title", "title_pl"):
            text = _normalize_title_text(v3_row.get(field) or "")
            if text:
                return text
    if isinstance(stage_row, dict):
        title = _feed_title_from_stage(stage_row, case_id=case_id)
        if title:
            return title
        return _title_from_stage(stage_row, message_id=message_id)
    return f"Wiadomosc Gmail: {message_id}" if message_id else ""


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _snapshot_payload_from_v3_row(v3_row: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(v3_row, dict):
        return {}
    payload = v3_row.get("snapshot_payload")
    return payload if isinstance(payload, dict) else {}


def _exact_snapshot_membership(
    snapshot_payload: dict[str, Any],
    *,
    message_id: str,
    signal_id: str,
    engagement_id: str,
    case_id: str,
) -> dict[str, Any]:
    feed = snapshot_payload.get("feed") if isinstance(snapshot_payload.get("feed"), dict) else {}
    desk = feed.get("desk") if isinstance(feed.get("desk"), list) else []
    target_message = str(message_id or "").strip()
    target_signal = str(signal_id or "").strip()
    target_engagement = str(engagement_id or "").strip()
    target_case = str(case_id or "").strip()

    for row in desk:
        if not isinstance(row, dict):
            continue
        row_note_id = str(row.get("note_id") or row.get("desk_note_id") or "").strip()
        row_title = _normalize_title_text(row.get("title") or row.get("title_pl") or "")
        row_message = str(row.get("source_message_id") or row.get("message_id") or "").strip()
        row_signals = _string_list(row.get("source_signal_ids"))
        row_engagement = str(row.get("engagement_id") or "").strip()
        row_case = str(row.get("case_id") or "").strip()

        if target_message and row_message != target_message:
            continue
        if target_signal and target_signal not in row_signals:
            continue
        if target_engagement and row_engagement != target_engagement:
            continue
        if target_case and row_case != target_case:
            continue

        return {
            "matched": True,
            "card_id": row_note_id,
            "title": row_title,
            "source_message_id": row_message,
            "source_signal_ids": row_signals,
            "engagement_id": row_engagement,
            "case_id": row_case,
        }

    return {
        "matched": False,
        "card_id": "",
        "title": "",
        "source_message_id": "",
        "source_signal_ids": [],
        "engagement_id": "",
        "case_id": "",
    }


def build_projection_proof_rows(run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    stage_path = run_dir / "stage_records.jsonl"
    v1_path = run_dir / "daszek_push_results.jsonl"
    v2_path = run_dir / "daszek_v2_push_results.jsonl"
    v3_path = run_dir / "daszek_v3_feed_push_results.jsonl"
    manifest = _load_run_manifest(run_dir)
    feed_primary = _feed_primary_mode(manifest)

    stages = read_jsonl(stage_path, allow_missing=True)
    v1_rows = read_jsonl(v1_path, allow_missing=True)
    v2_rows = read_jsonl(v2_path, allow_missing=True)
    v3_rows = read_jsonl(v3_path, allow_missing=True)

    message_ids: list[str] = []
    seen: set[str] = set()
    for row in stages:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("message_id") or "").strip()
        if mid and mid not in seen:
            seen.add(mid)
            message_ids.append(mid)

    items: list[dict[str, Any]] = []
    for mid in message_ids:
        stage_row = None
        for row in reversed(stages):
            if isinstance(row, dict) and str(row.get("message_id") or "") == mid:
                stage_row = row
                break

        primary_action = ""
        signal_id = ""
        case_id = ""
        case_understanding_case_id = ""
        note_id = ""
        title = ""
        source_message_id = mid
        source_signal_ids: list[str] = []
        engagement_id = ""
        snapshot_id = ""
        feed_visible = False
        feed_handoff_actionable = False
        handoff_tier = ""
        exact_snapshot_membership = {
            "matched": False,
            "card_id": "",
            "title": "",
            "source_message_id": "",
            "source_signal_ids": [],
            "engagement_id": "",
            "case_id": "",
        }
        if isinstance(stage_row, dict):
            apr = stage_row.get("action_plan_result") if isinstance(stage_row.get("action_plan_result"), dict) else {}
            primary_action = str(apr.get("primary_action") or "")
            cir = stage_row.get("case_intelligence_result") if isinstance(stage_row.get("case_intelligence_result"), dict) else {}
            cu = cir.get("case_understanding") if isinstance(cir.get("case_understanding"), dict) else {}
            case_understanding_case_id = str(cu.get("case_id") or "")
            signal_projection = stage_row.get("signal_projection") if isinstance(stage_row.get("signal_projection"), dict) else {}
            case_patch = stage_row.get("case_patch") if isinstance(stage_row.get("case_patch"), dict) else {}
            desk_patch = stage_row.get("desk_note_patch") if isinstance(stage_row.get("desk_note_patch"), dict) else {}
            signal_id = str(signal_projection.get("signal_id") or "").strip()
            source_ref = signal_projection.get("source_ref") if isinstance(signal_projection.get("source_ref"), dict) else {}
            case_id = str(case_patch.get("case_id") or desk_patch.get("case_id") or "").strip()
            if not case_id and case_understanding_case_id:
                case_id = case_understanding_case_id
            note_id = str(desk_patch.get("desk_note_id") or desk_patch.get("note_id") or "").strip()
            title = _title_from_stage(stage_row, message_id=mid)
            source_message_id = str(desk_patch.get("source_message_id") or source_ref.get("message_id") or mid).strip()
            source_signal_ids = _string_list(desk_patch.get("source_signal_ids"))
            engagement_id = _engagement_id_from_stage(stage_row)

        skip = _latest_projection_skip(v2_rows, mid)
        failure = _latest_projection_failure(v2_rows, mid)
        ingest = _latest_ingest(v2_rows, mid)
        v2_pol = _latest_v2_policy(v2_rows, mid)
        v1_pol = _latest_v1_policy(v1_rows, mid)
        rb = _latest_readback(v2_rows, mid)

        policy_status = "unknown"
        surface = "none"
        endpoint = ""
        record_id = ""
        reason = ""
        push_policy_reason = ""
        reason_code = ""
        store_readback = "not_checked"
        readback_reason = ""
        ui_visibility_expected = False
        ui_visibility_verified = False
        ui_visibility_note = ""
        readback_note_id = ""
        readback_case_id = ""
        readback_title = ""
        readback_source_message_id = ""
        readback_source_signal_ids: list[str] = []
        operator_action_available = False
        allowed_operator_actions: list[str] = []
        expected_bridge_domain = ""
        expected_adjudication_kind = ""
        handoff_actionable = False

        if skip:
            skip_reason = str(skip.get("push_policy_reason") or "")
            if skip_reason == "skipped_missing_v2_projection":
                policy_status = "skipped_missing_v2_projection"
                surface = "v2_ingest"
                endpoint = "/wp-json/daszek/v2/ingest"
                reason = str(skip.get("push_policy_detail") or skip.get("reason") or "")
                push_policy_reason = skip_reason
                reason_code = "skipped_missing_v2_projection"
                ui_visibility_note = "Shadow v2 contract missing before ingest; see errors/model stages."
            else:
                policy_status = "skipped_config_disabled"
                surface = "v2_ingest"
                endpoint = "/wp-json/daszek/v2/ingest"
                reason = str(skip.get("push_policy_detail") or skip.get("reason") or "")
                push_policy_reason = str(skip.get("push_policy_reason") or "")
                reason_code = "skipped_v2_config_disabled"
                ui_visibility_note = "v2 push disabled in config; no ingest attempted"
        elif failure and not ingest:
            policy_status = "projection_failed"
            surface = "v2_ingest"
            endpoint = "/wp-json/daszek/v2/ingest"
            reason = str(failure.get("error") or failure.get("detail") or "")
            push_policy_reason = str(failure.get("push_policy_reason") or "")
            reason_code = "projection_ingest_exception"
            ui_visibility_note = "ingest failed before readback; UI verification remains manual"
        elif ingest:
            policy_status = "accepted_projection"
            surface = "v2_ingest"
            endpoint = "/wp-json/daszek/v2/ingest"
            record_id = str(ingest.get("trace_id") or ingest.get("signal_id") or "")
            if not signal_id:
                signal_id = str(ingest.get("signal_id") or "").strip()
            push_policy_reason = str(ingest.get("push_policy_reason") or "")
            reason_code = "accepted_v2_ingest"
            if rb:
                store_readback = str(rb.get("store_readback") or "not_checked")
                readback_reason = str(rb.get("readback_reason") or "")
                ui_visibility_expected = bool(rb.get("ui_visibility_expected"))
                readback_note_id = str(rb.get("readback_note_id") or "").strip()
                readback_case_id = str(rb.get("readback_case_id") or "").strip()
                readback_title = _normalize_title_text(rb.get("readback_title") or "")
                readback_source_message_id = str(rb.get("readback_source_message_id") or "").strip()
                readback_source_signal_ids = _string_list(rb.get("readback_source_signal_ids"))
                operator_action_available = bool(rb.get("operator_action_available"))
                allowed_operator_actions = _string_list(rb.get("allowed_operator_actions"))
                expected_bridge_domain = str(rb.get("expected_bridge_domain") or "").strip()
                expected_adjudication_kind = str(rb.get("expected_adjudication_kind") or "").strip()
                if store_readback == "found" and ui_visibility_expected:
                    ui_visibility_note = _UI_MANUAL_NOTE
                elif store_readback == "found":
                    ui_visibility_note = "readback found; UI list visibility not inferred without desk_note_id/case_id"
                else:
                    ui_visibility_note = (
                        f"readback degraded ({store_readback}): {readback_reason or 'see v2_readback row'}"
                    )
            else:
                store_readback = "not_checked"
                readback_reason = ""
                ui_visibility_expected = False
                ui_visibility_note = _UI_READBACK_DISABLED_NOTE
        elif v2_pol:
            surface = "v2_ingest"
            endpoint = "/wp-json/daszek/v2/ingest"
            allowed = bool(v2_pol.get("allowed"))
            policy_status = "accepted_projection" if allowed else "blocked_policy"
            reason = str(v2_pol.get("push_policy_detail") or v2_pol.get("push_policy_reason") or "")
            push_policy_reason = str(v2_pol.get("push_policy_reason") or "")
            reason_code = str(v2_pol.get("push_policy_reason") or ("allowed_v2_policy" if allowed else "blocked_v2_policy"))
            if not allowed:
                ui_visibility_note = "operator projection blocked by policy before ingest"
        elif v1_pol:
            surface = "v1_tasks"
            endpoint = "/wp-json/daszek/v1/tasks"
            allowed = bool(v1_pol.get("allowed"))
            policy_status = "accepted_projection" if allowed else "blocked_policy"
            reason = str(v1_pol.get("push_policy_detail") or "")
            push_policy_reason = str(v1_pol.get("push_policy_reason") or "")
            reason_code = "v1_push_policy"
            if not allowed:
                ui_visibility_note = "legacy v1 push blocked by policy"

            if not allowed:
                ui_visibility_note = "legacy v1 push blocked by policy"

        v3_success = _latest_v3_feed_row(v3_rows, mid, "feed_success")
        v3_failure = _latest_v3_feed_row(v3_rows, mid, "feed_failure")
        v3_skip = _latest_v3_feed_row(v3_rows, mid, "feed_skip")
        has_v3_record = bool(v3_success or v3_failure or v3_skip)
        use_v3_primary = feed_primary and has_v3_record

        if use_v3_primary and v3_success:
            policy_status = "accepted_projection"
            surface = "v3_operational_feed"
            endpoint = _V3_ENDPOINT
            snapshot_id = str(v3_success.get("snapshot_id") or "").strip()
            record_id = snapshot_id
            reason_code = "accepted_v3_operational_feed"
            push_policy_reason = "allowed_v3_operational_feed"
            reason = ""
            if not engagement_id:
                engagement_id = str(v3_success.get("engagement_id") or "").strip()
            exact_snapshot_membership = _exact_snapshot_membership(
                _snapshot_payload_from_v3_row(v3_success),
                message_id=source_message_id or mid,
                signal_id=signal_id,
                engagement_id=engagement_id,
                case_id=case_id,
            )
            if exact_snapshot_membership["matched"]:
                note_id = str(exact_snapshot_membership.get("card_id") or "").strip()
                title = _normalize_title_text(exact_snapshot_membership.get("title") or "") or _feed_title_for_handoff(
                    stage_row, message_id=mid, case_id=case_id, v3_row=v3_success
                )
                source_message_id = str(exact_snapshot_membership.get("source_message_id") or "").strip() or source_message_id
                source_signal_ids = _string_list(exact_snapshot_membership.get("source_signal_ids"))
                engagement_id = str(exact_snapshot_membership.get("engagement_id") or "").strip() or engagement_id
                case_id = str(exact_snapshot_membership.get("case_id") or "").strip() or case_id
            else:
                title = _feed_title_for_handoff(stage_row, message_id=mid, case_id=case_id, v3_row=v3_success)
            counts = v3_success.get("counts") if isinstance(v3_success.get("counts"), dict) else {}
            feed_visible = bool(counts.get("desk") or counts.get("cases") or counts.get("tasks"))
            ui_visibility_expected = feed_visible
            ui_visibility_note = "v3 feed push accepted; Cockpit/feed visibility remains manual verification"
            store_readback = "not_applicable"
            readback_reason = "v3 feed-primary mode does not use v2 desk readback"
        elif use_v3_primary and v3_failure:
            policy_status = "projection_failed"
            surface = "v3_operational_feed"
            endpoint = _V3_ENDPOINT
            snapshot_id = str(v3_failure.get("snapshot_id") or "").strip()
            record_id = snapshot_id
            reason = str(v3_failure.get("error") or "")
            reason_code = "v3_feed_push_exception"
            push_policy_reason = "v3_operational_feed_push_failed"
            ui_visibility_note = "v3 feed push failed before operator visibility proof"
        elif use_v3_primary and v3_skip:
            surface = "v3_operational_feed"
            endpoint = _V3_ENDPOINT
            skip_reason = str(v3_skip.get("reason") or "feed_skip")
            reason = str(v3_skip.get("push_policy_detail") or v3_skip.get("reason") or "")
            push_policy_reason = skip_reason
            reason_code = skip_reason
            if skip_reason == "skipped_projection_refresh_not_needed":
                policy_status = "skipped_projection_refresh"
            elif skip_reason == "debounced_min_interval":
                policy_status = "skipped_debounced"
            elif skip_reason == "skipped_no_daszek_client":
                policy_status = "skipped_no_daszek_client"
            else:
                policy_status = "skipped_feed_push"
            ui_visibility_note = f"v3 feed push skipped ({skip_reason})"
        elif feed_primary and policy_status == "unknown" and bool(manifest.get("daszek_operational_feed_auto_push_enabled")):
            surface = "v3_operational_feed"
            endpoint = _V3_ENDPOINT
            policy_status = "skipped_no_feed_push_record"
            reason_code = "skipped_no_feed_push_record"
            push_policy_reason = "skipped_no_feed_push_record"
            reason = "feed auto-push enabled but no v3 feed JSONL row for this message"
            ui_visibility_note = reason

        handoff_actionable = (
            policy_status == "accepted_projection"
            and surface == "v2_ingest"
            and store_readback == "found"
            and bool(signal_id)
            and bool(case_id)
            and bool(note_id)
            and bool(title)
            and bool(source_message_id)
            and bool(readback_note_id)
            and bool(readback_case_id)
            and readback_note_id == note_id
            and readback_case_id == case_id
            and operator_action_available
            and "zla_sprawa" in allowed_operator_actions
            and expected_bridge_domain == "adjudication"
            and expected_adjudication_kind == "reject_same_case"
        )
        feed_handoff_mode = ""
        feed_handoff_actionable = (
            policy_status == "accepted_projection"
            and surface == "v3_operational_feed"
            and bool(exact_snapshot_membership.get("matched"))
            and bool(signal_id)
            and bool(title)
            and bool(source_message_id)
            and bool(snapshot_id)
            and bool(note_id)
            and (bool(case_id) or bool(engagement_id))
        )
        if feed_handoff_actionable:
            feed_handoff_mode = "case_ready" if bool(case_id) else "staging"
            handoff_tier = "row4a"
        elif handoff_actionable:
            handoff_tier = "row4b"

        product_surface = "Daszek V3 operator projection"
        if surface == "v1_tasks":
            product_surface = "legacy v1 /tasks"
        elif surface == "v2_ingest":
            product_surface = "Daszek V3 operator projection"
        technical_transport = (
            "daszek_v3_operational_feed"
            if surface == "v3_operational_feed"
            else ("daszek_v2_compat" if surface == "v2_ingest" else "daszek_v1_compat")
        )

        items.append(
            {
                "message_id": mid,
                "source_message_id": source_message_id,
                "signal_id": signal_id,
                "case_id": case_id,
                "case_understanding_case_id": case_understanding_case_id,
                "note_id": note_id,
                "title": title,
                "source_signal_ids": source_signal_ids,
                "engagement_id": engagement_id,
                "snapshot_id": snapshot_id,
                "feed_visible": feed_visible,
                "exact_snapshot_membership": bool(exact_snapshot_membership.get("matched")),
                "exact_card_id": str(exact_snapshot_membership.get("card_id") or ""),
                "exact_title": _normalize_title_text(exact_snapshot_membership.get("title") or ""),
                "exact_source_message_id": str(exact_snapshot_membership.get("source_message_id") or ""),
                "exact_source_signal_ids": _string_list(exact_snapshot_membership.get("source_signal_ids")),
                "primary_surface_mode": "feed_first" if feed_primary else "legacy_v2",
                "handoff_tier": handoff_tier,
                "primary_action": primary_action,
                "policy_status": policy_status,
                "product_surface": product_surface,
                "technical_transport": technical_transport,
                "surface": surface,
                "endpoint": endpoint,
                "record_id": record_id,
                "push_policy_reason": push_policy_reason,
                "reason_code": reason_code or policy_status,
                "reason": reason,
                "store_readback": store_readback,
                "readback_reason": readback_reason,
                "readback_note_id": readback_note_id,
                "readback_case_id": readback_case_id,
                "readback_title": readback_title,
                "readback_source_message_id": readback_source_message_id,
                "readback_source_signal_ids": readback_source_signal_ids,
                "operator_action_available": operator_action_available,
                "allowed_operator_actions": allowed_operator_actions,
                "expected_bridge_domain": expected_bridge_domain,
                "expected_adjudication_kind": expected_adjudication_kind,
                "handoff_actionable": handoff_actionable,
                "feed_handoff_actionable": feed_handoff_actionable,
                "feed_handoff_mode": feed_handoff_mode,
                "ui_visibility_expected": ui_visibility_expected,
                "ui_visibility_verified": ui_visibility_verified,
                "ui_visibility_note": ui_visibility_note,
            }
        )

    summary = _rollup_proof_summary(items, feed_primary_config=feed_primary)
    return items, summary


def write_projection_proof_report(run_dir: Path, *, out_path: Path | None = None) -> Path:
    run_dir = run_dir.resolve()
    items, summary = build_projection_proof_rows(run_dir)
    payload = {"run_dir": str(run_dir), "summary": summary, "items": items}
    target = out_path or (run_dir / "projection_proof_report.json")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Build projection_proof_report.json from run artifact JSONL files.")
    parser.add_argument("--run-dir", help="Single gmail_audit run directory")
    parser.add_argument("--out", help="Output path (default: <run-dir>/projection_proof_report.json)")
    parser.add_argument(
        "--run-dirs-glob",
        help="Glob of run directories (non-recursive unless pattern contains **); merges items into one JSON.",
    )
    args = parser.parse_args()

    if args.run_dirs_glob:
        paths = sorted(Path(p) for p in glob_module.glob(args.run_dirs_glob, recursive=True) if Path(p).is_dir())
        if not paths:
            print("No directories matched --run-dirs-glob.", file=sys.stderr)
            return 1
        combined: list[dict[str, Any]] = []
        runs_meta: list[dict[str, Any]] = []
        for rd in paths:
            items, summary = build_projection_proof_rows(rd)
            combined.extend(items)
            runs_meta.append({"run_dir": str(rd.resolve()), "summary": summary})
        merged_summary = _rollup_proof_summary(combined)
        merged_summary["runs_merged"] = len(paths)
        out_path = Path(args.out or "projection_proof_report.batch.json")
        out_path.write_text(
            json.dumps({"runs": runs_meta, "summary": merged_summary, "items": combined}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(str(out_path.resolve()))
        return 0

    if not args.run_dir:
        parser.error("Either --run-dir or --run-dirs-glob is required.")
    out = write_projection_proof_report(Path(args.run_dir), out_path=Path(args.out) if args.out else None)
    print(str(out.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
