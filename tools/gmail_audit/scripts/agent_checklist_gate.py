#!/usr/bin/env python3
"""Programmatic PR-AÔÇôG checklist gate ÔÇö exit 0 when all checks DONE."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = TOOL_DIR.parents[2]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))


def _check(name: str, ok: bool, detail: str = "") -> dict:
    return {"id": name, "status": "DONE" if ok else "FAIL", "detail": detail}


def run_checks() -> dict:
    checks: dict[str, dict] = {}
    audit = TOOL_DIR / "agent_runtime"
    docs = REPO_ROOT / "gmail-agent" / "docs"

    # PR-A
    store_py = (audit / "store.py").read_text(encoding="utf-8")
    checks["A1_build_insert_split"] = _check(
        "A1_build_insert_split",
        "def build_snapshot_from_signal" in store_py and "def insert_snapshot" in store_py,
    )
    schema = (audit / "AGENT_RUNTIME_SCHEMA.sql").read_text(encoding="utf-8")
    checks["A2_turns_created_at_no_default"] = _check(
        "A2_turns_created_at_no_default",
        "created_at TIMESTAMPTZ NOT NULL" in schema
        and "DEFAULT NOW()" not in schema.split("agent_runtime_turns")[1].split(");")[0],
    )
    checks["A3_snapshot_version_column"] = _check(
        "A3_snapshot_version_column",
        "snapshot_version" in schema,
    )
    checks["A4_exceptions_reexport"] = _check(
        "A4_exceptions_reexport",
        (audit / "exceptions.py").is_file(),
    )
    checks["A5_migration_002"] = _check(
        "A5_migration_002",
        (audit / "migrations" / "002_turns_append_only.sql").is_file(),
    )

    graph_py = (audit / "graph.py").read_text(encoding="utf-8")
    checks["B1_budget_exhausted"] = _check("B1_budget_exhausted", "budget_exhausted" in graph_py)
    checks["B2_loop_terminal"] = _check(
        "B2_loop_terminal",
        "_LOOP_TERMINAL_CODES" in graph_py and "ready_for_quote" not in graph_py.split("_LOOP_TERMINAL_CODES")[1][:120],
    )
    constitution_md = docs / "core" / "AGENT_CONSTITUTION.md"
    md_text = constitution_md.read_text(encoding="utf-8") if constitution_md.is_file() else ""
    checks["B3_constitution_headers"] = _check(
        "B3_constitution_headers",
        "## identity" in md_text and "## tool_allowlist" in md_text,
    )

    tool_modules = [
        "search_gmail.py",
        "read_drive_file.py",
        "extract_facts.py",
        "list_drive_folder.py",
        "search_rag.py",
        "check_cp2025.py",
        "call_kalk_top.py",
        "generate_draft.py",
        "report_gaps.py",
    ]
    checks["C1_tool_modules"] = _check(
        "C1_tool_modules",
        all((audit / "tools" / name).is_file() for name in tool_modules),
    )
    settings_py = (audit / "settings.py").read_text(encoding="utf-8")
    openai_py = (audit / "openai_agent_client.py").read_text(encoding="utf-8")
    checks["C2_finish_reason_stop"] = _check("C2_finish_reason_stop", 'finish_reason == "stop"' in openai_py)
    checks["C2_model_fallback"] = _check(
        "C2_model_fallback",
        "model_fallback" in settings_py and "fallback_model" in settings_py,
    )
    config_py = (TOOL_DIR / "config.py").read_text(encoding="utf-8")
    checks["C3_tracked_agent_keys"] = _check(
        "C3_tracked_agent_keys",
        '"AGENT_RUNTIME_ENABLED"' in config_py and '"AGENT_MODEL_FALLBACK"' in config_py,
    )
    handlers_py = (audit / "tools" / "handlers.py").read_text(encoding="utf-8")
    checks["C4_apply_facts_block"] = _check(
        "C4_apply_facts_block",
        "def apply_facts_to_snapshot_and_store" in handlers_py,
    )

    settings_py = (audit / "settings.py").read_text(encoding="utf-8")
    checks["D1_unknown_mode_raises"] = _check(
        "D1_unknown_mode_raises",
        "AgentRuntimeConfigError" in settings_py and "invalid:" in settings_py,
    )

    feed_pkg = TOOL_DIR / "daszek_engagement_feed"
    case_py = (feed_pkg / "case.py").read_text(encoding="utf-8") if (feed_pkg / "case.py").is_file() else ""
    checks["E1_thin_feed_package"] = _check(
        "E1_thin_feed_package",
        feed_pkg.is_dir() and "operator_essence_pl" in case_py and "hitl_pending" in case_py,
    )
    checks["E2_no_status_fallback"] = _check(
        "E2_no_status_fallback",
        "Status agenta:" not in case_py,
    )
    app_js = REPO_ROOT / "daszek" / "public" / "app.js"
    app_text = app_js.read_text(encoding="utf-8") if app_js.is_file() else ""
    checks["E4_hitl_ui_buttons"] = _check(
        "E4_hitl_ui_buttons",
        "data-hitl-approve" in app_text and "data-hitl-send" in app_text,
    )

    last_proven = docs / "runbooks" / "LAST_PROVEN_STATE.md"
    lps_text = last_proven.read_text(encoding="utf-8") if last_proven.is_file() else ""
    checks["F1_proof_pack_dir"] = _check(
        "F1_proof_pack_dir",
        last_proven.is_file()
        and "gate-b-row4a-s2-final-20260712T210500Z" in lps_text
        and "gate-b-row4a-s2-replay-b2-20260712T212706Z" in lps_text
        and "gate-b-worker-health-diagnosis-20260713T015059Z" in lps_text,
        "LPS must reference current S2/S2.1 and worker-health proof artifacts; external directories are not required for repo gate PASS",
    )
    checks["F2_last_proven_state"] = _check(
        "F2_last_proven_state",
        "signal_id=trace_id" not in lps_text
        and ("`signal_id` is the stable identity of the signal" in lps_text or "`signal_id` i `engagement_id` pozostają stabilne" in lps_text)
        and ("`trace_id` is technical run correlation" in lps_text or "techniczne trace ID" in lps_text)
        and ("Replay changes `run_id`" in lps_text or "zmieniają się między wykonaniami" in lps_text)
        and ("Row4a S2/S2.1" in lps_text or "Row4a" in lps_text),
    )

    mcp_py = (audit / "mcp_service.py").read_text(encoding="utf-8")
    checks["G1_approve_requires_enabled"] = _check(
        "G1_approve_requires_enabled",
        "enabled=True" in mcp_py and "_enable_action" not in mcp_py,
    )
    checks["G2_trigger_terminal_guard"] = _check(
        "G2_trigger_terminal_guard",
        "pending_operator" in mcp_py and "force: bool" in mcp_py,
    )
    checks["G3_run_id_new_status"] = _check(
        "G3_run_id_new_status",
        '"run_id"' in mcp_py and '"new_status"' in mcp_py,
    )

    env_example = (TOOL_DIR / ".env.example").read_text(encoding="utf-8")
    checks["H8_openrouter_env"] = _check(
        "H8_openrouter_env",
        "AGENT_MODEL_FALLBACK" in env_example,
    )

    constitution_doc = docs / "core" / "AGENT_CONSTITUTION.md"
    constitution_text = constitution_doc.read_text(encoding="utf-8") if constitution_doc.is_file() else ""
    checks["Z0_agent_constitution"] = _check(
        "Z0_agent_constitution",
        constitution_doc.is_file()
        and "forbidden_actions" in constitution_text
        and "hitl_policy" in constitution_text
        and "tool_allowlist" in constitution_text,
    )

    # Runtime smoke (in-process)
    try:
        from agent_runtime.mcp_service import evaluate_agent_mcp_smoke

        smoke = evaluate_agent_mcp_smoke()
        checks["G_smoke"] = _check("G_smoke", bool(smoke.get("ok")), str(smoke.get("checks")))
    except Exception as exc:  # noqa: BLE001
        checks["G_smoke"] = _check("G_smoke", False, str(exc))

    try:
        from agent_runtime.store import build_snapshot_from_signal, InMemoryOperatorEngagementStore

        store = InMemoryOperatorEngagementStore()
        built = build_snapshot_from_signal(
            signal={"signal_id": "gate"},
            case_id="case_gate",
            engagement_id="eng_gate",
        )
        assert store.load_snapshot("eng_gate") is None
        store.insert_snapshot(built)
        assert store.load_snapshot("eng_gate") is not None
        checks["A_runtime_build_insert"] = _check("A_runtime_build_insert", True)
    except Exception as exc:  # noqa: BLE001
        checks["A_runtime_build_insert"] = _check("A_runtime_build_insert", False, str(exc))

    passed = sum(1 for c in checks.values() if c["status"] == "DONE")
    total = len(checks)
    return {
        "ok": passed == total,
        "passed": passed,
        "total": total,
        "checks": checks,
    }


def main() -> int:
    report = run_checks()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
