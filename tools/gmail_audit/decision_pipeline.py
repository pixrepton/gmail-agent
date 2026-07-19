"""DecisionPipelineRun orchestration (decision_pipeline_run.v1)."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

from case_type_classifier import build_case_type_result
from decision_candidate import DECISION_CANDIDATE_SCHEMA_VERSION, build_decision_candidate, validate_decision_candidate
from priority_sla_scorer import build_priority_sla_result
from topic_classifier import build_topic_result

PIPELINE_RUN_SCHEMA_VERSION = "decision_pipeline_run.v1"


def _utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _step(name: str, status: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"step_name": name, "status": status}
    row.update(extra)
    return row


def run_decision_pipeline(
    *,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any] | None,
    business_result: dict[str, Any] | None,
    intelligence: dict[str, Any],
    understanding_output: dict[str, Any] | None,
    playbook_service_request_enabled: bool = False,
) -> dict[str, Any]:
    """Build topic / case_type / priority / decision_candidate with step metadata."""
    started = time.perf_counter()
    run_id = hashlib.sha256(f"{_utc()}|pipeline".encode()).hexdigest()[:20]
    steps: list[dict[str, Any]] = []
    errors: list[str] = []

    t0 = time.perf_counter()
    topic_result = build_topic_result(snapshot=snapshot, intake_result=intake_result, business_result=business_result)
    steps.append(
        _step(
            "topic_classifier",
            "ok",
            duration_ms=round((time.perf_counter() - t0) * 1000, 2),
            output_ref=topic_result.get("topic_result_id"),
        )
    )

    if playbook_service_request_enabled:
        tpb = time.perf_counter()
        from playbooks.service_request_intake_v1 import run_service_request_intake_v1

        uo = understanding_output if isinstance(understanding_output, dict) else {}
        conflicts = list(uo.get("conflicting_facts") or []) if isinstance(uo.get("conflicting_facts"), list) else []
        cl_dec = str((case_link_result or {}).get("decision") or "").strip()
        cal_n = 0
        cal = snapshot.get("calendar") if isinstance(snapshot.get("calendar"), dict) else {}
        if isinstance(cal.get("events"), list):
            cal_n = len(cal["events"])
        pb = run_service_request_intake_v1(
            topic_result=topic_result,
            missing_info=intelligence.get("missing_info") if isinstance(intelligence.get("missing_info"), dict) else {},
            conflicting_facts=conflicts,
            case_link_decision=cl_dec,
            calendar_event_count=cal_n,
        )
        steps.append(
            _step(
                "service_request_playbook_v1",
                "ok",
                duration_ms=round((time.perf_counter() - tpb) * 1000, 2),
                output_ref=pb.get("playbook_id"),
            )
        )
    else:
        pb = None

    t1 = time.perf_counter()
    case_type_result = build_case_type_result(
        snapshot=snapshot, intake_result=intake_result, case_link_result=case_link_result
    )
    steps.append(
        _step(
            "case_type_classifier",
            "ok",
            duration_ms=round((time.perf_counter() - t1) * 1000, 2),
            output_ref=case_type_result.get("case_type_result_id"),
        )
    )

    t2 = time.perf_counter()
    missing = intelligence.get("missing_info") if isinstance(intelligence.get("missing_info"), dict) else {}
    priority_sla = build_priority_sla_result(
        snapshot=snapshot,
        intake_result=intake_result,
        missing_info=missing,
        topic_result=topic_result,
    )
    steps.append(
        _step(
            "priority_sla_scorer",
            "ok",
            duration_ms=round((time.perf_counter() - t2) * 1000, 2),
            output_ref=priority_sla.get("priority_sla_result_id"),
        )
    )

    cu = intelligence.get("case_understanding") if isinstance(intelligence.get("case_understanding"), dict) else {}
    case_id = str(cu.get("case_id") or "").strip()
    sm = snapshot.get("source_message") if isinstance(snapshot.get("source_message"), dict) else {}
    source_signal_id = str(sm.get("message_id") or "").strip()

    nba = intelligence.get("next_best_action") if isinstance(intelligence.get("next_best_action"), dict) else {}
    primary = nba.get("primary_next_action") if isinstance(nba.get("primary_next_action"), dict) else {}
    nba_code = str(primary.get("action_type") or "wait")
    context_pack = (
        intelligence.get("mailbox_memory_context_pack")
        if isinstance(intelligence.get("mailbox_memory_context_pack"), dict)
        else {}
    )

    input_refs = {
        "intake_decision": (intake_result.get("decision") or {}),
        "case_link": {"decision": (case_link_result or {}).get("decision")},
    }
    input_hash = hashlib.sha256(json.dumps(input_refs, sort_keys=True, default=str).encode()).hexdigest()[:32]

    t3 = time.perf_counter()
    pipeline_run_id = f"dpr_{run_id}"
    candidate = build_decision_candidate(
        case_id=case_id,
        source_signal_id=source_signal_id,
        topic_result=topic_result,
        case_type_result=case_type_result,
        priority_sla=priority_sla,
        understanding_ref=understanding_output,
        next_best_action_code=nba_code,
        recommended_mode="projection_only",
        case_context_pack=context_pack,
        lineage_supplement={
            "pipeline_run_id": pipeline_run_id,
            "pipeline_schema_version": PIPELINE_RUN_SCHEMA_VERSION,
            "intake_case_link_input_hash": input_hash,
        },
    )
    _cand, cerr = validate_decision_candidate(candidate)
    if cerr:
        errors.extend(cerr)
    steps.append(
        _step(
            "decision_candidate",
            "ok" if not cerr else "degraded",
            duration_ms=round((time.perf_counter() - t3) * 1000, 2),
            output_ref=candidate.get("decision_candidate_id"),
            error_code=";".join(cerr) if cerr else "",
        )
    )

    finished_ms = round((time.perf_counter() - started) * 1000, 2)
    return {
        "schema_version": PIPELINE_RUN_SCHEMA_VERSION,
        "pipeline_run_id": f"dpr_{run_id}",
        "case_id": case_id,
        "source_signal_id": source_signal_id,
        "started_at": _utc(),
        "finished_at": _utc(),
        "input_refs": input_refs,
        "input_hash": input_hash,
        "steps": steps,
        "outputs": {
            "topic_result": topic_result,
            "case_type_result": case_type_result,
            "priority_sla": priority_sla,
            "decision_candidate": candidate,
            "service_request_playbook": pb,
        },
        "warnings": [],
        "errors": errors,
        "skipped_steps": [],
        "result_status": "ok" if not errors else "degraded",
        "projection_ready": (not errors) and bool(candidate.get("decision_candidate_id")),
        "replay_supported": True,
        "duration_ms_total": finished_ms,
        "decision_candidate_schema": DECISION_CANDIDATE_SCHEMA_VERSION,
    }


def replay_decision_pipeline_run(
    *,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any] | None,
    business_result: dict[str, Any] | None,
    intelligence: dict[str, Any],
    understanding_output: dict[str, Any] | None,
    saved_run: dict[str, Any] | None,
    playbook_service_request_enabled: bool = False,
) -> dict[str, Any]:
    """Re-run pipeline and compare input_hash to saved_run when present."""
    new_run = run_decision_pipeline(
        snapshot=snapshot,
        intake_result=intake_result,
        case_link_result=case_link_result,
        business_result=business_result,
        intelligence=intelligence,
        understanding_output=understanding_output,
        playbook_service_request_enabled=playbook_service_request_enabled,
    )
    if isinstance(saved_run, dict) and saved_run.get("input_hash"):
        new_run["replay_input_hash_match"] = saved_run.get("input_hash") == new_run.get("input_hash")
    else:
        new_run["replay_input_hash_match"] = None
    return new_run


__all__ = [
    "PIPELINE_RUN_SCHEMA_VERSION",
    "replay_decision_pipeline_run",
    "run_decision_pipeline",
]
