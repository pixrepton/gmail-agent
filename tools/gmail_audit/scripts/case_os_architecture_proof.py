#!/usr/bin/env python3
"""Gate B: Case OS architecture phases P0–P6 (full product).

Stdout on success: CASE_OS_PRODUCT_PROOF_OK
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[4]
TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

DIAGRAMS_MD = WORKSPACE / "knowledge" / "docs" / "daszek-system-diagrams.md"
PROPOSAL_MD = WORKSPACE / "knowledge" / "docs" / "case-os-target-architecture-proposal.md"
SYNC_SCRIPT = WORKSPACE / "daszek" / "scripts" / "sync_system_diagrams_manifest.py"
DASZEK_APP = WORKSPACE / "daszek" / "public" / "app.js"
DASZEK_INDEX = WORKSPACE / "daszek" / "public" / "index.php"
CIEPLO_SETTINGS = WORKSPACE / "cieplo-orchestrator" / "src" / "topinstal_cieplo_worker" / "settings.py"
FASTKALK_DISPATCH = (
    WORKSPACE / "fast-kalk" / "wp-content" / "plugins" / "topinstal-lead-widget" / "includes" / "class-offer-dispatch.php"
)
GENERATOR_USE_CASE = (
    WORKSPACE / "top-instal-generator" / "core" / "application" / "GenerateOfferDocumentUseCase.php"
)


def _run(cmd: list[str], *, cwd: Path | None = None) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd or WORKSPACE))
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"command failed ({' '.join(cmd)}): {detail}")


def _verify_p0() -> None:
    if not DIAGRAMS_MD.is_file():
        raise RuntimeError("P0: missing daszek-system-diagrams.md")
    if not PROPOSAL_MD.is_file():
        raise RuntimeError("P0: missing case-os-target-architecture-proposal.md")
    if "Case OS" not in DIAGRAMS_MD.read_text(encoding="utf-8"):
        raise RuntimeError("P0: diagrams missing Case OS model")
    _run([sys.executable, str(SYNC_SCRIPT), "--check"])
    print("CASE_OS_P0_PROOF_OK")


def _verify_p1() -> None:
    _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tools/gmail_audit/tests/test_case_os_architecture.py",
            "tools/gmail_audit/tests/test_case_os_product.py",
            "-q",
        ],
        cwd=WORKSPACE / "gmail-agent",
    )
    api = (WORKSPACE / "gmail-agent" / "tools" / "gmail_audit" / "api_app.py").read_text(encoding="utf-8")
    skrzat = (WORKSPACE / "gmail-agent" / "tools" / "gmail_audit" / "skrzat_copilot.py").read_text(encoding="utf-8")
    if "context_pack_lineage" not in api or "validate_operator_case_context_pack" not in api:
        raise RuntimeError("P1: api_app missing pack contract enforcement")
    if "build_rag_advisory_slice" not in skrzat or not (WORKSPACE / "gmail-agent" / "tools" / "gmail_audit" / "skrzat_rag_advisory.py").is_file():
        raise RuntimeError("P1: missing RAG advisory slice module")
    print("CASE_OS_P1_PROOF_OK")


def _verify_p2() -> None:
    feed = (WORKSPACE / "gmail-agent" / "tools" / "gmail_audit" / "daszek_v3_operational_feed.py").read_text(
        encoding="utf-8"
    )
    platform = (WORKSPACE / "gmail-agent" / "tools" / "gmail_audit" / "case_os_platform.py").read_text(encoding="utf-8")
    if "resolve_feed_action_proposals" not in feed or "merge_decision_view_with_pipeline_proposals" not in platform:
        raise RuntimeError("P2: feed exporter missing pipeline proposal merge")
    app = DASZEK_APP.read_text(encoding="utf-8")
    if "decision_pipeline_v2" not in app or "source_spine" not in app:
        raise RuntimeError("P2: Daszek UI missing pipeline proposal spine markers")
    print("CASE_OS_P2_PROOF_OK")


def _verify_p3() -> None:
    wf_events = (
        WORKSPACE / "cieplo-orchestrator" / "src" / "topinstal_cieplo_worker" / "integrations" / "event_spine" / "workflow_events.py"
    ).read_text(encoding="utf-8")
    if "_workflow_engagement_id" not in wf_events or "engagement_id=engagement_id" not in wf_events:
        raise RuntimeError("P3: cieplo workflow events missing engagement_id wiring")
    dispatch = FASTKALK_DISPATCH.read_text(encoding="utf-8")
    if "engagementId" not in dispatch:
        raise RuntimeError("P3: fast-kalk generator request missing engagementId")
    if not GENERATOR_USE_CASE.is_file() or "resolve_engagement_id" not in GENERATOR_USE_CASE.read_text(encoding="utf-8"):
        raise RuntimeError("P3: top-instal-generator missing engagement_id resolver")
    print("CASE_OS_P3_PROOF_OK")


def _verify_p4() -> None:
    _run(
        [sys.executable, "-m", "pytest", "tests/test_case_os_d3_ingress.py", "-q"],
        cwd=WORKSPACE / "cieplo-orchestrator",
    )
    settings_txt = CIEPLO_SETTINGS.read_text(encoding="utf-8")
    if "cieplo_gmail_poll_enabled" not in settings_txt or "CIEPLO_GMAIL_POLL_ENABLED" not in settings_txt:
        raise RuntimeError("P4: missing CIEPLO_GMAIL_POLL_ENABLED setting")
    processor = (
        WORKSPACE / "cieplo-orchestrator" / "src" / "topinstal_cieplo_worker" / "ingress" / "processor.py"
    ).read_text(encoding="utf-8")
    if "gmail_poll_disabled" not in processor:
        raise RuntimeError("P4: ingress processor missing poll gate")
    print("CASE_OS_P4_PROOF_OK")


def _verify_p5() -> None:
    app = DASZEK_APP.read_text(encoding="utf-8")
    index = DASZEK_INDEX.read_text(encoding="utf-8")
    for marker in ("Biurko Case OS", "System Case OS", "Kanał Oferta HVAC"):
        if marker not in app:
            raise RuntimeError(f"P5: app.js missing copy marker: {marker}")
    if "Biurko Case OS" not in index or "Sprawy Case OS" not in index:
        raise RuntimeError("P5: index.php nav missing Case OS labels")
    print("CASE_OS_P5_PROOF_OK")


def _verify_p6() -> None:
    _run(
        [sys.executable, "-m", "pytest", "tools/gmail_audit/tests/test_case_os_product.py", "-q"],
        cwd=WORKSPACE / "gmail-agent",
    )
    print("CASE_OS_P6_PROOF_OK")


def main() -> int:
    try:
        _verify_p0()
        _verify_p1()
        _verify_p2()
        _verify_p3()
        _verify_p4()
        _verify_p5()
        _verify_p6()
        print("CASE_OS_ARCHITECTURE_PROOF_OK")
        print("CASE_OS_PRODUCT_PROOF_OK")
        return 0
    except Exception as exc:
        print(f"CASE_OS_ARCHITECTURE_PROOF_FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
