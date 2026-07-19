"""Regenerate runtime-proof candidate JSON artifacts (local Node B builders only; no Gmail/Daszek I/O).

**NOT a production path.** Offline fixture generator: real ``DecisionCandidate`` (from
``run_decision_pipeline``) and real ``PolicyDecision`` (from ``build_policy_decision`` after policy
evaluation) are required before v2 proposals. Assemble v2 only via
``build_policy_gated_action_proposals_v2_bundle`` - do not paste synthetic ``action_proposals_v2``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = REPO_ROOT / "tools" / "gmail_audit"
TESTS_DIR = TOOL_DIR / "tests"
OUT_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(TOOL_DIR))
sys.path.insert(0, str(TESTS_DIR))

from action_proposal_v2 import build_policy_gated_action_proposals_v2_bundle  # noqa: E402
from case_intelligence import build_case_intelligence  # noqa: E402
from decision_pipeline import run_decision_pipeline  # noqa: E402
from decision_projection_blocks import build_decision_view_blocks  # noqa: E402
from daszek_v3_operational_feed import build_operational_feed_snapshot  # noqa: E402
from gmail_intake import build_case_intelligence_layer  # noqa: E402
from policy_action_proposal import attach_policy_evaluation_to_results, evaluate_policy_for_intake_stage  # noqa: E402
from policy_decision import build_policy_decision  # noqa: E402
from test_decision_pipeline_intake_integration import _minimal_stage, _pipeline_settings  # noqa: E402


def _write(name: str, obj: object) -> Path:
    path = OUT_DIR / name
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    snapshot, intake, case_link, business, reply, action_plan = _minimal_stage()
    settings = _pipeline_settings()

    ci_layer = build_case_intelligence_layer(
        snapshot,
        intake,
        case_link,
        business,
        reply,
        action_plan,
        {"settings": settings},
    )
    uo = ci_layer.get("understanding_output")
    if not isinstance(uo, dict):
        raise SystemExit("expected understanding_output dict")

    ci_core = build_case_intelligence(
        snapshot=snapshot,
        intake_result=intake,
        case_link_result=case_link,
        business_result=business,
        reply_result=reply,
        action_plan_result=action_plan,
    )

    dp_run = run_decision_pipeline(
        snapshot=snapshot,
        intake_result=intake,
        case_link_result=case_link,
        business_result=business,
        intelligence=ci_core,
        understanding_output=uo,
        playbook_service_request_enabled=bool(getattr(settings, "service_request_playbook_enabled", False)),
    )

    mailbox_memory_result: dict = {}
    run_state = {"run_id": "runtime-candidate-local-2026-05-13"}
    pr, prop = evaluate_policy_for_intake_stage(
        action_plan_result=action_plan,
        intake_result=intake,
        case_link_result=case_link,
        entity_link_result=None,
        case_intelligence_result=ci_layer,
        mailbox_memory_result=mailbox_memory_result,
        snapshot=snapshot,
        case_snapshot_hot_state=None,
        run_state=run_state,
    )
    attach_policy_evaluation_to_results(
        mailbox_memory_result=mailbox_memory_result,
        case_intelligence_result=ci_layer,
        policy_report=pr,
        policy_action_proposal=prop,
    )

    cand = (dp_run.get("outputs") or {}).get("decision_candidate")
    if not isinstance(cand, dict) or not cand.get("decision_candidate_id"):
        raise SystemExit("decision_candidate missing")
    pd = build_policy_decision(
        policy_report=pr.to_dict(),
        decision_candidate_id=str(cand.get("decision_candidate_id") or ""),
        decision_candidate=cand,
        case_link_result=case_link,
        dry_run_only=bool(getattr(settings, "decision_pipeline_dry_run_only", True)),
    )
    v2_bundle = build_policy_gated_action_proposals_v2_bundle(
        decision_candidate=cand,
        policy_decision=pd,
        planner_primary_action=str((action_plan or {}).get("primary_action") or "hold"),
        dry_run_only=bool(getattr(settings, "decision_pipeline_dry_run_only", True)),
    )
    proposals_v2 = v2_bundle["action_proposals_v2"]

    merged_ci = dict(ci_layer)
    merged_ci["decision_pipeline"] = dp_run
    merged_ci["policy_decision"] = pd
    merged_ci["action_proposals_v2"] = proposals_v2

    decision_view = build_decision_view_blocks(case_intelligence=merged_ci)

    case_id = str(cand.get("case_id") or "case-local-1")
    cockpit = {
        "desk": {"items": []},
        "cases": {"items": [{"case_id": case_id, "title": "Runtime candidate (local build)"}]},
    }
    feed = build_operational_feed_snapshot(
        cockpit=cockpit,
        day=None,
        tasks=None,
        snapshot_id="decision-pipeline-mvp-runtime-candidate-2026-05-13",
        case_decision_views={case_id: decision_view},
    )

    _write("01_understanding_output.json", uo)
    _write("02_case_intelligence_core.json", ci_core)
    _write("03_decision_pipeline_run.json", dp_run)
    _write("04_decision_candidate.json", cand)
    _write("05_policy_decision.json", pd)
    _write("06_action_proposals_v2.json", proposals_v2)
    _write("07_decision_view.json", decision_view)
    _write("08_operational_feed_snapshot.json", feed)
    _write(
        "00_case_intelligence_layer_merged.json",
        {
            "note": "Full operator-facing intelligence blob used for decision_view (includes understanding_output, pipeline, policy, v2 proposals).",
            "merged_case_intelligence": merged_ci,
        },
    )
    print("Wrote artifacts to", OUT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
