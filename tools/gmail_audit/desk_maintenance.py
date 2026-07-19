"""Deterministic desk-maintenance preview/apply helpers for Daszek v2."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from artifact_io import append_jsonl, write_json
from event_memory import build_event
from redaction import sanitize_for_storage
from v2_runtime import build_v2_ingest_payload
from v2_semantics import command_from_lifecycle_intent, decision_type_from_command


FEEDBACK_BLOCK_DAYS = 7
STALE_AFTER_HOURS = 72
ACTIVE_VIEW = "desk_day_active"
MAINTENANCE_SHADOW_CONTRACT = "daszek_v2_maintenance"

PRESENCE_ORDER = ("alarm", "strong", "advisory", "standard", "subtle", "silent")
PRESENCE_RANK = {mode: index for index, mode in enumerate(PRESENCE_ORDER)}

RULE_LABELS_PL = {
    "closed_case_move_to_case_only": "Zamknieta sprawa schodzi z biurka do sprawy",
    "merged_case_withdraw": "Polaczona sprawa wycofuje aktywna kartke",
    "stale_presence_soften": "Stara kartka mieknie o jeden poziom",
    "subtle_without_attention_move_to_case_only": "Stara dyskretna kartka schodzi do sprawy",
    "duplicate_active_note_withdraw": "Duplikat aktywnej kartki zostaje wycofany",
}


def collect_maintenance_preview(
    client: Any,
    *,
    case_id: str = "",
    note_id: str = "",
    limit: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or datetime.now().astimezone()
    candidate_items = _load_candidate_items(client, case_id=case_id, note_id=note_id, limit=limit)
    candidate_details = {
        item["note_id"]: client.get_v2_note_detail(item["note_id"])
        for item in candidate_items
        if str(item.get("note_id") or "").strip()
    }

    contexts = [
        _build_candidate_context(detail, now=now_dt)
        for detail in candidate_details.values()
        if isinstance(detail, dict) and isinstance(detail.get("note"), dict)
    ]
    duplicate_losers = _duplicate_losers(contexts)

    candidates: list[dict[str, Any]] = []
    proposed_actions: list[dict[str, Any]] = []
    noops: list[dict[str, Any]] = []

    for context in contexts:
        candidate_record = _candidate_record(context)
        candidates.append(candidate_record)
        proposal = _propose_action(context, duplicate_losers=duplicate_losers)
        if proposal is None:
            noops.append(
                {
                    "note_id": context["note_id"],
                    "case_id": context["case_id"],
                    "status": "noop",
                    "reason_code": "no_action_needed",
                    "reason_pl": "Kartka nie spelnia zadnej deterministycznej reguly maintenance.",
                }
            )
            continue
        if context["feedback_guard"]["blocked"]:
            noops.append(
                {
                    "note_id": context["note_id"],
                    "case_id": context["case_id"],
                    "status": "noop",
                    "reason_code": "blocked_by_recent_feedback",
                    "reason_pl": (
                        "Swiezy manual feedback blokuje maintenance przez "
                        f"{FEEDBACK_BLOCK_DAYS} dni."
                    ),
                    "blocked_until": context["feedback_guard"]["blocked_until"],
                    "blocked_rule_name": proposal["rule_name"],
                    "blocked_rule_label_pl": proposal["rule_label_pl"],
                }
            )
            continue
        proposed_actions.append(proposal)

    summary = _summarize_preview(
        candidates=candidates,
        proposed_actions=proposed_actions,
        noops=noops,
        now=now_dt,
    )

    return {
        "generated_at": now_dt.isoformat(),
        "candidates": candidates,
        "proposed_actions": proposed_actions,
        "noops": noops,
        "summary": summary,
        "candidate_details": candidate_details,
    }


def apply_maintenance_actions(
    client: Any,
    *,
    run_id: str,
    preview: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or datetime.now().astimezone()
    apply_results: list[dict[str, Any]] = []

    for proposal in preview.get("proposed_actions") or []:
        if not isinstance(proposal, dict):
            continue
        refreshed = _recheck_proposal(client, proposal, now=now_dt)
        current_proposal = refreshed.get("proposal")
        if current_proposal is None:
            apply_results.append(
                {
                    "note_id": proposal.get("note_id"),
                    "case_id": proposal.get("case_id"),
                    "rule_name": proposal.get("rule_name"),
                    "status": "noop",
                    "reason_code": "state_changed",
                    "reason_pl": "Stan kartki zmienil sie przed apply i proposal nie jest juz aktualny.",
                }
            )
            continue

        payload = build_maintenance_ingest_payload(
            run_id=run_id,
            proposal=current_proposal,
            note_detail=refreshed["detail"],
            emitted_at=now_dt.isoformat(),
        )
        result = client.push_v2_projection(payload)
        apply_results.append(
            {
                "note_id": current_proposal["note_id"],
                "case_id": current_proposal["case_id"],
                "rule_name": current_proposal["rule_name"],
                "rule_label_pl": current_proposal["rule_label_pl"],
                "status": result.status,
                "signal_id": result.signal_id,
                "trace_id": result.trace_id,
                "details": sanitize_for_storage(result.details),
            }
        )

    summary = _summarize_apply(preview=preview, apply_results=apply_results, now=now_dt)
    return {
        "generated_at": now_dt.isoformat(),
        "apply_results": apply_results,
        "summary": summary,
    }


def build_maintenance_ingest_payload(
    *,
    run_id: str,
    proposal: dict[str, Any],
    note_detail: dict[str, Any],
    emitted_at: str,
) -> dict[str, Any]:
    note = note_detail.get("note") or {}
    case = note_detail.get("case") or {}
    signal_id = _stable_id(
        "sigm",
        proposal.get("stable_anchor") or "",
        proposal.get("rule_name") or "",
        proposal.get("current_presence_mode") or "",
        proposal.get("current_lifecycle_state") or "",
    )
    message_key = f"maintenance:{proposal['note_id']}:{proposal['rule_name']}"
    command = str(proposal.get("persistence_command") or "").strip()
    lifecycle_intent = str(proposal.get("lifecycle_intent") or "").strip()
    target_zone = str(proposal.get("target_surface_zone") or "").strip()
    base_decision_type = decision_type_from_command(
        command,
        lifecycle_intent=lifecycle_intent,
        target_zone=target_zone,
    )
    decision_type = f"maintenance_{base_decision_type}"
    case_id = str(proposal.get("case_id") or "").strip()
    target_presence = str(proposal.get("target_presence_mode") or "silent")

    projection = {
        "signal_projection": {
            "signal_id": signal_id,
            "observed_at": emitted_at,
            "source_kind": "system_maintenance",
            "source_ref": {
                "mailbox": "daszek_v2",
                "message_id": message_key,
                "thread_id": str(note.get("source_message_id") or case_id or proposal["note_id"]),
                "received_at": emitted_at,
            },
            "intake": {
                "decision_action": "maintenance_housekeeping",
                "business_area": str(case.get("business_area") or ""),
                "case_family": str(note.get("case_family") or case.get("family") or "unknown"),
                "state_detected": str(case.get("current_state") or "none"),
                "state_change_detected": False,
                "primary_signal_code": str(proposal.get("rule_name") or ""),
                "primary_signal_name": str(proposal.get("rule_label_pl") or ""),
                "review_required": False,
                "review_flags": [],
                "preclassification_lane": "desk_maintenance",
            },
            "confidence": {
                "signal_confidence": 1.0,
                "case_link_confidence": 1.0 if case_id else 0.0,
                "decision_confidence": 1.0,
                "extraction_confidence": 1.0,
            },
            "artifacts": {
                "run_id": str(run_id or "").strip(),
                "shadow_contract": MAINTENANCE_SHADOW_CONTRACT,
            },
        },
        "case_patch": {
            "command": "noop",
            "case_id": case_id,
            "case_key": str(case.get("case_key") or ""),
            "latest_signal_id": "",
        },
        "desk_note_patch": {
            "command": command,
            "desk_note_id": proposal["note_id"],
            "case_id": case_id,
            "presence_mode": target_presence,
            "surface_zone": str(proposal.get("target_surface_zone") or "silent"),
            "day_bucket": str(proposal.get("target_day_bucket") or note.get("day_bucket") or "dzisiaj"),
            "lifecycle": str(proposal.get("target_lifecycle") or "active"),
            "title_pl": str(note.get("title_pl") or note.get("title") or ""),
            "summary_pl": str(note.get("summary_pl") or note.get("summary") or ""),
            "why_now_pl": str(note.get("why_now_pl") or note.get("why_on_desk") or ""),
            "recommended_next_step_pl": str(
                note.get("recommended_next_step_pl") or note.get("recommended_next_step") or ""
            ),
            "trace_summary_pl": str(proposal.get("reason_summary_pl") or ""),
            "source_signal_ids": [signal_id],
            "source_message_id": str(note.get("source_message_id") or message_key),
            "case_family": str(note.get("case_family") or case.get("family") or "unknown"),
            "business_priority": str(note.get("business_priority") or case.get("business_priority") or "medium"),
            "priority": str(note.get("priority") or "low"),
            "maintenance_origin": True,
        },
        "decision_trace": {
            "trace_id": _stable_id(
                "trace_m",
                proposal.get("stable_anchor") or "",
                proposal.get("rule_name") or "",
                proposal.get("current_presence_mode") or "",
                proposal.get("current_lifecycle_state") or "",
            ),
            "subject_type": "desk_note",
            "subject_id": proposal["note_id"],
            "case_id": case_id,
            "trigger_signal_id": signal_id,
            "decision_type": decision_type,
            "actor": "system_maintenance",
            "reason_summary_pl": str(proposal.get("reason_summary_pl") or ""),
            "presence_mode": target_presence,
            "created_at": emitted_at,
            "maintenance_rule": str(proposal.get("rule_name") or ""),
            "maintenance_rule_label_pl": str(proposal.get("rule_label_pl") or ""),
        },
    }

    operational_events = [
        build_event(
            event_type="desk_note_moved_to_case_only"
            if proposal.get("lifecycle_intent") == "move_to_case_only"
            else "desk_note_revised",
            entity_type="desk_note",
            entity_id=proposal["note_id"],
            case_id=case_id,
            source_signal_id=signal_id,
            payload={
                "decision_type": decision_type,
                "maintenance_rule": proposal.get("rule_name"),
                "target_surface_zone": proposal.get("target_surface_zone"),
                "presence_mode": target_presence,
            },
            actor_type="system",
            stage_name="maintenance",
            stable_anchor=proposal.get("stable_anchor") or proposal["note_id"],
            occurred_at=emitted_at,
        )
    ]

    return build_v2_ingest_payload(
        run_id=run_id,
        message_key=message_key,
        v2_projection=projection,
        operational_events=operational_events,
    )


def persist_maintenance_artifacts(
    run_dir: Path,
    *,
    preview: dict[str, Any],
    apply_result: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    artifact_paths = {
        "candidates": run_dir / "candidates.jsonl",
        "proposed_actions": run_dir / "proposed_actions.jsonl",
        "noops": run_dir / "noops.jsonl",
    }
    for path in artifact_paths.values():
        path.touch(exist_ok=True)
    if manifest:
        write_json(run_dir / "run_manifest.json", sanitize_for_storage(manifest))
    for row in preview.get("candidates") or []:
        append_jsonl(artifact_paths["candidates"], sanitize_for_storage(row))
    for row in preview.get("proposed_actions") or []:
        append_jsonl(artifact_paths["proposed_actions"], sanitize_for_storage(row))
    for row in preview.get("noops") or []:
        append_jsonl(artifact_paths["noops"], sanitize_for_storage(row))
    if apply_result:
        apply_results_path = run_dir / "apply_results.jsonl"
        apply_results_path.touch(exist_ok=True)
        for row in apply_result.get("apply_results") or []:
            append_jsonl(apply_results_path, sanitize_for_storage(row))
        write_json(run_dir / "summary.json", sanitize_for_storage(apply_result["summary"]))
        return
    write_json(run_dir / "summary.json", sanitize_for_storage(preview["summary"]))


def _load_candidate_items(
    client: Any,
    *,
    case_id: str = "",
    note_id: str = "",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    desk = client.get_v2_desk(include_subtle=True)
    day = client.get_v2_day(include_subtle=True)
    deduped: dict[str, dict[str, Any]] = {}

    for item in desk.get("items") or []:
        if isinstance(item, dict):
            deduped[str(item.get("note_id") or "").strip()] = item

    for section in day.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for item in section.get("items") or []:
            if not isinstance(item, dict):
                continue
            deduped.setdefault(str(item.get("note_id") or "").strip(), item)

    items = [item for key, item in deduped.items() if key]
    if case_id:
        items = [item for item in items if str(item.get("case_id") or "").strip() == case_id]
    if note_id:
        items = [item for item in items if str(item.get("note_id") or "").strip() == note_id]
    items.sort(key=_note_card_sort_key)
    if limit is not None:
        return items[:limit]
    return items


def _build_candidate_context(detail: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    note = detail.get("note") or {}
    case = detail.get("case") or {}
    feedback_guard = _feedback_guard(note.get("feedback_state") or {}, now=now)
    last_activity_at = _max_iso(
        [
            str(note.get("updated_at") or ""),
            str(note.get("latest_signal_at") or ""),
            str(case.get("latest_signal_at") or ""),
            str((note.get("feedback_state") or {}).get("ostatnia_akcja_at") or ""),
        ]
    )
    has_attention = _has_unresolved_attention(note)

    return {
        "detail": detail,
        "note": note,
        "case": case,
        "note_id": str(note.get("note_id") or note.get("desk_note_id") or "").strip(),
        "case_id": str(note.get("case_id") or case.get("case_id") or "").strip(),
        "presence_mode": _normalize_presence_mode(note.get("presence_mode")),
        "lifecycle_state": str(note.get("lifecycle_state") or "active").strip() or "active",
        "surface_zone": str(note.get("surface_zone") or "silent").strip() or "silent",
        "updated_at": str(note.get("updated_at") or "").strip(),
        "latest_signal_id": _pick_latest_signal_id(note, case),
        "latest_signal_at": str(note.get("latest_signal_at") or case.get("latest_signal_at") or "").strip(),
        "last_activity_at": last_activity_at,
        "stale": _is_stale(last_activity_at, now=now),
        "has_unresolved_attention": has_attention,
        "feedback_guard": feedback_guard,
        "case_status": str(case.get("status") or "open").strip() or "open",
        "case_open_desk_note_id": str(case.get("open_desk_note_id") or "").strip(),
    }


def _candidate_record(context: dict[str, Any]) -> dict[str, Any]:
    note = context["note"]
    case = context["case"]
    feedback_guard = context["feedback_guard"]
    return {
        "note_id": context["note_id"],
        "case_id": context["case_id"],
        "scope": ACTIVE_VIEW,
        "title": str(note.get("title") or ""),
        "case_title": str(case.get("title") or ""),
        "presence_mode": context["presence_mode"],
        "surface_zone": context["surface_zone"],
        "lifecycle_state": context["lifecycle_state"],
        "case_status": context["case_status"],
        "updated_at": context["updated_at"],
        "latest_signal_at": context["latest_signal_at"],
        "last_activity_at": context["last_activity_at"],
        "stale": bool(context["stale"]),
        "has_unresolved_attention": bool(context["has_unresolved_attention"]),
        "feedback_blocked": bool(feedback_guard["blocked"]),
        "feedback_blocked_until": feedback_guard["blocked_until"],
        "feedback_last_action": feedback_guard["last_action"],
    }


def _propose_action(
    context: dict[str, Any],
    *,
    duplicate_losers: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    note = context["note"]
    case = context["case"]
    if context["case_status"] == "merged":
        return _build_proposal(
            context,
            rule_name="merged_case_withdraw",
            lifecycle_intent="withdraw",
            target_presence_mode="silent",
            target_surface_zone="silent",
            target_lifecycle="withdrawn",
            reason_summary_pl="Sprawa jest juz polaczona, wiec aktywna kartka nie powinna pozostawac na biurku.",
        )
    if context["case_status"] == "closed":
        return _build_proposal(
            context,
            rule_name="closed_case_move_to_case_only",
            lifecycle_intent="move_to_case_only",
            target_presence_mode="silent",
            target_surface_zone="case_only",
            target_lifecycle="active",
            reason_summary_pl="Sprawa jest zamknieta, wiec temat powinien pozostac tylko w sprawie bez aktywnej presji na biurku.",
        )
    duplicate_meta = duplicate_losers.get(context["note_id"])
    if duplicate_meta:
        keeper_title = str(duplicate_meta.get("keeper_title") or "inna kartka sprawy")
        return _build_proposal(
            context,
            rule_name="duplicate_active_note_withdraw",
            lifecycle_intent="withdraw",
            target_presence_mode="silent",
            target_surface_zone="silent",
            target_lifecycle="withdrawn",
            reason_summary_pl=(
                "Ta kartka duplikuje aktywna uwage dla tej samej sprawy; keeperem zostaje "
                f'"{keeper_title}".'
            ),
        )
    if context["presence_mode"] == "subtle" and context["stale"] and not context["has_unresolved_attention"]:
        return _build_proposal(
            context,
            rule_name="subtle_without_attention_move_to_case_only",
            lifecycle_intent="move_to_case_only",
            target_presence_mode="silent",
            target_surface_zone="case_only",
            target_lifecycle="active",
            reason_summary_pl="Kartka jest dyskretna, stara i nie trzyma juz realnej uwagi operacyjnej, wiec schodzi tylko do sprawy.",
        )
    next_presence = _softened_presence(context["presence_mode"])
    if context["stale"] and next_presence:
        return _build_proposal(
            context,
            rule_name="stale_presence_soften",
            lifecycle_intent="deescalate_presence",
            target_presence_mode=next_presence,
            target_surface_zone=context["surface_zone"],
            target_lifecycle="active",
            reason_summary_pl="Kartka jest stara i bez swiezego sygnalu, wiec maintenance oslabia jej widocznosc o jeden poziom.",
        )
    return None


def _build_proposal(
    context: dict[str, Any],
    *,
    rule_name: str,
    lifecycle_intent: str,
    target_presence_mode: str,
    target_surface_zone: str,
    target_lifecycle: str,
    reason_summary_pl: str,
) -> dict[str, Any]:
    persistence_command = command_from_lifecycle_intent(lifecycle_intent, target_zone=target_surface_zone)
    decision_type = decision_type_from_command(
        persistence_command,
        lifecycle_intent=lifecycle_intent,
        target_zone=target_surface_zone,
    )
    note = context["note"]
    return {
        "note_id": context["note_id"],
        "case_id": context["case_id"],
        "title": str(note.get("title") or ""),
        "rule_name": rule_name,
        "rule_label_pl": RULE_LABELS_PL[rule_name],
        "lifecycle_intent": lifecycle_intent,
        "persistence_command": persistence_command,
        "decision_type": decision_type,
        "current_presence_mode": context["presence_mode"],
        "current_surface_zone": context["surface_zone"],
        "current_lifecycle_state": context["lifecycle_state"],
        "target_presence_mode": target_presence_mode,
        "target_surface_zone": target_surface_zone,
        "target_day_bucket": "w_najblizszym_czasie"
        if target_surface_zone == "case_only"
        else str(note.get("day_bucket") or "dzisiaj"),
        "target_lifecycle": target_lifecycle,
        "reason_summary_pl": reason_summary_pl,
        "stable_anchor": _stable_anchor(context, rule_name=rule_name),
        "latest_signal_id": context["latest_signal_id"],
        "latest_signal_at": context["latest_signal_at"],
    }


def _duplicate_losers(contexts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_case: dict[str, list[dict[str, Any]]] = {}
    for context in contexts:
        case_id = context["case_id"]
        if not case_id:
            continue
        by_case.setdefault(case_id, []).append(context)

    losers: dict[str, dict[str, Any]] = {}
    for group in by_case.values():
        if len(group) < 2:
            continue
        keeper = _select_duplicate_keeper(group)
        for context in group:
            if context["note_id"] == keeper["note_id"]:
                continue
            losers[context["note_id"]] = {
                "keeper_note_id": keeper["note_id"],
                "keeper_title": str(keeper["note"].get("title") or ""),
            }
    return losers


def _select_duplicate_keeper(group: list[dict[str, Any]]) -> dict[str, Any]:
    case_open_note_id = str(group[0].get("case_open_desk_note_id") or "").strip()
    if case_open_note_id:
        for context in group:
            if context["note_id"] == case_open_note_id:
                return context
    return sorted(group, key=_context_sort_key)[0]


def _recheck_proposal(client: Any, proposal: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    if proposal.get("rule_name") == "duplicate_active_note_withdraw":
        refreshed = collect_maintenance_preview(client, case_id=str(proposal.get("case_id") or ""), now=now)
    else:
        refreshed = collect_maintenance_preview(
            client,
            case_id=str(proposal.get("case_id") or ""),
            note_id=str(proposal.get("note_id") or ""),
            now=now,
        )
    refreshed_detail = (refreshed.get("candidate_details") or {}).get(str(proposal.get("note_id") or ""))
    refreshed_proposal = None
    for candidate in refreshed.get("proposed_actions") or []:
        if (
            str(candidate.get("note_id") or "") == str(proposal.get("note_id") or "")
            and str(candidate.get("rule_name") or "") == str(proposal.get("rule_name") or "")
        ):
            refreshed_proposal = candidate
            break
    return {"detail": refreshed_detail, "proposal": refreshed_proposal}


def _feedback_guard(feedback_state: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    last_action_at = str(feedback_state.get("ostatnia_akcja_at") or "").strip()
    last_action = str(feedback_state.get("ostatnia_akcja") or "").strip()
    if not last_action_at:
        return {"blocked": False, "blocked_until": "", "last_action": last_action}
    parsed = _parse_iso(last_action_at)
    if parsed is None:
        return {"blocked": False, "blocked_until": "", "last_action": last_action}
    blocked_until = parsed + timedelta(days=FEEDBACK_BLOCK_DAYS)
    return {
        "blocked": blocked_until > now,
        "blocked_until": blocked_until.isoformat(),
        "last_action": last_action,
    }


def _has_unresolved_attention(note: dict[str, Any]) -> bool:
    if list(note.get("unresolved_questions") or []):
        return True
    if list(note.get("risks") or []) or str(note.get("risk_summary_pl") or "").strip():
        return True
    if list(note.get("missing_info") or []) or str(note.get("missing_info_summary_pl") or "").strip():
        return True
    action_type = str(note.get("primary_next_action_type") or "").strip()
    if action_type and action_type not in {"wait", "move_to_case_only", "ignore", "none"}:
        return True
    return False


def _is_stale(value: str, *, now: datetime) -> bool:
    parsed = _parse_iso(value)
    if parsed is None:
        return False
    return now - parsed >= timedelta(hours=STALE_AFTER_HOURS)


def _softened_presence(current: str) -> str | None:
    current_mode = _normalize_presence_mode(current)
    if current_mode in {"silent", "subtle"}:
        return None
    index = PRESENCE_ORDER.index(current_mode)
    return PRESENCE_ORDER[index + 1]


def _note_card_sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
    presence = _normalize_presence_mode(item.get("presence_mode"))
    visibility = float(item.get("visibility_score") or 0.0)
    updated_at = str(item.get("updated_at") or "")
    return (
        PRESENCE_RANK.get(presence, PRESENCE_RANK["silent"]),
        -visibility,
        -_timestamp_value(updated_at),
        str(item.get("note_id") or ""),
    )


def _context_sort_key(context: dict[str, Any]) -> tuple[int, float, str]:
    return (
        PRESENCE_RANK.get(_normalize_presence_mode(context["presence_mode"]), PRESENCE_RANK["silent"]),
        -_timestamp_value(str(context.get("updated_at") or "")),
        str(context.get("note_id") or ""),
    )


def _normalize_presence_mode(value: Any) -> str:
    text = str(value or "").strip()
    return text if text in PRESENCE_RANK else "silent"


def _max_iso(values: list[str]) -> str:
    best_value = ""
    best_ts = float("-inf")
    for value in values:
        parsed = _parse_iso(value)
        if parsed is None:
            continue
        candidate_ts = parsed.timestamp()
        if candidate_ts > best_ts:
            best_ts = candidate_ts
            best_value = parsed.isoformat()
    if not best_value:
        return ""
    return best_value


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _timestamp_value(value: str) -> float:
    parsed = _parse_iso(value)
    return parsed.timestamp() if parsed else 0.0


def _pick_latest_signal_id(note: dict[str, Any], case: dict[str, Any]) -> str:
    source_signal_ids = note.get("source_signal_ids")
    if isinstance(source_signal_ids, list):
        for signal_id in source_signal_ids:
            candidate = str(signal_id or "").strip()
            if candidate:
                return candidate
    return str(case.get("latest_signal_id") or "").strip()


def _stable_anchor(context: dict[str, Any], *, rule_name: str) -> str:
    return "::".join(
        part
        for part in (
            context["case_id"],
            context["note_id"],
            rule_name,
            context["latest_signal_id"],
            context["latest_signal_at"],
            context["presence_mode"],
            context["lifecycle_state"],
        )
        if part
    )


def _stable_id(prefix: str, *parts: str) -> str:
    seed = "::".join(str(part or "").strip() for part in parts if str(part or "").strip())
    if not seed:
        seed = prefix
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _summarize_preview(
    *,
    candidates: list[dict[str, Any]],
    proposed_actions: list[dict[str, Any]],
    noops: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    actions_by_rule = Counter(str(item.get("rule_name") or "") for item in proposed_actions)
    actions_by_intent = Counter(str(item.get("lifecycle_intent") or "") for item in proposed_actions)
    noop_reasons = Counter(str(item.get("reason_code") or "") for item in noops)
    return {
        "mode": "preview",
        "generated_at": now.isoformat(),
        "candidate_count": len(candidates),
        "proposed_action_count": len(proposed_actions),
        "noop_count": len(noops),
        "feedback_blocked_count": sum(1 for item in noops if item.get("reason_code") == "blocked_by_recent_feedback"),
        "duplicate_count": sum(1 for item in proposed_actions if item.get("rule_name") == "duplicate_active_note_withdraw"),
        "actions_by_rule": dict(sorted(actions_by_rule.items())),
        "actions_by_lifecycle_intent": dict(sorted(actions_by_intent.items())),
        "noop_reasons": dict(sorted(noop_reasons.items())),
    }


def _summarize_apply(
    *,
    preview: dict[str, Any],
    apply_results: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    status_counts = Counter(str(item.get("status") or "") for item in apply_results)
    return {
        "mode": "apply",
        "generated_at": now.isoformat(),
        "candidate_count": int((preview.get("summary") or {}).get("candidate_count") or 0),
        "proposed_action_count": int((preview.get("summary") or {}).get("proposed_action_count") or 0),
        "apply_attempted_count": len(apply_results),
        "apply_status_counts": dict(sorted(status_counts.items())),
        "apply_noop_count": sum(1 for item in apply_results if item.get("status") == "noop"),
        "apply_failed_count": sum(1 for item in apply_results if item.get("status") == "failed"),
    }


__all__ = [
    "ACTIVE_VIEW",
    "FEEDBACK_BLOCK_DAYS",
    "MAINTENANCE_SHADOW_CONTRACT",
    "STALE_AFTER_HOURS",
    "apply_maintenance_actions",
    "build_maintenance_ingest_payload",
    "collect_maintenance_preview",
    "persist_maintenance_artifacts",
]
